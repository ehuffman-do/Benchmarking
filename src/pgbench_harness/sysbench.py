"""sysbench command construction and execution with a live line-buffered tee.

The target password is **never** placed on the command line. It is exported to
the child process environment as ``PGPASSWORD`` (sysbench's pgsql driver uses
libpq, which falls back to ``PGPASSWORD``/``PGSSLMODE`` when the corresponding
options are unset). Command lines are therefore safe to log and to print in
``--dry-run``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Exit code returned when the harness watchdog aborts an attempt (mirrors the
# conventional 124 used by coreutils `timeout`).
WATCHDOG_EXIT_CODE = 124

from pgbench_harness.errors import RunError
from pgbench_harness.spec import Spec
from pgbench_harness.util import get_redactor


@dataclass(frozen=True)
class SysbenchCommand:
    """A fully-built sysbench invocation."""

    argv: tuple[str, ...]
    cwd: Optional[str]

    def display(self) -> str:
        """Human-readable command line (no secrets are ever present in argv)."""
        prefix = f"cd {self.cwd} && " if self.cwd else ""
        return prefix + " ".join(self.argv)


def child_env(spec: Spec, password: str) -> dict[str, str]:
    """Environment for sysbench/psql children: password and sslmode via libpq vars."""
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    env["PGSSLMODE"] = spec.target.sslmode
    return env


def _connection_args(spec: Spec) -> list[str]:
    t = spec.target
    return [
        "--db-driver=pgsql",
        f"--pgsql-host={t.host}",
        f"--pgsql-port={t.port}",
        f"--pgsql-user={t.user}",
        f"--pgsql-db={t.database}",
    ]


def _workload_args(spec: Spec) -> tuple[str, list[str], Optional[str]]:
    """Return (script, workload args, cwd) for the configured workload."""
    w = spec.workload
    if w.type == "tpcc":
        script = "./tpcc.lua"
        args = [f"--tables={w.tables}", f"--scale={w.scale}"]
        cwd: Optional[str] = w.tpcc_path
    else:
        script = w.type
        args = [f"--tables={w.tables}", f"--table-size={w.table_size}"]
        cwd = None
    return script, args + list(w.extra_args), cwd


def build_run_command(spec: Spec, threads: int) -> SysbenchCommand:
    """Build the sysbench `run` command for one thread level."""
    script, wargs, cwd = _workload_args(spec)
    argv = (
        ["sysbench", script]
        + _connection_args(spec)
        + wargs
        + [
            f"--threads={threads}",
            f"--time={spec.sweep.duration_s}",
            "--report-interval=1",
            "--percentile=99",
        ]
        + (["--histogram"] if spec.capture.histogram else [])
        + ["run"]
    )
    return SysbenchCommand(argv=tuple(argv), cwd=cwd)


def build_prepare_command(spec: Spec) -> SysbenchCommand:
    """Build the sysbench `prepare` command (parallel load, capped at 16 threads)."""
    script, wargs, cwd = _workload_args(spec)
    threads = min(16, max(spec.sweep.threads))
    argv = (
        ["sysbench", script]
        + _connection_args(spec)
        + wargs
        + [f"--threads={threads}", "prepare"]
    )
    return SysbenchCommand(argv=tuple(argv), cwd=cwd)


class _Watchdog:
    """Kills a child process that stalls or overruns its time budget.

    A long unattended soak must never hang forever: ``--time`` only bounds a
    cooperative sysbench, but a network black-hole or a connection torn out
    mid-failover can leave the process blocked well past its deadline. The
    watchdog runs in a daemon thread and terminates the child when either:

    * no output line has arrived for ``stall_timeout_s`` (the process is wedged
      — with ``--report-interval=1`` a healthy run emits a line every second), or
    * total runtime exceeds ``hard_timeout_s`` (it is ignoring ``--time``).

    When either timeout is ``None`` the corresponding check is disabled; with
    both ``None`` the watchdog thread never starts (preserving prior behaviour
    for quiet phases like ``prepare``).
    """

    def __init__(
        self, proc: "subprocess.Popen[str]", stall_timeout_s: Optional[float],
        hard_timeout_s: Optional[float], logger: logging.Logger,
    ) -> None:
        self._proc = proc
        self._stall = stall_timeout_s
        self._hard = hard_timeout_s
        self._logger = logger
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._last_beat = self._start
        self._killed_reason: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if stall_timeout_s or hard_timeout_s:
            self._thread = threading.Thread(target=self._watch, daemon=True)
            self._thread.start()

    def beat(self) -> None:
        """Record that a line of output just arrived."""
        with self._lock:
            self._last_beat = time.monotonic()

    def _watch(self) -> None:
        while not self._stop.wait(2.0):
            now = time.monotonic()
            with self._lock:
                since_line = now - self._last_beat
            elapsed = now - self._start
            reason: Optional[str] = None
            if self._hard and elapsed > self._hard:
                reason = f"exceeded hard time limit of {int(self._hard)}s"
            elif self._stall and since_line > self._stall:
                reason = (f"no output for {int(since_line)}s "
                          f"(stall limit {int(self._stall)}s)")
            if reason:
                self._killed_reason = reason
                self._logger.error("watchdog: terminating sysbench — %s", reason)
                self._terminate()
                return

    def _terminate(self) -> None:
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception:  # pragma: no cover - best-effort kill
            pass

    def finish(self) -> Optional[str]:
        """Stop the watchdog thread; return the kill reason if it fired."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self._killed_reason


def run_streaming(
    cmd: SysbenchCommand,
    env: dict[str, str],
    log_path: Path,
    logger: logging.Logger,
    heartbeat_every: int = 60,
    stall_timeout_s: Optional[float] = None,
    hard_timeout_s: Optional[float] = None,
) -> int:
    """Run *cmd*, teeing stdout+stderr line-buffered to *log_path* live.

    Every line is flushed to the raw log immediately so logs are inspectable
    mid-run; a heartbeat (the latest line) goes to the harness logger every
    *heartbeat_every* lines. If *stall_timeout_s* or *hard_timeout_s* is set, a
    watchdog terminates a wedged/overrunning child; in that case a ``FATAL:``
    marker is appended to the log and :data:`WATCHDOG_EXIT_CODE` is returned (so
    callers treat it as a failed attempt). Otherwise returns the process exit
    code.
    """
    redact = get_redactor().redact
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            list(cmd.argv),
            cwd=cmd.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RunError(
            f"could not execute '{cmd.argv[0]}': {exc}",
            hint="install sysbench (see README) and re-run preflight.",
        ) from exc
    watchdog = _Watchdog(proc, stall_timeout_s, hard_timeout_s, logger)
    lines_seen = 0
    with open(log_path, "w", encoding="utf-8") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(redact(line))
            log.flush()
            lines_seen += 1
            watchdog.beat()
            if lines_seen % heartbeat_every == 0:
                logger.info("    %s", redact(line.rstrip()))
        rc = proc.wait()
        reason = watchdog.finish()
        if reason:
            log.write(f"FATAL: harness watchdog aborted this attempt: {reason}\n")
            log.flush()
            return WATCHDOG_EXIT_CODE  # terminate yields a signal code; normalize it
    return rc


def sysbench_version() -> str:
    """Return `sysbench --version` output, raising RunError if unavailable."""
    try:
        out = subprocess.run(
            ["sysbench", "--version"], capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError as exc:
        raise RunError(
            "sysbench is not installed or not on PATH",
            hint="see README 'Installing sysbench' for Ubuntu 24.04 instructions.",
        ) from exc
    return out.stdout.strip() or out.stderr.strip()
