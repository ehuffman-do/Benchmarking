"""Run orchestration: preflight wiring, prepare, the sweep loop, dry-run, resume."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from pgbench_harness import capture, report, report_soak, soak, sysbench
from pgbench_harness.errors import PreflightError, RunError
from pgbench_harness.manifest import (
    STATUS_FAILED, STATUS_OK, STATUS_RUNNING, Level, Manifest, plan_levels,
)
from pgbench_harness.spec import Spec, Sweep, dump_spec_copy, load_spec
from pgbench_harness.soak import TIMESERIES_COLUMNS as SOAK_TIMESERIES_COLUMNS
from pgbench_harness.summarize import IncrementalCsvWriter, SAMPLE_COLUMNS, write_parsed
from pgbench_harness.util import (
    atomic_write_json, atomic_write_text, fmt_duration, get_logger, get_redactor,
    make_run_id, read_json, setup_logging, utc_now_iso,
)


def planned_budget_s(spec: Spec) -> float:
    """Planned wall-clock budget: sum of durations plus inter-level cooldowns."""
    assert spec.sweep is not None
    n = len(spec.sweep.threads) * spec.sweep.repetitions
    return n * spec.sweep.duration_s + max(0, n - 1) * spec.sweep.cooldown_s


def print_dry_run(spec: Spec) -> None:
    """Print the exact sysbench command per level and the wall-clock budget."""
    assert spec.sweep is not None
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
        "wrong_schema": "the tables exist but not on this connection's search_path, so "
                        "sysbench's run would not see them. Set the search_path on the "
                        "target (e.g. `ALTER ROLE <user> IN DATABASE <db> SET "
                        "search_path = <schema>, public;`) so the schema above is first, "
                        "then re-run preflight. This commonly happens on poolers/clusters "
                        "where the default schema is not `public`.",
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


def cmd_validate(spec_path: Path) -> int:
    """`validate` subcommand: parse + validate a spec without connecting (CI lint)."""
    spec = load_spec(spec_path)  # raises SpecError (CLI prints message + hint, exit 2)
    mode = ("soak" if spec.is_soak else "suite" if spec.is_suite
            else "sweep" if spec.sweep else "device-probe")
    print(f"OK: {spec_path} is valid.")
    print(f"  label    : {spec.run.label}  ({spec.run.edition} / {spec.run.tshirt_size})")
    print(f"  target   : {spec.target.host}:{spec.target.port}/{spec.target.database} "
          f"(password via ${spec.target.password_env})")
    print(f"  workload : {spec.workload.type}")
    print(f"  mode     : {mode}")
    if spec.is_soak:
        assert spec.soak is not None
        print(f"  soak     : {spec.soak.threads} threads for {fmt_duration(spec.soak.duration_s)} "
              f"(events: operator-marked only)")
    elif spec.is_suite:
        assert spec.suite is not None
        n = len(spec.suite.workloads) + (2 if spec.suite.pgbench else 0)
        print(f"  suite    : {n} workloads x threads {list(spec.suite.threads)}, "
              f"{spec.suite.duration_s}s/cell")
    elif spec.sweep is not None:
        print(f"  sweep    : threads {list(spec.sweep.threads)}, {spec.sweep.duration_s}s/level, "
              f"{spec.sweep.repetitions} rep(s); budget {fmt_duration(planned_budget_s(spec))}")
    if spec.cluster:
        print(f"  cluster  : {spec.cluster.cr_kind}/{spec.cluster.cr_name} "
              f"ns {spec.cluster.namespace} (storage identity + device IOPS captured)")
    if spec.device_probe:
        armed = "ARMED" if spec.device_probe.allow_device_probe else \
            "disarmed (allow_device_probe: false)"
        print(f"  probe    : fileio {spec.device_probe.test_mode} "
              f"{spec.device_probe.file_total_size_gb}G x{spec.device_probe.threads}t "
              f"— {armed}")
    return 0


def cmd_doctor() -> int:
    """`doctor` subcommand: environment sanity — versions, git SHA/remote, tools."""
    import subprocess
    print(capture.harness_version())
    here = str(Path(__file__).resolve().parent)
    for label, args in (("git HEAD", ["git", "-C", here, "rev-parse", "--short", "HEAD"]),
                        ("git remote", ["git", "-C", here, "config", "--get", "remote.origin.url"])):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=10)
            print(f"  {label:11}: {out.stdout.strip() or '(unknown)'}")
        except (OSError, subprocess.SubprocessError):
            print(f"  {label:11}: (unavailable)")
    for label, args in (("sysbench", ["sysbench", "--version"]),
                        ("psql", ["psql", "--version"]),
                        ("pgbench", ["pgbench", "--version"])):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=15)
            print(f"  {label:11}: {(out.stdout or out.stderr).strip() or 'present'}")
        except (OSError, subprocess.SubprocessError):
            print(f"  {label:11}: NOT FOUND on PATH")
    return 0


def _maybe_prepare(spec_path: Path, results_dir: Path, do_prepare: bool,
                   logger: logging.Logger) -> None:
    """For run/soak --prepare: load the dataset first if it's missing (idempotent).

    Unlike a bare `prepare` (which now errors on an already-loaded dataset so the
    operator must opt into a recreate), the run/soak convenience flag is a no-op
    when the data is already present.
    """
    if not do_prepare:
        return
    spec = load_spec(spec_path)
    if capture.check_dataset(spec, spec.password()).ok:
        logger.info("--prepare: dataset already present; skipping load")
        return
    logger.info("--prepare: dataset missing — loading it before the run")
    cmd_prepare(spec_path, results_dir)


def cmd_preflight(spec_path: Path, json_output: bool = False) -> int:
    """`preflight` subcommand.

    With ``json_output`` (used by the web tier), emit one JSON event per check on
    stdout as it completes — a live checklist — and exit non-zero if any check
    failed. Default text behaviour (used by humans and run/soak) is unchanged.
    """
    spec = load_spec(spec_path)
    password = spec.password()
    get_redactor().register(password)
    logger = setup_logging()
    if json_output:
        import json as _json
        import sys as _sys
        worst = "ok"
        rank = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
        for event in capture.preflight_steps(spec, password, logger):
            print(_json.dumps(event), flush=True)
            if rank.get(event["status"], 0) > rank.get(worst, 0):
                worst = event["status"]
        print(_json.dumps({"name": "Preflight", "status": worst,
                           "detail": "all checks complete"}), flush=True)
        _sys.stdout.flush()
        return 0 if worst != "fail" else 1
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
        "load_threads": min(16, capture.peak_threads(spec)),
        "log": str(log_path),
    }
    atomic_write_json(prepare_stats_path(spec, results_dir), stats)
    return stats


def _log_tail(path: Path, lines: int = 15) -> str:
    """Last *lines* of a log file, indented, for inclusion in an error hint."""
    try:
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return f"  (could not read {path})"
    return "\n".join("    " + line for line in tail) or "  (log is empty)"


def cmd_prepare(spec_path: Path, results_dir: Path, recreate: str = "",
                create_db: bool = False, confirm: str = "") -> int:
    """`prepare` subcommand: load the dataset, with explicit, non-silent outcomes.

    Options (used by the web console; CLI flags too):
    * ``create_db``   — create the target database first if it does not exist.
    * ``recreate``    — "database" (drop+recreate the DB) or "tables" (drop just
                        the benchmark tables) before loading. Destructive, so it
                        requires ``confirm`` to equal the database name.
    Distinct, actionable errors instead of a silent no-op: already-present and
    missing-database are reported clearly.
    """
    spec = load_spec(spec_path)
    password = spec.password()
    get_redactor().register(password)
    logger = setup_logging()
    db = spec.target.database
    if recreate and recreate not in ("database", "tables"):
        raise RunError(f"unknown recreate mode '{recreate}'", hint="use 'database' or 'tables'.")
    if recreate and confirm != db:
        raise RunError(
            f"refusing to recreate: typed confirmation '{confirm}' does not match "
            f"database '{db}'", hint="type the exact database name to confirm.")

    maint = capture.maintenance_db(spec, password)   # for DB-level admin ops (may be None)
    exists = capture.database_exists(spec, password, maint) if maint else None

    if recreate == "database":
        if maint is None:
            raise RunError("cannot recreate the database: no maintenance database "
                           "(defaultdb/postgres) is reachable",
                           hint="check credentials/SSL; the user needs CREATEDB.")
        logger.info("recreate=database: dropping and recreating %s", db)
        ok, err = capture.drop_database(spec, password, maint)
        if not ok:
            raise RunError(f"DROP DATABASE {db} failed: {err}",
                           hint="ensure no other sessions hold it and the user owns it.")
        ok, err = capture.create_database(spec, password, maint)
        if not ok:
            raise RunError(f"CREATE DATABASE {db} failed: {err}")
        if not capture.wait_for_db(spec, password):
            raise RunError("database is not reachable after recreate",
                           hint="managed PG can lag just after a drop/create — retry prepare.")
    elif recreate == "tables":
        logger.info("recreate=tables: dropping benchmark tables in %s", db)
        ok, err = capture.drop_benchmark_tables(spec, password)
        if not ok:
            raise RunError(f"dropping benchmark tables failed: {err}")
    elif exists is False:
        assert maint is not None   # exists is False only when a maintenance DB resolved
        if create_db:
            logger.info("database %s does not exist; creating it", db)
            ok, err = capture.create_database(spec, password, maint)
            if not ok:
                raise RunError(f"CREATE DATABASE {db} failed: {err}")
            # Don't proceed to load against a cluster that isn't reachable yet:
            # a created DB can lag before it accepts connections on managed PG.
            if not capture.wait_for_db(spec, password):
                raise RunError(
                    f"database '{db}' was created but is not reachable yet",
                    hint="managed PG can lag just after a create — retry prepare.")
        else:
            raise RunError(
                f"database '{db}' does not exist on {spec.target.host}",
                hint="re-run prepare with 'create database' enabled to create it first.")
    elif maint is None and not capture.wait_for_db(spec, password, attempts=1):
        # No maintenance database (defaultdb/postgres) was reachable, so we could
        # neither verify nor create the target — AND the target itself does not
        # answer. Report this explicitly instead of falling through to check_dataset
        # and surfacing a generic connectivity error with no actionable cause.
        raise RunError(
            f"cannot reach a maintenance database to create '{db}', and '{db}' "
            f"itself is not reachable on {spec.target.host}",
            hint=("creating a database needs a reachable maintenance DB where the "
                  "user has CREATEDB; check credentials/SSL." if create_db
                  else "check credentials/SSL and that the database exists."))

    check = capture.check_dataset(spec, password)
    if check.ok and not recreate:
        raise RunError(
            f"dataset already present and matches the spec ({check.detail})",
            hint="re-run prepare with 'drop existing data first' (recreate) to reload it, "
                 "or just run the benchmark against the existing data.")
    if check.status not in ("missing", "ok") and not recreate:
        raise _dataset_error(check, spec_path)
    results_dir.mkdir(parents=True, exist_ok=True)
    cmd = sysbench.build_prepare_command(spec)
    log_path = results_dir / f"prepare_{_prepare_slug(spec)}.log"
    logger.info("preparing dataset: %s", cmd.display())
    started, t0 = utc_now_iso(), time.monotonic()
    rc = sysbench.run_streaming(cmd, sysbench.child_env(spec, password), log_path, logger)
    if rc != 0 and recreate:
        # absorb the well-known post-drop connection flakiness with one fresh retry
        logger.warning("prepare exited %d right after recreate; retrying once on a fresh "
                       "connection", rc)
        capture.wait_for_db(spec, password)
        rc = sysbench.run_streaming(cmd, sysbench.child_env(spec, password), log_path, logger)
    if rc != 0:
        raise RunError(
            f"sysbench prepare exited with code {rc} (full output in {log_path})",
            hint="inspect the log; common causes are credentials, sslmode and disk space.",
        )
    check = capture.check_dataset(spec, password)
    if check.status in ("wrong_schema", "mismatch", "incomplete"):
        # sysbench succeeded but the result is not usable as-is — give the
        # specific guidance rather than blaming the load.
        raise _dataset_error(check, spec_path)
    if not check.ok:
        raise RunError(
            f"sysbench prepare reported success but no benchmark tables exist "
            f"afterwards: {check.detail}",
            hint=f"inspect the prepare log for errors:\n{_log_tail(log_path)}",
        )
    stats = _write_prepare_stats(spec, password, results_dir,
                                 time.monotonic() - t0, started, log_path)
    logger.info("dataset ready: %s", check.detail)
    logger.info("load metrics: %s in %s (%s, ~%s MB/s) — recorded in %s",
                stats["loaded_units"], fmt_duration(stats["wall_s"]),
                stats["db_size_pretty"], stats["load_mb_s"] or "?",
                prepare_stats_path(spec, results_dir).name)
    return 0


def _find_resume_dir(results_dir: Path, label: str) -> Path:
    """Most-recent run directory for this label (used by --resume without --run-dir).

    Sorted by manifest mtime, not name: the ``-2`` same-second disambiguation
    suffix breaks lexicographic ordering (``...Z-10`` < ``...Z-2``).
    """
    slug = make_run_id(label).rsplit("-", 1)[0]
    candidates = [
        d for d in results_dir.glob(f"{slug}-*") if (d / "manifest.json").exists()
    ]
    if not candidates:
        raise RunError(
            f"--resume: no previous run for label '{label}' under {results_dir}",
            hint="pass --run-dir explicitly, or start a fresh run without --resume.",
        )
    return max(candidates, key=lambda d: (d / "manifest.json").stat().st_mtime)


def _init_run(
    spec: Spec, spec_path: Path, results_dir: Path, resume: bool, run_dir_opt: Optional[Path]
) -> tuple[Path, Manifest]:
    """Create (or reopen, for --resume) the run directory and manifest."""
    if resume:
        run_dir = run_dir_opt or _find_resume_dir(results_dir, spec.run.label)
        manifest = Manifest.load(run_dir)
        return run_dir, manifest
    assert spec.sweep is not None
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


def _live_sweep_callback(
    live: Optional[IncrementalCsvWriter], run_id: str, lvl: Level
) -> Optional[Callable[[str], None]]:
    """A per-line tap that appends each parsed interval to the live samples.csv
    (so the cockpit plots from second one). Mirrors summarize._samples_rows; the
    canonical file is rebuilt by write_parsed at finalize."""
    if live is None:
        return None
    from pgbench_harness.parser import parse_interval_line

    def _cb(line: str) -> None:
        s = parse_interval_line(line)
        if s is not None:
            live.append([run_id, lvl.rep, lvl.threads, s.t_offset, s.tps, s.qps,
                         s.r, s.w, s.o, s.lat_ms, s.err_s, s.reconn_s, lvl.seg])
    return _cb


DISK_ABORT_BYTES = 500 * 1024 * 1024
DISK_WARN_BYTES = 2 * 1024 * 1024 * 1024


def _disk_guard(run_dir: Path, logger: logging.Logger,
                warned: dict) -> None:
    """Free-space check between segments/levels: a long run on a filling
    volume must stop CLEANLY with a clear reason (results to date remain
    valid and reportable) rather than corrupt its artifacts at ENOSPC."""
    free = shutil.disk_usage(run_dir).free
    if free < DISK_ABORT_BYTES:
        raise RunError(
            f"results volume has only {free // 1048576} MB free — stopping the "
            "run before artifacts get corrupted",
            hint="free disk space (old runs, logs) and resume/re-run; results "
                 "written so far are intact and reportable.")
    if free < DISK_WARN_BYTES and not warned.get("disk"):
        warned["disk"] = True
        logger.warning("results volume is low on space (%d MB free) — the run "
                       "continues but will stop cleanly under %d MB",
                       free // 1048576, DISK_ABORT_BYTES // 1048576)


def _level_watchdog_s(duration_s: int) -> float:
    """Wall-clock ceiling for one benchmark cell: the configured duration plus
    a grace for connect/summary/histogram. A hung child (connected but silent)
    must never freeze an hours-long run — the watchdog kills it, the level is
    marked failed, and the sweep continues. Grace is env-tunable for tests."""
    import os as _os
    grace = float(_os.environ.get("PGB_LEVEL_WATCHDOG_GRACE_S", "0") or 0) \
        or max(120.0, duration_s * 0.5)
    return duration_s + grace


def _execute_level(
    spec: Spec, password: str, run_dir: Path, manifest: Manifest,
    lvl: Level, logger: logging.Logger, live: Optional[IncrementalCsvWriter] = None,
) -> None:
    """Run one (rep, threads) level: stats snapshots, sysbench, outcome bookkeeping."""
    raw_rel = f"raw/{lvl.key}.log"
    lvl.raw_log = raw_rel
    lvl.status = STATUS_RUNNING
    lvl.started_utc = utc_now_iso()
    manifest.save(run_dir)
    if spec.capture.bgwriter_stats:
        pre = capture.snapshot_bgwriter(spec, password)
    if spec.capture.io_stats:
        io_pre = capture.snapshot_io_stats(spec, password)
    cmd = sysbench.build_run_command(spec, lvl.threads)
    logger.info("level %s: %s", lvl.key, cmd.display())
    rc = sysbench.run_streaming(
        cmd, sysbench.child_env(spec, password), run_dir / raw_rel, logger,
        on_line=_live_sweep_callback(live, manifest.run_id, lvl),
        timeout_s=_level_watchdog_s(spec.sweep.duration_s))
    lvl.exit_code = rc
    lvl.finished_utc = utc_now_iso()
    if spec.capture.bgwriter_stats:
        post = capture.snapshot_bgwriter(spec, password)
        atomic_write_text(
            run_dir / "raw" / f"{lvl.key}_bgwriter.json",
            f'{{"pre": {pre or "null"}, "post": {post or "null"}}}\n',
        )
    if spec.capture.io_stats:
        atomic_write_json(
            run_dir / "raw" / f"{lvl.key}_iostats.json",
            {"pre": io_pre, "post": capture.snapshot_io_stats(spec, password)},
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
    prepare: bool = False,
) -> int:
    """`run` subcommand: preflight, sweep, parse, report."""
    spec = load_spec(spec_path)
    if spec.is_soak:
        raise RunError("this spec has a 'soak' section, not 'sweep'",
                       hint="use `pgbench-harness soak --spec ...` for resilience runs.")
    if dry_run:
        print_dry_run(spec)
        return 0
    password = spec.password()
    get_redactor().register(password)
    _maybe_prepare(spec_path, results_dir, prepare, setup_logging())
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
    sampler = (capture.LivePgSampler(spec, password, run_dir,
                                     spec.capture.live_pg_interval_s, logger)
               if spec.capture.live_pg else None)
    if sampler:
        sampler.start()
    dev_sampler = _start_observation(spec, run_dir, logger)
    # Live per-second series for the cockpit (incrementally appended during the
    # run); write_parsed rebuilds the canonical samples.csv atomically at finalize.
    live = IncrementalCsvWriter(run_dir / "parsed" / "samples.csv", SAMPLE_COLUMNS)
    try:
        _sweep(spec, password, run_dir, manifest, logger, live=live)
    except Exception:  # noqa: BLE001 — never leave the manifest at 'running' on an abort
        manifest.status = "failed"
        manifest.wall_time_s = _wall_time_s(manifest)
        manifest.save(run_dir)
        raise
    finally:
        live.close()
        if sampler:
            sampler.stop()
        if dev_sampler:
            dev_sampler.stop()
    status = manifest.finalize_status()
    manifest.wall_time_s = _wall_time_s(manifest)
    manifest.save(run_dir)
    write_parsed(run_dir, spec, manifest)
    _finish_evidence(run_dir, spec, logger)
    if spec.capture.pg_stat_monitor != "false" and pf.pg_stat_monitor:
        atomic_write_text(run_dir / "env" / "pg_stat_monitor.json",
                          capture.snapshot_pg_stat_monitor(spec, password) + "\n")
    report.generate_report(run_dir)
    logger.info("run %s finished with status '%s'; report: %s",
                manifest.run_id, status, run_dir / "report.html")
    # Exit code drives the worker's job state: complete->0 (done), partial->1
    # (done, a real result), failed/other->2 (failed). A genuine failure must
    # never read as a completed job in the Tasks tab.
    return {"complete": 0, "partial": 1}.get(status, 2)


def _iso_micros() -> str:
    """UTC now at microsecond precision — the shared soak clock."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _append_event(run_dir: Path, etype: str, label: str, note: str, source: str,
                  ts_utc: Optional[str] = None) -> dict:
    """Append one event marker to events.jsonl (the shared-clock event log)."""
    ev = {"ts_utc": ts_utc or _iso_micros(), "type": etype,
          "label": label, "note": note, "source": source}
    path = run_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:  # append-only; never holds a secret
        fh.write(json.dumps(ev) + "\n")
    return ev


def _soak_start_dt(run_dir: Path) -> Optional[datetime]:
    """The soak's t=0 anchor (manifest.soak.start_utc) as a datetime, or None."""
    try:
        doc = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    s = (doc.get("soak") or {}).get("start_utc") if isinstance(doc, dict) else None
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def cmd_mark(run_dir: Path, etype: str, label: str, note: str,
             at_s: Optional[float] = None) -> int:
    """`mark` subcommand: stamp a timeline event into a running/finished soak.

    ``at_s`` stamps the event at a specific offset (seconds from the soak start)
    instead of 'now' — used by the report's click-to-annotate. The offset is
    converted to the absolute shared-clock ts so the analysis places it correctly.
    """
    setup_logging()
    if not (run_dir / "manifest.json").exists():
        raise RunError(f"no manifest.json in {run_dir}", hint="pass the soak run directory.")
    ts_utc: Optional[str] = None
    if at_s is not None:
        if at_s < 0:
            raise RunError("cannot stamp an event before the run start (at_s < 0)")
        start = _soak_start_dt(run_dir)
        if start is None:
            raise RunError("cannot stamp at an offset: this run has no soak start time",
                           hint="offset stamping is only available for soak runs.")
        ts = start + timedelta(seconds=float(at_s))
        ts_utc = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    ev = _append_event(run_dir, etype, label, note, source="mark", ts_utc=ts_utc)
    get_logger().info("marked %s '%s' at %s in %s", etype, label, ev["ts_utc"], run_dir)
    return 0


def _live_soak_callback(
    live: IncrementalCsvWriter, base_dt: datetime, seen: set[int], seg_name: str,
) -> Callable[[str, str], None]:
    """Per-line tap that appends each parsed interval to the live soak timeseries,
    keyed on the read-time offset from soak start. Replicates build_timeline's
    first-seen-wins / non-negative-offset dedup so the live file matches the
    canonical one (soak._write_timeseries) at the finalize swap."""
    from pgbench_harness.parser import parse_interval_line

    def _cb(ts: str, line: str) -> None:
        s = parse_interval_line(line)
        if s is None:
            return
        try:
            ts_dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return
        off = int(round((ts_dt - base_dt).total_seconds()))
        if off < 0 or off in seen:
            return
        seen.add(off)
        live.append([off, ts, s.tps, s.qps, s.lat_ms, s.err_s, s.reconn_s, s.threads,
                     seg_name, s.r, s.w, s.o, s.lat_pct])
    return _cb


def _segment_error_excerpt(seg_log: Path, rc: int, n_intervals: int) -> str:
    """Best-effort human reason a soak segment failed, read from its raw log.

    Segment logs are ``<ISO>\\t<sysbench line>``; we strip the timestamp prefix
    and surface sysbench's own FATAL/ERROR lines (the same machinery
    ``_execute_level`` uses for sweeps). Falls back to the last few non-empty
    lines so connection / SSL / timeout messages that don't match the error
    regex are still visible. The log is already redacted at write time, so the
    excerpt can never contain the password.
    """
    from pgbench_harness.parser import ERROR_LINE_RE
    try:
        raw = seg_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"sysbench exited with code {rc} ({n_intervals} intervals); log unreadable"
    lines = [(ln.partition("\t")[2] or ln).rstrip() for ln in raw]
    errs = [ln for ln in lines if ln and ERROR_LINE_RE.search(ln)]
    if errs:
        return "\n".join(errs[:5])
    tail = [ln for ln in lines if ln][-5:]
    if tail:
        return "\n".join(tail)
    return f"sysbench exited with code {rc} with no output ({n_intervals} intervals)"


def _soak_supervisor(
    spec: Spec, password: str, run_dir: Path, manifest: Manifest, logger: logging.Logger,
    stop: Optional[dict] = None,
) -> dict:
    """Run sysbench at fixed concurrency for the full window, relaunching if it
    exits early so an outage can never truncate the test. Each segment's lines
    are timestamped at read time; gaps between segments are measured as downtime.

    Safety bounds (a run that can't produce samples must fail fast with a clear
    reason, never burn the window): every segment is wall-clock bounded
    (``run_streaming_timestamped`` SIGTERM/SIGKILLs a hung child); the loop never
    runs past ``duration_s + hard_ceiling_grace_s``; and ``fast_fail_segments``
    consecutive zero-sample launches abort immediately with the load generator's
    own error surfaced.
    """
    soak = spec.soak
    assert soak is not None
    env = sysbench.child_env(spec, password)
    start_mono = time.monotonic()
    start_utc = _iso_micros()
    deadline = start_mono + soak.duration_s
    hard_deadline = deadline + soak.hard_ceiling_grace_s
    segments: list[dict] = []
    seg = relaunches = total_intervals = 0
    consecutive_short = consecutive_zero_sample = 0
    prev_rate_idx = -1
    disk_warned: dict = {}
    last_excerpt = ""

    # Shared-clock anchor for the live per-second writer. Timeline events are NOT
    # pre-declared in the spec, and the harness does not auto-detect them: they
    # arrive ONLY via operator marks (the live cockpit / report stamping, or
    # `pgbench-harness mark`), appended to events.jsonl.
    base_dt = datetime.strptime(start_utc, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)

    # Persist the soak anchor (start_utc) to the manifest immediately — BEFORE the
    # first (possibly hours-long) segment — so live consumers can resolve t=0 from
    # second one: offset event-stamping from the report and the in-app timeline
    # markers both need start_utc on disk while the run is still in flight.
    manifest.soak = _soak_doc(manifest, start_utc, soak.duration_s, segments, relaunches)
    manifest.save(run_dir)

    # Live per-second series for the cockpit, appended as each interval arrives
    # (soak.analyze rebuilds the canonical file atomically at finalize).
    live = IncrementalCsvWriter(run_dir / "parsed" / "soak_timeseries.csv", SOAK_TIMESERIES_COLUMNS)
    seen_offsets: set[int] = set()
    try:
        while True:
            if stop is not None and stop.get("flag"):
                logger.warning("soak: stop requested (signal) — finalizing partial results.")
                break
            now = time.monotonic()
            if now >= hard_deadline:   # absolute backstop: never exceed duration_s + grace
                logger.error("soak: hard wall-clock ceiling reached (duration %ds + grace %ds); "
                             "stopping.", soak.duration_s, soak.hard_ceiling_grace_s)
                break
            remaining = int(round(deadline - now))
            if remaining < 1:
                break
            rate, seg_time = 0, remaining
            if soak.rate_steps:
                elapsed = soak.duration_s - remaining
                idx = min(elapsed // soak.step_duration_s, len(soak.rate_steps) - 1)
                rate = soak.rate_steps[idx]
                seg_time = max(1, min(remaining,
                                      (idx + 1) * soak.step_duration_s - elapsed))
            seg += 1
            _disk_guard(run_dir, logger, disk_warned)
            if soak.rate_steps and idx != prev_rate_idx:
                # planned step transition, not a crash — stamp it for the report
                _append_event(run_dir, "note",
                              f"rate step {idx + 1}/{len(soak.rate_steps)}: "
                              f"{rate or 'unthrottled'} tps offered",
                              f"sysbench --rate={rate}", "auto")
                prev_rate_idx = idx
            elif seg > 1:
                relaunches += 1
                _append_event(run_dir, "loadgen_restart", f"sysbench relaunch #{relaunches}",
                              "supervisor relaunched the load generator after early exit", "auto")
            cmd = sysbench.build_soak_command(spec, soak.threads, seg_time, rate=rate)
            seg_log = run_dir / "raw" / f"soak_seg{seg:02d}.log"
            seg_start = _iso_micros()
            seg_mono = time.monotonic()
            logger.info("soak segment %d (remaining %ds): %s", seg, remaining, cmd.display())
            # Bound the segment to its requested time plus a kill grace, so a child
            # that connects but hangs (no output, never exits) cannot block the loop
            # past the deadline — the watchdog SIGTERM/SIGKILLs it.
            seg_timeout = float(seg_time + soak.segment_kill_grace_s)
            rc, n_intervals, timed_out = sysbench.run_streaming_timestamped(
                cmd, env, seg_log, logger,
                timeout_s=seg_timeout, kill_grace_s=float(soak.segment_kill_grace_s),
                on_line=_live_soak_callback(live, base_dt, seen_offsets, seg_log.stem))
            seg_wall = time.monotonic() - seg_mono
            excerpt = ""
            if rc != 0 or n_intervals == 0:
                excerpt = _segment_error_excerpt(seg_log, rc, n_intervals)
                last_excerpt = excerpt
                head = excerpt.splitlines()[0] if excerpt else ""
                logger.error("soak segment %d FAILED (exit %d, %d intervals%s): %s",
                             seg, rc, n_intervals, ", timed out" if timed_out else "", head)
            segments.append({"seg": seg, "log": f"raw/{seg_log.name}", "started_utc": seg_start,
                             "finished_utc": _iso_micros(), "exit_code": rc,
                             "intervals": n_intervals, "timed_out": timed_out,
                             "error_excerpt": excerpt})
            manifest.soak = _soak_doc(manifest, start_utc, soak.duration_s, segments, relaunches)
            manifest.save(run_dir)
            total_intervals += n_intervals
            consecutive_zero_sample = consecutive_zero_sample + 1 if n_intervals == 0 else 0
            consecutive_short = consecutive_short + 1 if seg_wall < min(5, remaining) else 0
            if rc == 0 and time.monotonic() >= deadline - 1:
                break  # completed the window cleanly (or the final rate step)
            # The fast-fail / churn guards apply ONLY before the soak has produced
            # any samples (total_intervals == 0) — i.e. a load generator that is
            # broken from the start. Once it HAS produced samples, zero-sample or
            # short segments are an OUTAGE to ride through (the whole point of a
            # failover soak); the per-segment timeout + hard ceiling + deadline +
            # max_relaunches bound it without truncating the test.
            if total_intervals == 0 and consecutive_zero_sample >= soak.fast_fail_segments:
                raise RunError(
                    f"soak produced no samples in {consecutive_zero_sample} consecutive launches "
                    f"at {soak.threads} threads — the load generator cannot sustain load against "
                    f"the target.",
                    hint=("last sysbench error:\n  "
                          + ((last_excerpt or f"exit {rc}").replace("\n", "\n  "))
                          + "\nrun `preflight`; verify connectivity, credentials, the dataset, and "
                            "that the target accepts this concurrency. NOTE: preflight's idle-holder "
                            "ceiling probe passing does not guarantee tpcc's heavier per-thread init "
                            "succeeds at this thread count."))
            if total_intervals == 0 and consecutive_short >= 15:
                logger.error("soak: load generator exited almost immediately %d times in a "
                             "row without ever producing a sample; stopping.", consecutive_short)
                break
            if relaunches >= soak.max_relaunches:
                logger.error("soak: reached max_relaunches=%d; stopping early.", soak.max_relaunches)
                break
            if time.monotonic() < deadline:   # brief backoff so a hard outage isn't hot-looped
                time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    finally:
        live.close()

    doc = _soak_doc(manifest, start_utc, soak.duration_s, segments, relaunches)
    doc["finish_utc"] = _iso_micros()
    if last_excerpt:
        doc["failure_excerpt"] = last_excerpt
    return doc


def _soak_doc(manifest: Manifest, start_utc: str, duration_s: int,
              segments: list, relaunches: int) -> dict:
    return {"run_id": manifest.run_id, "start_utc": start_utc,
            "target_duration_s": duration_s, "segments": segments, "relaunches": relaunches}


def _finalize_soak(
    run_dir: Path, spec: Spec, manifest: Manifest, logger: logging.Logger, aborted: bool,
) -> str:
    """Persist a TERMINAL manifest status + (best-effort) report on every exit.

    Runs on both the normal-return and the abort (supervisor-raised) paths so the
    on-disk manifest is never left at 'running'. Analysis and report generation
    are best-effort: a failure in either must not undo the terminal status (that
    would re-introduce the 'stuck live forever' cockpit bug).
    """
    summary: Optional[dict] = None
    try:
        if manifest.soak:
            summary = soak.analyze(run_dir, spec, manifest.soak)
    except Exception as exc:  # noqa: BLE001
        logger.error("soak: analysis failed during finalize: %s", exc)
    if summary is not None:
        status = summary.get("status", "failed")
        if aborted and status == "complete":   # an aborted run is never 'complete'
            status = "failed"
        manifest.wall_time_s = float(summary.get("observed_seconds", 0))
    else:
        status = "failed"
    manifest.status = status
    manifest.finished_utc = (manifest.soak.get("finish_utc") if manifest.soak else "") or utc_now_iso()
    manifest.save(run_dir)
    try:
        out = report_soak.generate_soak_report(run_dir)
        cov = summary.get("coverage_pct", 0.0) if summary else 0.0
        logger.info("soak %s finished (status '%s', coverage %.1f%%); report: %s",
                    manifest.run_id, status, cov, out)
    except Exception as exc:  # noqa: BLE001
        logger.error("soak: report generation failed during finalize: %s", exc)
    return status


def cmd_soak(
    spec_path: Path, results_dir: Path, dry_run: bool = False, prepare: bool = False,
) -> int:
    """`soak` subcommand: fixed-concurrency resilience run + resilience report."""
    spec = load_spec(spec_path)
    if not spec.is_soak:
        raise RunError("this spec has no 'soak' section",
                       hint="add a soak: block (threads, duration_s) for resilience runs.")
    assert spec.soak is not None
    if dry_run:
        if spec.soak.rate_steps:
            print(f"# rate-stepped soak dry run for '{spec.run.label}' "
                  f"({len(spec.soak.rate_steps)} steps x {spec.soak.step_duration_s}s)")
            for i, r in enumerate(spec.soak.rate_steps, 1):
                cmd = sysbench.build_soak_command(spec, spec.soak.threads,
                                                  spec.soak.step_duration_s, rate=r)
                print(f"[step {i}, rate {r or 'unthrottled':>6}] {cmd.display()}")
        else:
            cmd = sysbench.build_soak_command(spec, spec.soak.threads, spec.soak.duration_s)
            print(f"# soak dry run for '{spec.run.label}'")
            print(cmd.display())
        print(f"# fixed concurrency {spec.soak.threads}, duration "
              f"{fmt_duration(spec.soak.duration_s)} (supervisor relaunches on early exit)")
        print("# events: marked only by an operator (the console Annotate button or "
              "`pgbench-harness mark`) — none are pre-declared or auto-detected")
        print(f"# password source: env var {spec.target.password_env} -> PGPASSWORD")
        return 0
    password = spec.password()
    get_redactor().register(password)
    _maybe_prepare(spec_path, results_dir, prepare, setup_logging())
    run_id = make_run_id(spec.run.label)
    run_dir = results_dir / run_id
    n = 1
    while run_dir.exists():
        n += 1
        run_id = f"{make_run_id(spec.run.label)}-{n}"
        run_dir = results_dir / run_id
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    dump_spec_copy(spec, run_dir / "spec.yaml")
    dump_spec_copy(spec, run_dir / "env" / "spec.yaml")
    manifest = Manifest(run_id=run_id, label=spec.run.label, edition=spec.run.edition,
                        tshirt_size=spec.run.tshirt_size, mode="soak")
    manifest.save(run_dir)
    logger = setup_logging(run_dir / "harness.log")
    logger.info("soak %s -> %s (duration %s, %d threads)", run_id, run_dir,
                fmt_duration(spec.soak.duration_s), spec.soak.threads)
    try:
        pf = capture.run_preflight(spec, password, logger)
        assert pf.dataset is not None
        if not pf.dataset.ok:
            raise _dataset_error(pf.dataset, spec_path)
    except PreflightError:
        manifest.status = "failed"
        manifest.save(run_dir)
        raise
    manifest.preflight = _preflight_doc(pf)
    manifest.status = "running"
    manifest.save(run_dir)
    capture.capture_env(run_dir, spec, password, pf)
    _attach_prepare_stats(spec, results_dir, run_dir, logger)
    logger.info("starting soak load; trigger events from the provider console and run "
                "`pgbench-harness mark --run-dir %s --type <failover|scale_up|...> "
                "--label '...'` at the moment you trigger them.", run_dir)
    if spec.soak.tolerate_errors:
        logger.warning("soak: tolerate_errors has no effect with the pgsql driver "
                       "(sysbench --ignore-errors is MySQL-only); on a hard error the load "
                       "generator exits and the supervisor relaunches it.")
    # Graceful stop: SIGINT/SIGTERM finalize a partial resilience report instead
    # of discarding the run. The signal also reaches the child sysbench, which
    # exits, unblocking the supervisor's read loop so it sees the flag.
    import signal
    stop = {"flag": False}

    def _on_signal(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    old_int = signal.signal(signal.SIGINT, _on_signal)
    old_term = signal.signal(signal.SIGTERM, _on_signal)
    sampler = (capture.LivePgSampler(spec, password, run_dir,
                                     spec.capture.live_pg_interval_s, logger)
               if spec.capture.live_pg else None)
    if sampler:
        sampler.start()
    dev_sampler = _start_observation(spec, run_dir, logger)
    supervisor_error: Optional[BaseException] = None
    try:
        manifest.soak = _soak_supervisor(spec, password, run_dir, manifest, logger, stop=stop)
    except Exception as exc:  # noqa: BLE001 — finalize a terminal status even on abort, then re-raise
        supervisor_error = exc
        logger.error("soak aborted: %s", exc)
    finally:
        if sampler:
            sampler.stop()
        if dev_sampler:
            dev_sampler.stop()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    # ALWAYS finalize: persist a terminal manifest status + (best-effort) report,
    # even when the supervisor aborted. Otherwise the run is stuck 'running' on
    # disk forever and the cockpit can never leave 'live' (see _progress / the
    # SSE 'done' event, which key off manifest.status).
    status = _finalize_soak(run_dir, spec, manifest, logger,
                            aborted=supervisor_error is not None)
    _finish_evidence(run_dir, spec, logger)
    if supervisor_error is not None:
        raise supervisor_error
    # Exit code drives the worker's job state: complete->0 (done), partial->1
    # (done, a real result), failed/other->2 (failed). A genuine failure must
    # never read as a completed job in the Tasks tab.
    return {"complete": 0, "partial": 1}.get(status, 2)


def _sweep(
    spec: Spec, password: str, run_dir: Path, manifest: Manifest, logger: logging.Logger,
    live: Optional[IncrementalCsvWriter] = None,
) -> None:
    """Execute all pending levels in order, with cooldowns in between."""
    assert spec.sweep is not None
    pending = manifest.pending_levels()
    done = len(manifest.levels) - len(pending)
    if done:
        logger.info("resume: %d level(s) already completed, %d remaining", done, len(pending))
    warned: dict = {}
    for i, lvl in enumerate(pending):
        _disk_guard(run_dir, logger, warned)
        _execute_level(spec, password, run_dir, manifest, lvl, logger, live=live)
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
    """Actual benchmarking time: the sum of per-level durations.

    Summing levels (rather than finished-minus-created) keeps the figure
    correct across ``--resume``, where a run may be picked up hours or days
    after it was created — the idle gap is not benchmarking time.
    """
    from datetime import datetime

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    total = 0.0
    for lvl in manifest.levels:
        if not lvl.started_utc or not lvl.finished_utc:
            continue
        try:
            start = datetime.strptime(lvl.started_utc, fmt)
            end = datetime.strptime(lvl.finished_utc, fmt)
        except ValueError:
            continue
        total += max(0.0, (end - start).total_seconds())
    return total


def cmd_report(run_dir: Path) -> int:
    """`report` subcommand: regenerate the report for an existing run (sweep or soak)."""
    setup_logging()
    mode = Manifest.load(run_dir).mode
    if mode in ("suite", "probe"):
        from pgbench_harness import report_evidence
        out = report_evidence.generate_evidence_report(run_dir)
    elif (run_dir / "parsed" / "soak_summary.json").exists() or mode == "soak":
        out = report_soak.generate_soak_report(run_dir)
    else:
        out = report.generate_report(run_dir)
    get_logger().info("report written: %s", out)
    return 0


# ── IOPS ceiling verification: observation, suite mode, device probe ──

def _start_observation(spec: Spec, run_dir: Path, logger: logging.Logger):
    """Cluster-aware evidence capture: storage identity + 1s device sampler.
    Pure-SQL runs (no cluster: section) skip this entirely; any failure is a
    recorded warning, never a run failure."""
    if spec.cluster is None:
        return None
    from pgbench_harness import deviceio
    try:
        ident = deviceio.capture_storage_identity(spec, run_dir, logger)
        logger.info("storage identity: %s", ident.get("finding")
                    or "; ".join(ident.get("warnings", [])) or "captured")
    except Exception as exc:  # noqa: BLE001
        logger.warning("storage identity capture failed: %s", exc)
    sampler = deviceio.DeviceIoSampler(spec, run_dir, logger)
    return sampler if sampler.start() else None


def _finish_evidence(run_dir: Path, spec: Spec, logger: logging.Logger) -> Optional[dict]:
    """Derive the device series, compute the verdict, write evidence.json,
    and print the finding — for every run kind."""
    from pgbench_harness import deviceio, evidence
    try:
        rows = deviceio.derive_device_series(run_dir)
        verdict = deviceio.compute_verdict(rows, spec.limits) if spec.cluster else None
        doc = evidence.build_evidence(run_dir, spec, verdict)
        if verdict:
            line = f"IOPS verdict: {verdict['finding'].upper()} — {verdict['detail']}"
            logger.info("%s", line)
            print(line, flush=True)
        return doc
    except Exception as exc:  # noqa: BLE001 — evidence must not undo a finished run
        logger.warning("evidence assembly degraded: %s", exc)
        return None


def _suite_segments(spec: Spec) -> list[Level]:
    assert spec.suite is not None
    levels: list[Level] = []
    for seg in spec.suite.workloads:
        for t in spec.suite.threads:
            levels.append(Level(rep=1, threads=t, seg=seg))
    if spec.suite.pgbench:
        for seg in ("pgbench_tpcb", "pgbench_select"):
            for t in spec.suite.threads:
                levels.append(Level(rep=1, threads=t, seg=seg))
    return levels


def _segment_spec(spec: Spec, seg: str) -> Spec:
    """Derived per-segment Spec: the segment's workload over the suite ladder,
    expressed as a one-level sweep so _execute_level runs unchanged."""
    assert spec.suite is not None
    wl = dataclasses.replace(spec.workload, type=seg,
                             dataset_gb=0.0, mix="",
                             rand_type=spec.workload.rand_type)
    sweep = Sweep(threads=spec.suite.threads, duration_s=spec.suite.duration_s,
                  warmup_s=spec.suite.warmup_s, cooldown_s=spec.suite.cooldown_s)
    return dataclasses.replace(spec, workload=wl, sweep=sweep, suite=None)


def _execute_pgbench_level(
    spec: Spec, password: str, run_dir: Path, manifest: Manifest,
    lvl: Level, logger: logging.Logger,
) -> None:
    """One pgbench cell (TPC-B or SELECT-only at N clients) — the independent
    second driver, same bookkeeping contract as _execute_level."""
    from pgbench_harness import pgbench_cmd
    assert spec.suite is not None
    raw_rel = f"raw/{lvl.key}.log"
    lvl.raw_log = raw_rel
    lvl.status = STATUS_RUNNING
    lvl.started_utc = utc_now_iso()
    manifest.save(run_dir)
    if spec.capture.io_stats:
        io_pre = capture.snapshot_io_stats(spec, password)
    cmd = pgbench_cmd.build_pgbench_run(
        spec, lvl.threads, spec.suite.duration_s,
        select_only=(lvl.seg == "pgbench_select"))
    logger.info("level %s: %s", lvl.key, cmd.display())
    rc = sysbench.run_streaming(cmd, sysbench.child_env(spec, password),
                                run_dir / raw_rel, logger,
                                timeout_s=_level_watchdog_s(spec.suite.duration_s))
    lvl.exit_code = rc
    lvl.finished_utc = utc_now_iso()
    if spec.capture.io_stats:
        atomic_write_json(run_dir / "raw" / f"{lvl.key}_iostats.json",
                          {"pre": io_pre,
                           "post": capture.snapshot_io_stats(spec, password)})
    if rc == 0:
        lvl.status = STATUS_OK
        logger.info("level %s: OK", lvl.key)
    else:
        lvl.status = STATUS_FAILED
        tail = (run_dir / raw_rel).read_text(errors="replace").splitlines()[-5:]
        lvl.error_excerpt = "\n".join(tail) or f"pgbench exited with code {rc}"
        logger.error("level %s FAILED (exit %d) — continuing", lvl.key, rc)
    manifest.save(run_dir)


def print_suite_dry_run(spec: Spec) -> None:
    from pgbench_harness import pgbench_cmd
    assert spec.suite is not None
    levels = _suite_segments(spec)
    print(f"# suite dry run for '{spec.run.label}': {len(levels)} cells, "
          f"{spec.suite.duration_s}s each + {spec.suite.cooldown_s}s stabilization")
    for lvl in levels:
        if lvl.seg.startswith("pgbench"):
            cmd = pgbench_cmd.build_pgbench_run(
                spec, lvl.threads, spec.suite.duration_s,
                select_only=(lvl.seg == "pgbench_select"))
        else:
            cmd = sysbench.build_run_command(_segment_spec(spec, lvl.seg), lvl.threads)
        print(f"[{lvl.seg:>18} c{lvl.threads:>3}] {cmd.display()}")
    if spec.suite.pgbench:
        print(f"# pgbench dataset: `pgbench -i -s {spec.suite.pgbench_scale}` "
              "runs once before the pgbench cells")
    n = len(levels)
    budget = n * spec.suite.duration_s + (n - 1) * spec.suite.cooldown_s
    print(f"# planned wall-clock budget: {fmt_duration(budget)}")
    print(f"# password source: env var {spec.target.password_env} -> PGPASSWORD")


def cmd_suite(spec_path: Path, results_dir: Path, dry_run: bool = False,
              prepare: bool = False) -> int:
    """`suite` subcommand: the storage team's full evidentiary matrix — four
    sysbench OLTP workloads + pgbench TPC-B/SELECT-only across the thread
    ladder — as sequential segments in one consolidated evidence bundle."""
    from pgbench_harness import pgbench_cmd, report_evidence
    spec = load_spec(spec_path)
    if not spec.is_suite:
        raise RunError("this spec has no 'suite' section",
                       hint="add a suite: block (duration_s, threads, workloads).")
    assert spec.suite is not None
    if dry_run:
        print_suite_dry_run(spec)
        return 0
    password = spec.password()
    get_redactor().register(password)
    _maybe_prepare(spec_path, results_dir, prepare, setup_logging())
    run_id = make_run_id(spec.run.label)
    run_dir = results_dir / run_id
    n = 1
    while run_dir.exists():
        n += 1
        run_id = f"{make_run_id(spec.run.label)}-{n}"
        run_dir = results_dir / run_id
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    dump_spec_copy(spec, run_dir / "spec.yaml")
    dump_spec_copy(spec, run_dir / "env" / "spec.yaml")
    manifest = Manifest(run_id=run_id, label=spec.run.label, edition=spec.run.edition,
                        tshirt_size=spec.run.tshirt_size, mode="suite",
                        levels=_suite_segments(spec))
    manifest.save(run_dir)
    logger = setup_logging(run_dir / "harness.log")
    logger.info("suite %s -> %s (%d cells)", run_id, run_dir, len(manifest.levels))
    try:
        pf = capture.run_preflight(spec, password, logger)
        assert pf.dataset is not None
        if not pf.dataset.ok:
            raise _dataset_error(pf.dataset, spec_path)
    except PreflightError:
        manifest.status = "failed"
        manifest.save(run_dir)
        raise
    manifest.preflight = _preflight_doc(pf)
    manifest.status = "running"
    manifest.save(run_dir)
    capture.capture_env(run_dir, spec, password, pf)
    _attach_prepare_stats(spec, results_dir, run_dir, logger)
    sampler = (capture.LivePgSampler(spec, password, run_dir,
                                     spec.capture.live_pg_interval_s, logger)
               if spec.capture.live_pg else None)
    if sampler:
        sampler.start()
    dev_sampler = _start_observation(spec, run_dir, logger)
    live = IncrementalCsvWriter(run_dir / "parsed" / "samples.csv", SAMPLE_COLUMNS)
    pgbench_ready = False
    try:
        pending = manifest.pending_levels()
        disk_warned: dict = {}
        for i, lvl in enumerate(pending):
            _disk_guard(run_dir, logger, disk_warned)
            _append_event(run_dir, "note", f"segment {lvl.seg} c{lvl.threads}",
                          "suite cell start", "auto")
            if lvl.seg.startswith("pgbench"):
                if not pgbench_ready:
                    init = pgbench_cmd.build_pgbench_init(spec, spec.suite.pgbench_scale)
                    logger.info("pgbench init: %s", init.display())
                    rc = sysbench.run_streaming(
                        init, sysbench.child_env(spec, password),
                        run_dir / "raw" / "pgbench_init.log", logger,
                        timeout_s=7200)
                    if rc != 0:
                        raise RunError(f"pgbench -i failed (exit {rc}) — see "
                                       "raw/pgbench_init.log")
                    pgbench_ready = True
                _execute_pgbench_level(spec, password, run_dir, manifest, lvl, logger)
            else:
                _execute_level(_segment_spec(spec, lvl.seg), password, run_dir,
                               manifest, lvl, logger, live=live)
            if i < len(pending) - 1 and spec.suite.cooldown_s > 0:
                logger.info("stabilization %ds ...", spec.suite.cooldown_s)
                time.sleep(spec.suite.cooldown_s)
    except Exception:  # noqa: BLE001
        manifest.status = "failed"
        manifest.wall_time_s = _wall_time_s(manifest)
        manifest.save(run_dir)
        raise
    finally:
        live.close()
        if sampler:
            sampler.stop()
        if dev_sampler:
            dev_sampler.stop()
    status = manifest.finalize_status()
    manifest.wall_time_s = _wall_time_s(manifest)
    manifest.finished_utc = utc_now_iso()
    manifest.save(run_dir)
    write_parsed(run_dir, spec, manifest)
    _finish_evidence(run_dir, spec, logger)
    report_evidence.generate_evidence_report(run_dir)
    logger.info("suite %s finished with status '%s'; report: %s",
                manifest.run_id, status, run_dir / "report.html")
    return {"complete": 0, "partial": 1}.get(status, 2)


def cmd_device_probe(spec_path: Path, results_dir: Path, dry_run: bool = False) -> int:
    """`device-probe` subcommand: sysbench fileio against the pgdata volume
    from a pod pinned to the primary's node (see deviceprobe.py guardrails)."""
    from pgbench_harness import deviceprobe
    spec = load_spec(spec_path)
    if spec.device_probe is None:
        raise RunError("this spec has no 'device_probe' section",
                       hint="add device_probe: {allow_device_probe: true, ...} "
                            "plus a cluster: section — TEST CLUSTERS ONLY.")
    return deviceprobe.run_device_probe(spec, results_dir, dry_run=dry_run)
