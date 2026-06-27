"""Job worker: claim queued runs and drive the harness CLI as a subprocess.

Design choices that satisfy "survives UI/web restart and disconnects":
* The worker is a *separate* systemd service from the web tier, so bouncing the
  web server never touches a running benchmark.
* It shells out to the existing ``pgbench-harness`` CLI (a first-class entry
  point) rather than reimplementing run logic — cancel = SIGTERM the child
  (the harness finalizes gracefully / is ``--resume``-able), and the filesystem
  ``results/`` stays the source of truth.
* The DB password is read from the encrypted secret store and injected into the
  child env as ``PGB_TARGET_PASSWORD`` at exec time — never written to the spec,
  DB, or any artifact.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

import yaml

from pgbench_harness.util import get_redactor
from pgbench_webapp import index, queries
from pgbench_webapp.config import Config, ensure_dirs, load_config
from pgbench_webapp.db import connect
from pgbench_webapp.secrets_store import SecretStore
from pgbench_webapp.util import utc_now_iso

POLL_SECONDS = 3


def job_password_ref(job_id: int) -> str:
    return f"job:{job_id}:password"


def _store(cfg: Config) -> SecretStore:
    return SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")


def _spec_path(cfg: Config, job_id: int) -> Path:
    d = cfg.data_dir / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"job_{job_id}.yaml"


def run_job(cfg: Config, conn: sqlite3.Connection, job: sqlite3.Row,
            store: Optional[SecretStore] = None) -> str:
    """Execute one claimed job to completion. Returns the final job state.

    Synchronous: used by the worker loop and by tests. The job must already be
    in 'running' state (claimed).
    """
    store = store or _store(cfg)
    ensure_dirs(cfg)
    spec_file = _spec_path(cfg, job["id"])
    spec_file.write_text(job["spec_yaml"], encoding="utf-8")  # never contains a secret

    env = dict(os.environ)
    pw = store.get(job_password_ref(job["id"]))
    if pw:
        env["PGB_TARGET_PASSWORD"] = pw
        get_redactor().register(pw)  # scrub from any harness output the worker reads

    before = {p.name for p in cfg.results_dir.iterdir()} if cfg.results_dir.exists() else set()
    argv = [cfg.harness_bin, job["kind"], "--spec", str(spec_file),
            "--results-dir", str(cfg.results_dir)]
    if job["resume_run_id"] and job["kind"] == "run":   # UI-driven resume of a sweep
        argv += ["--resume", "--run-dir", str(cfg.results_dir / job["resume_run_id"])]
    log_path = cfg.data_dir / "jobs" / f"job_{job['id']}.out"
    redact = get_redactor().redact
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        queries.update_job(conn, job["id"], pid=proc.pid)
        assert proc.stdout is not None
        for line in proc.stdout:
            logf.write(redact(line))
            logf.flush()
        rc = proc.wait()

    # Link the job to the results directory it created.
    after = {p.name for p in cfg.results_dir.iterdir()} if cfg.results_dir.exists() else set()
    new_dirs = sorted(after - before)
    run_id = new_dirs[-1] if new_dirs else None
    fresh = queries.get_job(conn, job["id"])
    canceling = fresh is not None and fresh["state"] == "canceling"
    if canceling:
        state = "canceled"
    elif rc in (0, 1):       # 1 = partial (failed levels) — still a real result
        state = "done"
    else:
        state = "failed"
    queries.update_job(conn, job["id"], state=state, run_id=run_id, exit_code=rc,
                       finished_utc=utc_now_iso(), pid=None,
                       error="" if state != "failed" else f"exit {rc}")
    if run_id:
        run_dir = cfg.results_dir / run_id
        row = index._run_row(run_dir)
        if row:
            row["source"] = "web"
            queries.upsert_run(conn, row)
    store.delete(job_password_ref(job["id"]))   # secret no longer needed
    _notify(cfg, conn, job["id"], state, run_id)
    return state


def _notify(cfg: Config, conn: sqlite3.Connection, job_id: int, state: str, run_id: Optional[str]) -> None:
    """Best-effort completion record + SMTP/Slack notification. Never raises."""
    try:
        queries.audit(conn, None, f"job_{state}", target=run_id or f"job:{job_id}",
                      detail="worker finished job")
    except Exception:  # noqa: BLE001
        pass
    try:
        from pgbench_webapp import notify as _n
        run = queries.get_run(conn, run_id) if run_id else None
        _n.notify(conn, _store(cfg), state=state, run_id=run_id,
                  label=(run["label"] if run else ""),
                  peak_qps=(run["peak_qps"] if run else None))
    except Exception:  # noqa: BLE001  (notifications must never break a run)
        pass


def reconcile_startup(cfg: Config, conn: sqlite3.Connection) -> None:
    """On worker start, mark orphaned 'running'/'canceling' jobs whose process is
    gone as interrupted, so the queue isn't wedged after a crash/restart."""
    for job in queries.list_jobs(conn, states=("running", "canceling")):
        pid = job["pid"]
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if not alive:
            queries.update_job(conn, job["id"], state="failed", pid=None,
                               finished_utc=utc_now_iso(),
                               error="interrupted (worker restart); resume from the run page")


def worker_loop(cfg: Optional[Config] = None) -> None:
    """Long-running poll loop (the `pgbench-worker` service)."""
    cfg = cfg or load_config()
    ensure_dirs(cfg)
    conn = connect(cfg.db_path)
    reconcile_startup(cfg, conn)
    store = _store(cfg)
    while True:
        max_conc = int(queries.get_setting(conn, "max_concurrency", "1") or "1")
        job = queries.claim_next_job(conn, max_conc)
        if job is None:
            time.sleep(POLL_SECONDS)
            continue
        try:
            run_job(cfg, conn, job, store)
        except Exception as exc:  # noqa: BLE001  (one bad job must not kill the worker)
            queries.update_job(conn, job["id"], state="failed", pid=None,
                               finished_utc=utc_now_iso(), error=str(exc)[:500])


def cancel_job_process(conn: sqlite3.Connection, job_id: int) -> bool:
    """Mark a running job canceling and SIGTERM its child (graceful stop)."""
    job = queries.get_job(conn, job_id)
    if job is None or job["state"] not in ("running", "queued", "canceling"):
        return False
    if job["state"] == "queued":
        queries.update_job(conn, job_id, state="canceled", finished_utc=utc_now_iso())
        return True
    queries.update_job(conn, job_id, state="canceling")
    if job["pid"]:
        try:
            os.kill(job["pid"], signal.SIGTERM)
        except OSError:
            return False
    return True
