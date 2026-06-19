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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pgbench_harness.errors import RunError
from pgbench_harness.parser import INTERVAL_RE
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
    assert spec.sweep is not None
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


def build_soak_command(spec: Spec, threads: int, time_s: int) -> SysbenchCommand:
    """Build a soak `run` command: fixed concurrency for *time_s* seconds.

    Note on outage survival: sysbench's `--ignore-errors` is MySQL-driver only;
    the pgsql driver has no equivalent, so on a connection reset sysbench may
    exit. We do NOT inject `--reconnect` (it would distort steady throughput);
    instead the runner's supervisor relaunches sysbench for the remaining time,
    and the gap is measured as downtime. See runner._soak_supervisor.
    """
    assert spec.soak is not None
    script, wargs, cwd = _workload_args(spec)
    argv = (
        ["sysbench", script]
        + _connection_args(spec)
        + wargs
        + [
            f"--threads={threads}",
            f"--time={time_s}",
            f"--report-interval={spec.soak.report_interval_s}",
            "--percentile=99",
        ]
        + (["--histogram"] if spec.capture.histogram else [])
        + ["run"]
    )
    return SysbenchCommand(argv=tuple(argv), cwd=cwd)


def build_prepare_command(spec: Spec) -> SysbenchCommand:
    """Build the sysbench `prepare` command (parallel load, capped at 16 threads)."""
    script, wargs, cwd = _workload_args(spec)
    if spec.sweep is not None:
        peak = max(spec.sweep.threads)
    else:
        assert spec.soak is not None
        peak = spec.soak.threads
    threads = min(16, peak)
    argv = (
        ["sysbench", script]
        + _connection_args(spec)
        + wargs
        + [f"--threads={threads}", "prepare"]
    )
    return SysbenchCommand(argv=tuple(argv), cwd=cwd)


def run_streaming(
    cmd: SysbenchCommand,
    env: dict[str, str],
    log_path: Path,
    logger: logging.Logger,
    heartbeat_every: int = 60,
) -> int:
    """Run *cmd*, teeing stdout+stderr line-buffered to *log_path* live.

    Every line is flushed to the raw log immediately so logs are inspectable
    mid-run; a heartbeat (the latest line) goes to the harness logger every
    *heartbeat_every* lines. Returns the process exit code.
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
    lines_seen = 0
    with open(log_path, "w", encoding="utf-8") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(redact(line))
            log.flush()
            lines_seen += 1
            if lines_seen % heartbeat_every == 0:
                logger.info("    %s", redact(line.rstrip()))
    return proc.wait()


def run_streaming_timestamped(
    cmd: SysbenchCommand,
    env: dict[str, str],
    log_path: Path,
    logger: logging.Logger,
    heartbeat_every: int = 120,
) -> tuple[int, int]:
    """Run *cmd*, teeing each line to *log_path* prefixed with the read-time UTC.

    Each line becomes ``<ISO-8601 UTC>\\t<original line>``. The timestamp is the
    moment the load generator received the line — i.e. ~the end of that 1s
    sysbench interval — which is the clock used to align events and provider
    graphs. The raw log stays parseable by INTERVAL_RE (it ``search``es, so the
    prefix is ignored). Returns ``(exit_code, interval_line_count)`` so the
    supervisor can detect launches that produced no samples.
    """
    redact = get_redactor().redact
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            list(cmd.argv), cwd=cmd.cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RunError(
            f"could not execute '{cmd.argv[0]}': {exc}",
            hint="install sysbench (see README) and re-run preflight.",
        ) from exc
    intervals = 0
    seen = 0
    with open(log_path, "w", encoding="utf-8") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            log.write(f"{ts}\t{redact(line)}")
            log.flush()
            if INTERVAL_RE.search(line):
                intervals += 1
            seen += 1
            if seen % heartbeat_every == 0:
                logger.info("    %s", redact(line.rstrip()))
    return proc.wait(), intervals


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
