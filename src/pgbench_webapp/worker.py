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

import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from pgbench_harness.util import get_redactor
from pgbench_webapp import index, ops_support, queries
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


def _run_dir_names(results_dir: Path) -> set[str]:
    """Names of actual run directories (those with a manifest.json).

    Filters out stray files like ``prepare_<slug>.json`` / ``.log`` that also
    live directly under ``results/`` — otherwise the new-dir set-diff could pick
    one of them up as a bogus run_id.
    """
    if not results_dir.exists():
        return set()
    return {p.name for p in results_dir.iterdir()
            if p.is_dir() and (p / "manifest.json").exists()}


def _parse_run_id(output: str, results_dir: Path) -> Optional[str]:
    """Extract the run id from the harness's own stdout (it logs ``... -> <run_dir>``).

    This is exact per-job, so it attributes runs correctly even when several jobs
    run concurrently (the global new-dir set-diff cannot). Returns None if not found.
    """
    m = re.search(re.escape(str(results_dir)) + r"/([A-Za-z0-9][A-Za-z0-9._-]*)", output)
    return m.group(1) if m else None


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
    # Prefer a saved target's persistent password (enables one-click re-run);
    # otherwise the per-job secret captured at submit time. Either way the secret
    # only ever reaches the child env — never the spec, DB, or any artifact.
    pw = None
    if job["target_id"]:
        tgt = queries.get_target(conn, job["target_id"])
        if tgt is not None:
            pw = store.get(tgt["password_ref"])
    if pw is None:
        pw = store.get(job_password_ref(job["id"]))
    if pw:
        env["PGB_TARGET_PASSWORD"] = pw
        get_redactor().register(pw)  # scrub from any harness output the worker reads

    # Cluster Ops jobs: inject KUBECONFIG (path or decrypted copy) the same way —
    # child env only, contents registered with the redactor either way.
    kube_tmp: Optional[Path] = None
    kind = job["kind"]
    if ops_support.is_ops_kind(kind):
        kt = queries.get_kube_target(conn, job["kube_target_id"]) \
            if job["kube_target_id"] else None
        if kt is None:
            queries.update_job(conn, job["id"], state="failed", pid=None,
                               finished_utc=utc_now_iso(),
                               error="kube target no longer exists")
            store.delete(job_password_ref(job["id"]))
            return "failed"
        kube_tmp = ops_support.prepare_env(cfg, store, env, kt, job["id"])
    elif kind in ("run", "soak", "suite", "device_probe") and job["kube_target_id"]:
        # cluster-aware benchmark: inject KUBECONFIG so storage identity and
        # the device IOPS series can be captured. A vanished target degrades
        # the evidence (recorded by the runner) except for device_probe, which
        # is meaningless without the cluster.
        kt = queries.get_kube_target(conn, job["kube_target_id"])
        if kt is not None:
            kube_tmp = ops_support.prepare_env(cfg, store, env, kt, job["id"])
        elif kind == "device_probe":
            queries.update_job(conn, job["id"], state="failed", pid=None,
                               finished_utc=utc_now_iso(),
                               error="kube target no longer exists")
            store.delete(job_password_ref(job["id"]))
            return "failed"

    before = _run_dir_names(cfg.results_dir)
    if ops_support.is_ops_kind(kind):
        argv = ops_support.build_argv(cfg, kind, spec_file)
    elif kind == "doctor":                     # environment health; no spec/password
        argv = [cfg.harness_bin, "doctor"]
    elif kind == "preflight":                  # live checklist (structured JSON events)
        argv = [cfg.harness_bin, "preflight", "--spec", str(spec_file), "--json"]
    elif kind == "prepare":                    # dataset load (no run dir produced)
        argv = [cfg.harness_bin, "prepare", "--spec", str(spec_file),
                "--results-dir", str(cfg.results_dir)]
        opts = {}
        if job["options"]:
            try:
                opts = json.loads(job["options"])
            except (ValueError, TypeError):
                opts = {}
        if opts.get("create_db"):
            argv.append("--create-db")
        if opts.get("recreate") in ("database", "tables"):
            argv += ["--recreate", opts["recreate"], "--confirm", str(opts.get("confirm", ""))]
    elif kind == "device_probe":
        argv = [cfg.harness_bin, "device-probe", "--spec", str(spec_file),
                "--results-dir", str(cfg.results_dir)]
    else:                                       # run | soak | suite
        argv = [cfg.harness_bin, kind, "--spec", str(spec_file),
                "--results-dir", str(cfg.results_dir)]
        if job["resume_run_id"] and kind == "run":   # UI-driven resume of a sweep
            argv += ["--resume", "--run-dir", str(cfg.results_dir / job["resume_run_id"])]
    log_path = cfg.data_dir / "jobs" / f"job_{job['id']}.out"
    redact = get_redactor().redact
    head: list[str] = []
    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            # start_new_session=True: the harness becomes its own session/group
            # leader (pgid == pid), so a Stop can signal the WHOLE tree (harness +
            # sysbench grandchild) with one killpg instead of orphaning sysbench.
            proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    start_new_session=True)
            queries.update_job(conn, job["id"], pid=proc.pid)
            assert proc.stdout is not None
            early_run_id: Optional[str] = None
            for line in proc.stdout:
                red = redact(line)
                logf.write(red)
                logf.flush()
                if len(head) < 400:        # enough to capture the early "run -> <dir>" line
                    head.append(red)
                # Link the run to the job the instant the harness prints its run dir
                # (the very first lines), so the UI can open the LIVE cockpit while
                # the run is still going — not only after it finishes.
                if early_run_id is None and kind in ops_support.RUN_DIR_KINDS:
                    rid = ops_support.parse_op_run_id(red, cfg.results_dir)
                    if rid:
                        early_run_id = rid
                        queries.update_job(conn, job["id"], run_id=rid)
                        ops_support.index_ops_run(cfg, conn, rid, job)
                if early_run_id is None and kind in ("run", "soak", "suite", "device_probe"):
                    rid = _parse_run_id(red, cfg.results_dir)
                    if rid:
                        early_run_id = rid
                        queries.update_job(conn, job["id"], run_id=rid)
                        # Index the run now (its manifest already exists) so it also
                        # shows up in the Runs list live, not only the cockpit link.
                        # The just-written manifest may still say "created"; force a
                        # non-terminal status to "running" so the list shows it live
                        # instead of stuck at "created" until the job finishes.
                        row = index._run_row(cfg.results_dir / rid)
                        if row:
                            row["source"] = "web"
                            if row.get("status", "") not in index.TERMINAL_RUN:
                                row["status"] = "running"
                            queries.upsert_run(conn, row)
            rc = proc.wait()

        # Only run/soak (and run-dir ops kinds) produce a run directory. Prefer
        # the id the harness printed (exact per-job, concurrency-safe); fall back
        # to the new manifest-bearing dir, then to the resume dir.
        # preflight/prepare/doctor/ops_validate/ops_discover never set a run_id.
        run_id: Optional[str] = None
        if kind in ("run", "soak", "suite", "device_probe"):
            run_id = _parse_run_id("".join(head), cfg.results_dir)
            if run_id is None:
                new_dirs = sorted(_run_dir_names(cfg.results_dir) - before)
                run_id = new_dirs[-1] if new_dirs else (job["resume_run_id"] or None)
        elif kind in ops_support.RUN_DIR_KINDS:
            run_id = ops_support.parse_op_run_id("".join(head), cfg.results_dir)
        fresh = queries.get_job(conn, job["id"])
        canceling = fresh is not None and fresh["state"] == "canceling"
        if canceling or rc < 0:   # rc < 0 => killed by a signal (our cancel SIGTERM)
            state = "canceled"
        elif rc in (0, 1):        # 1 = partial (failed levels) — still a real result
            state = "done"
        else:
            state = "failed"
        fields: dict[str, Any] = {"state": state, "exit_code": rc, "pid": None,
                                  "finished_utc": utc_now_iso(),
                                  "error": "" if state != "failed" else f"exit {rc}"}
        if run_id:               # never clobber an existing link with NULL
            fields["run_id"] = run_id
        queries.update_job(conn, job["id"], **fields)
        if ops_support.is_ops_kind(kind):
            # Ops jobs index into ops_runs (never the benchmark runs table) and
            # cache validate/discover/schedule results onto the kube target.
            ops_support.postprocess(cfg, conn, job, state, run_id, log_path)
        elif run_id:
            # A terminal job must drive its run to a consistent terminal status —
            # even when the harness was killed before it could finalize (e.g. a
            # SIGKILLed stop) — so the run is never re-indexed as 'running' and the
            # cockpit can leave 'live'. The manifest stays source of truth; this
            # only overrides a stuck non-terminal one.
            index.converge_run_status(cfg.results_dir, run_id, state)
            row = index._run_row(cfg.results_dir / run_id)
            if row:
                row["source"] = "web"
                queries.upsert_run(conn, row)
        _notify(cfg, conn, job, state, run_id)
        return state
    finally:
        store.delete(job_password_ref(job["id"]))   # secret gone even on error
        if kube_tmp is not None:
            try:
                kube_tmp.unlink()                   # decrypted kubeconfig copy gone too
            except OSError:
                pass


def _notify(cfg: Config, conn: sqlite3.Connection, job: sqlite3.Row, state: str,
            run_id: Optional[str]) -> None:
    """Best-effort completion record + SMTP/Slack notification. Never raises."""
    try:
        queries.audit(conn, None, f"job_{state}", target=run_id or f"job:{job['id']}",
                      detail="worker finished job")
    except Exception:  # noqa: BLE001
        pass
    if job["kind"] not in ("run", "soak", "suite", "device_probe"):
        return   # don't email/Slack for preflight/prepare/doctor health checks
    try:
        from pgbench_webapp import notify as _n
        run = queries.get_run(conn, run_id) if run_id else None
        # Report the run's indexed status ("complete"/"partial"/...), not the queue
        # job state ("done"), so a successful run doesn't alert as "done". The run
        # row was just converged/upserted before this call, so its status is current.
        notify_state = run["status"] if run and run["status"] else state
        _n.notify(conn, _store(cfg), state=notify_state, run_id=run_id,
                  label=(run["label"] if run else ""),
                  peak_qps=(run["peak_qps"] if run else None))
    except Exception:  # noqa: BLE001  (notifications must never break a run)
        pass


def reconcile_startup(cfg: Config, conn: sqlite3.Connection) -> None:
    """On worker start, mark orphaned 'running'/'canceling' jobs whose process is
    gone as interrupted, so the queue isn't wedged after a crash/restart, then
    converge any run left non-terminal on disk whose owning job has ended."""
    # Cluster Ops jobs are converged by ops_support (which also drives their
    # results/ops meta.json + index terminal). Skip them here so the generic
    # loop can't mark an ops job 'failed' BEFORE that runs — which would leave
    # the ops reconcile with nothing to converge and the run stuck 'running'.
    for job in queries.list_jobs(conn, states=("running", "canceling")):
        if ops_support.is_ops_kind(job["kind"]):
            continue
        pid = job["pid"]
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if not alive:
            # 'canceling' with a dead pid was a stop in flight -> canceled; a
            # 'running' orphan crashed -> failed (and is resumable for sweeps).
            terminal = "canceled" if job["state"] == "canceling" else "failed"
            queries.update_job(conn, job["id"], state=terminal, pid=None,
                               finished_utc=utc_now_iso(),
                               error=("stopped (worker restart)" if terminal == "canceled"
                                      else "interrupted (worker restart); resume from the run page"))
        elif job["state"] == "running" and job["kind"] in (
                "run", "soak", "suite", "device_probe"):
            # The harness child SURVIVED the worker restart (KillMode=process;
            # it is its own process group). Re-attach: watch the pid and
            # converge the job + run when it finishes — a deploy mid-way
            # through a week-long benchmark must not lose it.
            threading.Thread(target=_reattach_orphan,
                             args=(cfg, job["id"], int(pid)),
                             name=f"reattach-{job['id']}", daemon=True).start()
    # Converge stuck-running runs against their now-terminal jobs and re-index
    # the filesystem so no run survives a restart still showing 'live'.
    index.reconcile(conn, cfg.results_dir)
    # Same for Cluster Ops jobs/runs, which index.reconcile does not cover.
    ops_support.reconcile_stale_ops_jobs(cfg, conn, startup=True)


def _reattach_orphan(cfg: Config, job_id: int, pid: int,
                     poll_s: float = 5.0) -> None:
    """Adopted child from before a worker restart: poll the pid (we cannot
    wait() a non-child) and converge the job row + run index from the run
    directory's manifest when it exits. The run dir stays the source of
    truth, so nothing is lost even if THIS worker restarts again."""
    conn = connect(cfg.db_path)
    try:
        queries.audit(conn, None, "job_reattach", target=f"job:{job_id}",
                      detail=f"pid={pid} survived a worker restart")
        while True:
            try:
                os.kill(pid, 0)
            except OSError:
                break                            # child finished (or died)
            time.sleep(poll_s)
        job = queries.get_job(conn, job_id)
        if job is None or job["state"] not in ("running", "canceling"):
            return                               # someone else converged it
        status = ""
        if job["run_id"]:
            try:
                manifest = json.loads(
                    (cfg.results_dir / job["run_id"] / "manifest.json")
                    .read_text(encoding="utf-8"))
                status = manifest.get("status", "")
            except (OSError, ValueError):
                status = ""
        state = "done" if status in ("complete", "partial") else             ("canceled" if job["state"] == "canceling" else "failed")
        queries.update_job(conn, job_id, state=state, pid=None,
                           finished_utc=utc_now_iso(),
                           error="" if state == "done" else
                           f"run ended with status '{status or 'unknown'}' "
                           "(converged after worker restart)")
        index.reconcile(conn, cfg.results_dir)
        queries.audit(conn, None, f"job_{state}", target=job["run_id"] or f"job:{job_id}",
                      detail="converged by re-attach after worker restart")
    except Exception:  # noqa: BLE001 — a re-attach failure must not kill the worker
        pass
    finally:
        conn.close()


def _run_job_threaded(cfg: Config, store: SecretStore, job_id: int) -> None:
    """Execute one job on its own DB connection (used for concurrent runs)."""
    conn = connect(cfg.db_path)
    try:
        job = queries.get_job(conn, job_id)
        if job is not None:
            run_job(cfg, conn, job, store)
    except Exception as exc:  # noqa: BLE001  (one bad job must not kill the worker)
        try:
            queries.update_job(conn, job_id, state="failed", pid=None,
                               finished_utc=utc_now_iso(), error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def worker_loop(cfg: Optional[Config] = None) -> None:
    """Long-running poll loop (the ``pgbench-worker`` service).

    Honors the admin-set ``max_concurrency``: up to N jobs run at once, each in
    its own thread with its own SQLite connection. Only this loop claims jobs
    (single claimer → no claim race); ``claim_next_job`` still gates on
    ``running_count`` so the limit holds even if the setting changes mid-flight.
    """
    cfg = cfg or load_config()
    ensure_dirs(cfg)
    conn = connect(cfg.db_path)
    reconcile_startup(cfg, conn)
    store = _store(cfg)
    # value = (thread, kind); the kind lets the monitor lane run WITHOUT
    # occupying a benchmark/ops concurrency slot. Gating the loop on
    # len(active) alone would let one long-lived monitor wedge the queue —
    # claim_next_job's running_count already excludes monitors, so the outer
    # gate must exclude them too, or the exclusion is a no-op.
    active: dict[int, tuple[threading.Thread, str]] = {}
    # Absolute backstop so a fleet of per-target monitors can't spawn unbounded
    # threads even though each is admissible on its own lane.
    monitor_cap = 32
    last_auto_health = 0.0
    while True:
        for jid in [j for j, (t, _k) in active.items() if not t.is_alive()]:
            active.pop(jid)[0].join()
        # Continuous intelligence: enqueue due auto-health checks (cheap scan,
        # throttled — the per-target interval itself gates actual enqueues).
        if time.monotonic() - last_auto_health > 30:
            last_auto_health = time.monotonic()
            try:
                ops_support.maybe_enqueue_auto_health(cfg, conn)
            except Exception:  # noqa: BLE001 — scheduling must never kill the loop
                pass
        max_conc = max(1, int(queries.get_setting(conn, "max_concurrency", "1") or "1"))
        slotted = sum(1 for _t, k in active.values() if k != "ops_monitor")
        monitors = len(active) - slotted
        if slotted >= max_conc or monitors >= monitor_cap:
            time.sleep(POLL_SECONDS)
            continue
        job = queries.claim_next_job(conn, max_conc)
        if job is None:
            time.sleep(POLL_SECONDS)
            continue
        t = threading.Thread(target=_run_job_threaded, args=(cfg, store, job["id"]),
                             name=f"job-{job['id']}", daemon=True)
        active[job["id"]] = (t, job["kind"])
        t.start()


def _stop_grace_s(conn: sqlite3.Connection) -> float:
    """Seconds to wait after SIGTERM before escalating to SIGKILL (admin-tunable)."""
    try:
        return max(0.0, float(queries.get_setting(conn, "stop_grace_s", "15") or "15"))
    except (ValueError, TypeError):
        return 15.0


def _escalate_kill(cfg: Config, job_id: int, pid: int, pgid: int, grace_s: float) -> None:
    """After the graceful grace, SIGKILL the process group if the job is still
    canceling and the leader is alive. The worker's run_job records the terminal
    job state and converges the run status once the child actually dies, so the
    cockpit leaves 'live' even on a hard kill."""
    time.sleep(grace_s)
    conn = connect(cfg.db_path)
    try:
        job = queries.get_job(conn, job_id)
        if job is None or job["state"] != "canceling":
            return                               # exited gracefully within the grace
        try:
            os.kill(pid, 0)                      # leader still alive?
        except OSError:
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    finally:
        conn.close()


def stop_job_process(cfg: Config, conn: sqlite3.Connection, job_id: int) -> bool:
    """Stop a job gracefully, escalating to SIGKILL after a grace period.

    queued -> canceled immediately. running/canceling -> mark canceling and
    SIGTERM the child's process GROUP (so the sysbench grandchild is reaped too,
    not just the harness); a daemon watcher SIGKILLs the group if it has not
    exited within stop_grace_s. SIGKILL cannot be caught, so the terminal
    manifest/run status is written by run_job / reconcile when the child dies —
    never relied on from a graceful handler here.
    """
    job = queries.get_job(conn, job_id)
    if job is None or job["state"] not in ("running", "queued", "canceling"):
        return False
    if job["state"] == "queued":
        queries.update_job(conn, job_id, state="canceled", finished_utc=utc_now_iso())
        return True
    queries.update_job(conn, job_id, state="canceling")
    pid = job["pid"]
    if not pid:
        return True                              # nothing to signal; reconcile finalizes
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return True                              # already gone
    try:
        os.killpg(pgid, signal.SIGTERM)          # graceful: reaches harness + sysbench
    except OSError:
        return True
    threading.Thread(target=_escalate_kill,
                     args=(cfg, job_id, pid, pgid, _stop_grace_s(conn)),
                     daemon=True).start()
    return True
