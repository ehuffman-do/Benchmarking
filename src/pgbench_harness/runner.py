"""Run orchestration: preflight wiring, prepare, the sweep loop, dry-run, resume."""

from __future__ import annotations

import dataclasses
import logging
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
    atomic_write_text, fmt_duration, get_logger, get_redactor, make_run_id,
    setup_logging, utc_now_iso,
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


def cmd_preflight(spec_path: Path) -> int:
    """`preflight` subcommand."""
    spec = load_spec(spec_path)
    password = spec.password()
    get_redactor().register(password)
    logger = setup_logging()
    pf = capture.run_preflight(spec, password, logger)
    if not pf.dataset_ok:
        logger.warning("dataset NOT ready: %s", pf.dataset_detail)
        logger.warning("run `pgbench-harness prepare --spec %s` before `run`.", spec_path)
        return 1
    logger.info("preflight OK")
    return 0


def cmd_prepare(spec_path: Path) -> int:
    """`prepare` subcommand: load the dataset, idempotently."""
    spec = load_spec(spec_path)
    password = spec.password()
    get_redactor().register(password)
    logger = setup_logging()
    ok, detail = capture.check_dataset(spec, password)
    if ok:
        logger.info("dataset already present (%s); nothing to do.", detail)
        return 0
    cmd = sysbench.build_prepare_command(spec)
    logger.info("preparing dataset: %s", cmd.display())
    log_path = Path("prepare.log").resolve()
    rc = sysbench.run_streaming(cmd, sysbench.child_env(spec, password), log_path, logger)
    if rc != 0:
        raise RunError(
            f"sysbench prepare exited with code {rc} (full output in {log_path})",
            hint="inspect the log; common causes are credentials, sslmode and disk space.",
        )
    ok, detail = capture.check_dataset(spec, password)
    if not ok:
        raise RunError(f"prepare finished but dataset check still fails: {detail}")
    logger.info("dataset ready: %s", detail)
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


def _execute_level(
    spec: Spec, password: str, run_dir: Path, manifest: Manifest,
    lvl: Level, logger: logging.Logger,
) -> None:
    """Run one (rep, threads) level: stats snapshots, sysbench, outcome bookkeeping."""
    raw_rel = f"raw/{lvl.key}.log"
    lvl.raw_log = raw_rel
    lvl.status = STATUS_RUNNING
    lvl.started_utc = utc_now_iso()
    manifest.save(run_dir)
    if spec.capture.bgwriter_stats:
        pre = capture.snapshot_bgwriter(spec, password)
    cmd = sysbench.build_run_command(spec, lvl.threads)
    logger.info("level %s: %s", lvl.key, cmd.display())
    rc = sysbench.run_streaming(
        cmd, sysbench.child_env(spec, password), run_dir / raw_rel, logger)
    lvl.exit_code = rc
    lvl.finished_utc = utc_now_iso()
    if spec.capture.bgwriter_stats:
        post = capture.snapshot_bgwriter(spec, password)
        atomic_write_text(
            run_dir / "raw" / f"{lvl.key}_bgwriter.json",
            f'{{"pre": {pre or "null"}, "post": {post or "null"}}}\n',
        )
    if rc == 0:
        lvl.status = STATUS_OK
        logger.info("level %s: OK", lvl.key)
    else:
        from pgbench_harness.parser import parse_log_file
        errs = parse_log_file(run_dir / raw_rel).error_lines
        lvl.status = STATUS_FAILED
        lvl.error_excerpt = "\n".join(errs[:5]) or f"sysbench exited with code {rc}"
        logger.error("level %s FAILED (exit %d): %s — continuing with remaining levels",
                     lvl.key, rc, lvl.error_excerpt.splitlines()[0])
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
    pf = capture.run_preflight(spec, password, logger)
    if not pf.dataset_ok:
        raise PreflightError(
            f"dataset is not ready: {pf.dataset_detail}",
            hint=f"run `pgbench-harness prepare --spec {spec_path}` first; "
                 "`run` never prepares silently.",
        )
    manifest.preflight = _preflight_doc(pf)
    manifest.status = "running"
    manifest.save(run_dir)
    capture.capture_env(run_dir, spec, password, pf)
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
