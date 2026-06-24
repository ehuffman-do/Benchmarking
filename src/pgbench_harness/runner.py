"""Run orchestration: preflight wiring, prepare, the sweep loop, dry-run, resume."""

from __future__ import annotations

import dataclasses
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from pgbench_harness import capture, report, sysbench
from pgbench_harness.errors import PreflightError, RunError
from pgbench_harness.manifest import (
    STATUS_FAILED, STATUS_OK, STATUS_RUNNING, Level, Manifest, plan_levels,
)
from pgbench_harness.spec import Spec, dump_spec_copy, load_spec
from pgbench_harness.summarize import write_parsed
from pgbench_harness.util import (
    atomic_write_json, atomic_write_text, fmt_duration, get_logger, get_redactor,
    make_run_id, read_json, setup_logging, utc_now_iso,
)


def planned_budget_s(spec: Spec) -> float:
    """Planned wall-clock budget: sum of durations plus inter-level cooldowns."""
    n = len(spec.sweep.threads) * spec.sweep.repetitions
    return n * spec.sweep.duration_s + max(0, n - 1) * spec.sweep.cooldown_s


def print_dry_run(spec: Spec) -> None:
    """Print the exact sysbench command per level and the wall-clock budget."""
    print(f"# dry run for label '{spec.run.label}' "
          f"({spec.sweep.repetitions} repetition(s) x {len(spec.sweep.threads)} levels)")
    for rep in range(1, spec.sweep.repetitions + 1):
        for threads in spec.sweep.threads:
            cmd = sysbench.build_run_command(spec, threads)
            print(f"[rep {rep}, {threads:>4} threads] {cmd.display()}")
    print(f"# planned wall-clock budget: {fmt_duration(planned_budget_s(spec))} "
          f"({len(spec.sweep.threads) * spec.sweep.repetitions} x {spec.sweep.duration_s}s "
          f"+ cooldowns of {spec.sweep.cooldown_s}s)")
    print("# password source: env var "
          f"{spec.target.password_env} -> PGPASSWORD (never on the command line)")


def _dataset_error(check: "capture.DatasetCheck", spec_path: Path) -> PreflightError:
    """Map a non-ok dataset check onto an actionable abort message."""
    hints = {
        "missing": f"run `pgbench-harness prepare --spec {spec_path}` first; "
                   "`run` never prepares silently.",
        "incomplete": "the benchmark tables are partially present or have an "
                      "unrecognized schema. Drop them (or use a dedicated database) "
                      "and run `prepare` again — the harness never overwrites tables "
                      "it cannot positively identify as its own.",
        "mismatch": "drop the benchmark tables (or point at a fresh database) and run "
                    "`prepare` again with the current spec — benchmarking a dataset "
                    "of the wrong size would silently break cross-run comparability.",
        "error": "check connectivity, credentials and permissions, then re-run preflight.",
    }
    return PreflightError(
        f"dataset check failed [{check.status}]: {check.detail}",
        hint=hints.get(check.status, ""),
    )


def cmd_preflight(spec_path: Path) -> int:
    """`preflight` subcommand."""
    spec = load_spec(spec_path)
    password = spec.password()
    get_redactor().register(password)
    logger = setup_logging()
    pf = capture.run_preflight(spec, password, logger)
    assert pf.dataset is not None
    if pf.dataset.status == "missing":
        logger.warning("dataset NOT loaded: %s", pf.dataset.detail)
        logger.warning("run `pgbench-harness prepare --spec %s` before `run`.", spec_path)
        return 1
    if not pf.dataset.ok:
        raise _dataset_error(pf.dataset, spec_path)
    logger.info("preflight OK")
    return 0


def _prepare_slug(spec: Spec) -> str:
    """Stable key for prepare artifacts: one dataset per (host, database)."""
    return re.sub(r"[^a-z0-9-]+", "-", f"{spec.target.host}-{spec.target.database}".lower())


def prepare_stats_path(spec: Spec, results_dir: Path) -> Path:
    """Where `prepare` records its load metrics for later report inclusion."""
    return results_dir / f"prepare_{_prepare_slug(spec)}.json"


def _write_prepare_stats(
    spec: Spec, password: str, results_dir: Path, wall_s: float,
    started: str, log_path: Path,
) -> dict:
    """Record data-load metrics (wall time, DB size, derived throughput)."""
    size = capture.database_size_bytes(spec, password)
    w = spec.workload
    units = (f"{w.tables * w.scale} warehouses" if w.type == "tpcc"
             else f"{w.tables * w.table_size:,} rows")
    stats = {
        "target_host": spec.target.host,
        "database": spec.target.database,
        "workload": dict(spec.raw.get("workload", {})),
        "started_utc": started,
        "finished_utc": utc_now_iso(),
        "wall_s": round(wall_s, 1),
        "db_size_bytes": size,
        "db_size_pretty": f"{size / 1024**3:.2f} GiB" if size else "n/a",
        "load_mb_s": round(size / 1024**2 / wall_s, 1) if size and wall_s > 0 else None,
        "loaded_units": units,
        "load_threads": min(16, max(spec.sweep.threads)),
        "log": str(log_path),
    }
    atomic_write_json(prepare_stats_path(spec, results_dir), stats)
    return stats


def cmd_prepare(spec_path: Path, results_dir: Path) -> int:
    """`prepare` subcommand: load the dataset idempotently, recording load metrics."""
    spec = load_spec(spec_path)
    password = spec.password()
    get_redactor().register(password)
    logger = setup_logging()
    check = capture.check_dataset(spec, password)
    if check.ok:
        logger.info("dataset already present and matches the spec (%s); nothing to do.",
                    check.detail)
        return 0
    if check.status != "missing":
        raise _dataset_error(check, spec_path)
    results_dir.mkdir(parents=True, exist_ok=True)
    cmd = sysbench.build_prepare_command(spec)
    log_path = results_dir / f"prepare_{_prepare_slug(spec)}.log"
    logger.info("preparing dataset: %s", cmd.display())
    started, t0 = utc_now_iso(), time.monotonic()
    rc = sysbench.run_streaming(cmd, sysbench.child_env(spec, password), log_path, logger)
    if rc != 0:
        raise RunError(
            f"sysbench prepare exited with code {rc} (full output in {log_path})",
            hint="inspect the log; common causes are credentials, sslmode and disk space.",
        )
    check = capture.check_dataset(spec, password)
    if not check.ok:
        raise RunError(f"prepare finished but the dataset check still fails: {check.detail}",
                       hint="inspect the prepare log; the load may have been cut short.")
    stats = _write_prepare_stats(spec, password, results_dir,
                                 time.monotonic() - t0, started, log_path)
    logger.info("dataset ready: %s", check.detail)
    logger.info("load metrics: %s in %s (%s, ~%s MB/s) — recorded in %s",
                stats["loaded_units"], fmt_duration(stats["wall_s"]),
                stats["db_size_pretty"], stats["load_mb_s"] or "?",
                prepare_stats_path(spec, results_dir).name)
    return 0


def _find_resume_dir(results_dir: Path, label: str) -> Path:
    """Latest run directory for this label (used by --resume without --run-dir)."""
    slug = make_run_id(label).rsplit("-", 1)[0]
    candidates = sorted(
        d for d in results_dir.glob(f"{slug}-*") if (d / "manifest.json").exists()
    )
    if not candidates:
        raise RunError(
            f"--resume: no previous run for label '{label}' under {results_dir}",
            hint="pass --run-dir explicitly, or start a fresh run without --resume.",
        )
    return candidates[-1]


def _init_run(
    spec: Spec, spec_path: Path, results_dir: Path, resume: bool, run_dir_opt: Optional[Path]
) -> tuple[Path, Manifest]:
    """Create (or reopen, for --resume) the run directory and manifest."""
    if resume:
        run_dir = run_dir_opt or _find_resume_dir(results_dir, spec.run.label)
        manifest = Manifest.load(run_dir)
        return run_dir, manifest
    run_id = make_run_id(spec.run.label)
    run_dir = results_dir / run_id
    n = 1
    while run_dir.exists():  # two runs started within the same second
        n += 1
        run_id = f"{make_run_id(spec.run.label)}-{n}"
        run_dir = results_dir / run_id
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        run_id=run_id, label=spec.run.label, edition=spec.run.edition,
        tshirt_size=spec.run.tshirt_size,
        levels=plan_levels(spec.sweep.threads, spec.sweep.repetitions),
    )
    dump_spec_copy(spec, run_dir / "spec.yaml")
    dump_spec_copy(spec, run_dir / "env" / "spec.yaml")
    manifest.save(run_dir)
    return run_dir, manifest


# Watchdog budget for a single sysbench `run` attempt, derived from duration_s:
# a healthy run emits a per-second interval line, so prolonged silence or a gross
# overrun of `--time` means the process is wedged and must be killed.
_WATCHDOG_STALL_S = 120          # no output line for this long -> stalled
_WATCHDOG_HARD_GRACE_S = 180     # allowed overrun past duration_s before a hard kill


def _watchdog_timeouts(spec: Spec) -> tuple[float, float]:
    """(stall_timeout_s, hard_timeout_s) for one sysbench `run` attempt."""
    return float(_WATCHDOG_STALL_S), float(spec.sweep.duration_s + _WATCHDOG_HARD_GRACE_S)


def _run_one_attempt(
    spec: Spec, password: str, run_dir: Path, raw_rel: str, lvl: Level,
    logger: logging.Logger,
) -> int:
    """Run sysbench once for *lvl*, returning its (or the watchdog's) exit code."""
    cmd = sysbench.build_run_command(spec, lvl.threads)
    logger.info("level %s (attempt %d): %s", lvl.key, lvl.attempts, cmd.display())
    stall_s, hard_s = _watchdog_timeouts(spec)
    return sysbench.run_streaming(
        cmd, sysbench.child_env(spec, password), run_dir / raw_rel, logger,
        stall_timeout_s=stall_s, hard_timeout_s=hard_s,
    )


def _level_error_excerpt(run_dir: Path, raw_rel: str, rc: int) -> str:
    """Verbatim error lines from the attempt log, or a generic exit-code message."""
    from pgbench_harness.parser import parse_log_file
    errs = parse_log_file(run_dir / raw_rel).error_lines
    return "\n".join(errs[:5]) or f"sysbench exited with code {rc}"


def _execute_level(
    spec: Spec, password: str, run_dir: Path, manifest: Manifest,
    lvl: Level, logger: logging.Logger,
) -> None:
    """Run one (rep, threads) level with retries: a failed attempt is preserved,
    the harness waits for the target to accept writes again, and re-runs — so a
    transient outage (e.g. a managed-database failover) is ridden out rather than
    recorded as a permanent failure. The level ends ``ok`` if any attempt
    succeeds, ``failed`` only after all attempts are exhausted.
    """
    raw_rel = f"raw/{lvl.key}.log"
    lvl.raw_log = raw_rel
    lvl.status = STATUS_RUNNING
    lvl.started_utc = utc_now_iso()
    lvl.attempts = 0
    manifest.save(run_dir)
    if spec.capture.bgwriter_stats:
        pre = capture.snapshot_bgwriter(spec, password)
    max_attempts = 1 + spec.sweep.retries
    rc = 1
    while lvl.attempts < max_attempts:
        lvl.attempts += 1
        rc = _run_one_attempt(spec, password, run_dir, raw_rel, lvl, logger)
        lvl.exit_code = rc
        if rc == 0:
            break
        excerpt = _level_error_excerpt(run_dir, raw_rel, rc)
        if lvl.attempts < max_attempts:
            # Preserve this failed attempt's log, then wait for the primary to
            # accept writes again before the next attempt (post-failover window).
            attempt_log = run_dir / "raw" / f"{lvl.key}.attempt{lvl.attempts}.log"
            shutil.copy(run_dir / raw_rel, attempt_log)
            logger.warning(
                "level %s attempt %d/%d FAILED (exit %d): %s — preserved as %s; retrying",
                lvl.key, lvl.attempts, max_attempts, rc,
                excerpt.splitlines()[0], attempt_log.name)
            writable = capture.wait_until_writable(
                spec, password, spec.sweep.retry_wait_s, logger)
            if not writable:
                logger.warning(
                    "level %s: target still not accepting writes after %ds; "
                    "retrying anyway", lvl.key, spec.sweep.retry_wait_s)
            manifest.save(run_dir)
    lvl.finished_utc = utc_now_iso()
    if spec.capture.bgwriter_stats:
        post = capture.snapshot_bgwriter(spec, password)
        atomic_write_text(
            run_dir / "raw" / f"{lvl.key}_bgwriter.json",
            f'{{"pre": {pre or "null"}, "post": {post or "null"}}}\n',
        )
    if rc == 0:
        lvl.status = STATUS_OK
        if lvl.attempts > 1:
            logger.info("level %s: OK (auto-recovered after %d attempts)",
                        lvl.key, lvl.attempts)
        else:
            logger.info("level %s: OK", lvl.key)
    else:
        lvl.status = STATUS_FAILED
        lvl.error_excerpt = _level_error_excerpt(run_dir, raw_rel, rc)
        logger.error(
            "level %s FAILED after %d attempt(s) (exit %d): %s — "
            "continuing with remaining levels",
            lvl.key, lvl.attempts, rc, lvl.error_excerpt.splitlines()[0])
    manifest.save(run_dir)


def cmd_run(
    spec_path: Path,
    results_dir: Path,
    resume: bool = False,
    run_dir_opt: Optional[Path] = None,
    dry_run: bool = False,
) -> int:
    """`run` subcommand: preflight, sweep, parse, report."""
    spec = load_spec(spec_path)
    if dry_run:
        print_dry_run(spec)
        return 0
    password = spec.password()
    get_redactor().register(password)
    run_dir, manifest = _init_run(spec, spec_path, results_dir, resume, run_dir_opt)
    logger = setup_logging(run_dir / "harness.log")
    logger.info("run %s -> %s (budget %s)", manifest.run_id, run_dir,
                fmt_duration(planned_budget_s(spec)))
    try:
        pf = capture.run_preflight(spec, password, logger)
        assert pf.dataset is not None
        if not pf.dataset.ok:
            raise _dataset_error(pf.dataset, spec_path)
    except PreflightError:
        manifest.status = "failed"  # keep `list` honest about aborted runs
        manifest.save(run_dir)
        raise
    manifest.preflight = _preflight_doc(pf)
    manifest.status = "running"
    manifest.save(run_dir)
    capture.capture_env(run_dir, spec, password, pf)
    _attach_prepare_stats(spec, results_dir, run_dir, logger)
    _sweep(spec, password, run_dir, manifest, logger)
    status = manifest.finalize_status()
    manifest.wall_time_s = _wall_time_s(manifest)
    manifest.save(run_dir)
    write_parsed(run_dir, spec, manifest)
    if spec.capture.pg_stat_statements != "false" and pf.pg_stat_statements:
        atomic_write_text(run_dir / "env" / "pg_stat_statements.json",
                          capture.snapshot_pg_stat_statements(spec, password) + "\n")
    report.generate_report(run_dir)
    logger.info("run %s finished with status '%s'; report: %s",
                manifest.run_id, status, run_dir / "report.html")
    return 0 if status == "complete" else 1


def _sweep(
    spec: Spec, password: str, run_dir: Path, manifest: Manifest, logger: logging.Logger
) -> None:
    """Execute all pending levels in order, with cooldowns in between."""
    pending = manifest.pending_levels()
    done = len(manifest.levels) - len(pending)
    if done:
        logger.info("resume: %d level(s) already completed, %d remaining", done, len(pending))
    for i, lvl in enumerate(pending):
        _execute_level(spec, password, run_dir, manifest, lvl, logger)
        if i < len(pending) - 1 and spec.sweep.cooldown_s > 0:
            logger.info("cooldown %ds ...", spec.sweep.cooldown_s)
            time.sleep(spec.sweep.cooldown_s)


def _attach_prepare_stats(
    spec: Spec, results_dir: Path, run_dir: Path, logger: logging.Logger
) -> None:
    """Copy the data-load metrics into env/ when they match this run's workload."""
    src = prepare_stats_path(spec, results_dir)
    if not src.exists():
        return
    try:
        stats = read_json(src)
    except ValueError:
        return
    if stats.get("workload") == dict(spec.raw.get("workload", {})):
        shutil.copy(src, run_dir / "env" / "prepare_stats.json")
        logger.info("attached data-load metrics from %s", src.name)
    else:
        logger.warning(
            "ignoring %s: it was recorded for a different workload configuration", src.name)


def _preflight_doc(pf: capture.PreflightResult) -> dict:
    doc = dataclasses.asdict(pf)
    return doc


def _wall_time_s(manifest: Manifest) -> float:
    from datetime import datetime

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        start = datetime.strptime(manifest.created_utc, fmt)
        end = datetime.strptime(manifest.finished_utc, fmt)
        return (end - start).total_seconds()
    except ValueError:
        return 0.0


def cmd_report(run_dir: Path) -> int:
    """`report` subcommand: regenerate report.html for an existing run."""
    setup_logging()
    out = report.generate_report(run_dir)
    get_logger().info("report written: %s", out)
    return 0
