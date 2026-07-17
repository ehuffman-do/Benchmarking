"""Worker-side plumbing for Cluster Ops jobs.

Keeps ``worker.py`` small: this module knows how to build the ``ops`` argv,
inject KUBECONFIG into the child environment (path-reference or decrypted
copy — contents registered with the redactor either way, so they can never
reach the job log), and index finished op runs into SQLite from the
``results/ops/`` filesystem (which stays the source of truth).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.ops.oprun import TERMINAL as OPS_TERMINAL
from pgbench_harness.ops.oprun import read_meta
from pgbench_harness.util import atomic_write_json, get_redactor
from pgbench_webapp import queries
from pgbench_webapp.config import Config
from pgbench_webapp.secrets_store import SecretStore
from pgbench_webapp.util import utc_now_iso

# job kind -> ops CLI subcommand. Kinds with a run dir get --results-dir.
OPS_KINDS: dict[str, str] = {
    "ops_validate": "validate",
    "ops_discover": "discover",
    "ops_cr_apply": "cr-apply",
    "ops_backup": "backup",
    "ops_scenario": "scenario",
    "ops_monitor": "monitor",
    "ops_pg_params": "pg-params",
    "ops_diag": "diag",
    "ops_health": "health",
    "ops_operate": "operate",
    "ops_pmm_enable": "pmm-enable",
    "ops_pmm_status": "pmm-status",
    "ops_pmm_disable": "pmm-disable",
}
RUN_DIR_KINDS = ("ops_cr_apply", "ops_backup", "ops_scenario", "ops_monitor",
                 "ops_diag", "ops_operate", "ops_pmm_enable", "ops_pmm_status",
                 "ops_pmm_disable")

SUMMARY_MARKER = "OPS_SUMMARY_JSON"
TOPOLOGY_MARKER = "OPS_TOPOLOGY_JSON"
SCHEDULES_MARKER = "OPS_SCHEDULES_JSON"
PARAMS_MARKER = "OPS_PARAMS_JSON"
HEALTH_MARKER = "OPS_HEALTH_JSON"


def is_ops_kind(kind: str) -> bool:
    return kind in OPS_KINDS


def build_argv(cfg: Config, kind: str, spec_file: Path) -> list[str]:
    argv = [cfg.harness_bin, "ops", OPS_KINDS[kind], "--ops-spec", str(spec_file)]
    if kind in RUN_DIR_KINDS:
        argv += ["--results-dir", str(cfg.results_dir)]
    return argv


def kubeconfigs_dir(cfg: Config) -> Path:
    d = cfg.data_dir / "kubeconfigs"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def kubeconfig_ref(name: str) -> str:
    return f"kube:{name}:kubeconfig"


_KUBECONFIG_SECRET_KEYS = re.compile(
    r"^\s*(certificate-authority-data|client-certificate-data|client-key-data|"
    r"token|password|refresh-token|access-token|id-token|client-secret)\s*:\s*(.+)$")


def _register_kubeconfig_secrets(content: str) -> None:
    """Register the credential-bearing kubeconfig values with the redactor.

    A kubeconfig is multi-line, so registering the whole string would only
    catch verbatim full-document echoes; per-value registration catches the
    dangerous parts (certificate data, tokens, passwords) wherever they
    surface. Non-secret values (server URL, context names) stay readable —
    the validate op deliberately reports the API server URL to the UI.
    """
    red = get_redactor()
    for line in content.splitlines():
        m = _KUBECONFIG_SECRET_KEYS.match(line)
        if m:
            val = m.group(2).strip().strip("'\"")
            if len(val) >= 8:
                red.register(val)


def prepare_env(cfg: Config, store: SecretStore, env: dict[str, str],
                kube_target: sqlite3.Row, job_id: int) -> Optional[Path]:
    """Set KUBECONFIG in the child env. Returns a temp path to clean up (or None).

    Path mode: point at the operator-managed file (must live under the data
    dir when running under the shipped systemd sandbox). Ref mode: decrypt the
    imported copy to a 0600 file for the duration of the job.
    """
    tmp: Optional[Path] = None
    if kube_target["kubeconfig_ref"]:
        content = store.get(kube_target["kubeconfig_ref"])
        if content is None:
            raise RuntimeError(f"kube target '{kube_target['name']}': stored kubeconfig "
                               "missing from the secret store")
        _register_kubeconfig_secrets(content)
        tmp = kubeconfigs_dir(cfg) / f".job_{job_id}.kubeconfig"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        env["KUBECONFIG"] = str(tmp)
    else:
        path = kube_target["kubeconfig_path"]
        env["KUBECONFIG"] = path
        try:
            _register_kubeconfig_secrets(Path(path).read_text(encoding="utf-8"))
        except OSError:
            pass    # unreadable path — the validate op reports this properly
    return tmp


def parse_op_run_id(output: str, results_dir: Path) -> Optional[str]:
    """Extract the op run id from the runner's 'ops run -> <dir>' line."""
    m = re.search(re.escape(str(results_dir)) + r"/ops/([A-Za-z0-9][A-Za-z0-9._-]*)", output)
    return m.group(1) if m else None


def _ops_run_row(cfg: Config, op_run_id: str, job: sqlite3.Row) -> Optional[dict[str, Any]]:
    meta = read_meta(cfg.results_dir / "ops" / op_run_id)
    if meta is None:
        return None
    return {
        "op_run_id": op_run_id,
        "kind": meta.get("op", ""),
        "kube_target_id": job["kube_target_id"],
        "kube_target_name": (meta.get("target") or {}).get("name", ""),
        "label": meta.get("label", ""),
        "params": json.dumps(meta.get("params") or {}),
        "status": meta.get("status", ""),
        "linked_run_id": (meta.get("params") or {}).get("linked_run_id", "") or
                         (meta.get("headline") or {}).get("linked_run_id", ""),
        "headline": json.dumps(meta.get("headline") or {}),
        "created_utc": meta.get("created_utc", ""),
        "finished_utc": meta.get("finished_utc", ""),
    }


def index_ops_run(cfg: Config, conn: sqlite3.Connection, op_run_id: str,
                  job: sqlite3.Row) -> None:
    row = _ops_run_row(cfg, op_run_id, job)
    if row:
        queries.upsert_ops_run(conn, row)


def converge_ops_run(cfg: Config, op_run_id: str, job_state: str) -> None:
    """Drive a stuck non-terminal meta.json to terminal when its job has ended
    (mirror of index.converge_run_status for benchmark runs)."""
    if job_state not in ("done", "failed", "canceled"):
        return
    path = cfg.results_dir / "ops" / op_run_id / "meta.json"
    meta = read_meta(path.parent)
    if meta is None or meta.get("status") in OPS_TERMINAL:
        return
    meta["status"] = "canceled" if job_state == "canceled" else "failed"
    if not meta.get("finished_utc"):
        meta["finished_utc"] = utc_now_iso()
    atomic_write_json(path, meta)


def _marker_payload(log_path: Path, marker: str) -> Optional[dict[str, Any]]:
    """Last occurrence of ``MARKER {json}`` in the job output, parsed."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    payload: Optional[dict[str, Any]] = None
    for line in text.splitlines():
        if line.startswith(marker + " "):
            try:
                obj = json.loads(line[len(marker) + 1:])
                if isinstance(obj, dict):
                    payload = obj
            except ValueError:
                continue
    return payload


def reconcile_stale_ops_jobs(cfg: Config, conn: sqlite3.Connection,
                             startup: bool = False) -> int:
    """Converge ops jobs whose worker process is gone (crash recovery).

    A scenario/backup CLI is its own session leader, so it can outlive a worker
    crash; when it finishes it writes a terminal meta.json but nothing updates
    the owning job row (the tracking thread died with the worker). Left alone,
    the job stays 'running' forever and the per-target mutex is wedged.

    A job is converged when its worker process is provably gone: the recorded
    pid is dead, OR its op-run meta.json on disk is already terminal. A
    ``running`` job with NO pid is ambiguous — it may have just been claimed a
    moment before ``run_job`` recorded the pid — so it is only reaped on the
    worker-startup pass (``startup=True``), where no concurrent claims exist;
    the opportunistic web-tier call leaves it alone. Returns how many converged.
    """
    n = 0
    for job in queries.list_jobs(conn, states=("running", "canceling")):
        if not is_ops_kind(job["kind"]):
            continue
        pid = job["pid"]
        run_id = job["run_id"]
        meta = read_meta(cfg.results_dir / "ops" / run_id) if run_id else None
        meta_terminal = meta is not None and meta.get("status") in OPS_TERMINAL
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive and not meta_terminal:
            continue                           # genuinely running
        if not pid and not meta_terminal and not startup:
            continue                           # claim window (pid not yet recorded)
        if job["state"] == "canceling":
            state = "canceled"
        elif meta_terminal:
            state = "done" if meta["status"] in ("complete", "warning") else "failed"
        else:
            state = "failed"
        queries.update_job(conn, job["id"], state=state, pid=None,
                           finished_utc=utc_now_iso(),
                           error="" if state == "done"
                           else "interrupted (worker restart)")
        if run_id:
            converge_ops_run(cfg, run_id, "canceled" if state == "canceled" else
                             ("done" if state == "done" else "failed"))
            index_ops_run(cfg, conn, run_id, job)
        n += 1
    # Straggler pass: converge any ops run still indexed non-terminal whose
    # owning job has ALREADY ended (e.g. marked terminal by another code path
    # before this ran). Self-heals regardless of reconcile ordering.
    for row in queries.list_ops_runs(conn):
        if row["status"] in OPS_TERMINAL:
            continue
        job = queries.job_for_run(conn, row["op_run_id"])
        if job is None or job["state"] in ("done", "failed", "canceled"):
            jstate = job["state"] if job else "failed"
            converge_ops_run(cfg, row["op_run_id"],
                             "canceled" if jstate == "canceled" else
                             ("done" if jstate == "done" else "failed"))
            if job is not None:
                index_ops_run(cfg, conn, row["op_run_id"], job)
            n += 1
    return n


SEVERITY_ORDER = ("ok", "info", "warn", "crit")


def _disk_trend_finding(history: list[sqlite3.Row],
                        now_pct: Optional[float]) -> Optional[dict[str, Any]]:
    """Least-squares projection of data-volume fill from health history.

    Returns a synthetic finding when the trend crosses 90% within 14 days —
    the alert that matters BEFORE the disk_pct threshold ever fires."""
    import time as _time
    from datetime import datetime, timezone
    pts: list[tuple[float, float]] = []
    for row in reversed(history):        # oldest -> newest
        try:
            m = json.loads(row["metrics"] or "{}")
            pct = m.get("disk_pct_max")
            if pct is None:
                continue
            ts = datetime.strptime(row["ts_utc"], "%Y-%m-%dT%H:%M:%SZ")
            pts.append((ts.replace(tzinfo=timezone.utc).timestamp(), float(pct)))
        except (ValueError, KeyError, TypeError):
            continue
    if now_pct is not None:
        pts.append((_time.time(), float(now_pct)))
    if len(pts) < 3:
        return None
    xs = [p[0] / 86400 for p in pts]     # days
    ys = [p[1] for p in pts]
    n = len(pts)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom  # %/day
    if slope < 0.1:                       # flat or shrinking — no projection
        return None
    latest = ys[-1]
    days_to_90 = (90.0 - latest) / slope
    if days_to_90 > 14 or days_to_90 < 0:
        return None
    sev = "crit" if days_to_90 <= 3 else "warn"
    return {"id": "disk_trend", "severity": sev,
            "title": "Data volume trending toward full",
            "value": f"~{days_to_90:.1f} days to 90% (+{slope:.2f}%/day)",
            "detail": "Linear projection over recent health samples. When "
                      "/pgdata fills, Postgres PANICs and the pod crash-loops.",
            "remediation": "Expand the PVC now (storage class must allow "
                           "expansion), check inactive slots and WAL retention.",
            "action": {"type": "diag", "checks": ["pvc_usage", "slots"]}}


def _record_health(cfg: Config, conn: sqlite3.Connection, kt_id: int,
                   health: dict[str, Any]) -> None:
    """Cache the health doc, append history, inject the trend finding, and
    fire a notification on a status transition."""
    prev = queries.list_health_history(conn, kt_id, limit=60)
    prev_status = prev[0]["status"] if prev else None

    metrics = health.get("metrics") or {}
    trend = _disk_trend_finding(prev, metrics.get("disk_pct_max"))
    if trend is not None:
        findings = [f for f in (health.get("findings") or [])
                    if f.get("id") != "disk_trend"]
        findings.insert(0, trend)
        health["findings"] = findings
        worst = max((SEVERITY_ORDER.index(f.get("severity", "ok"))
                     for f in findings), default=0)
        health["status"] = SEVERITY_ORDER[worst]

    status = str(health.get("status") or "ok")
    findings = health.get("findings") or []
    crit = sum(1 for f in findings if f.get("severity") == "crit")
    warn = sum(1 for f in findings if f.get("severity") == "warn")
    queries.update_kube_target(conn, kt_id, health_json=json.dumps(health),
                               health_utc=utc_now_iso())
    queries.insert_health_history(conn, kt_id, status, crit, warn,
                                  {k: v for k, v in metrics.items()})

    # Notify on TRANSITIONS, not states — an always-warn cluster shouldn't
    # page every 15 minutes.
    if prev_status is not None and status != prev_status:
        kt = queries.get_kube_target(conn, kt_id)
        try:
            from pgbench_webapp.notify import notify_health
            from pgbench_webapp.secrets_store import SecretStore
            store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
            top = "; ".join(f"{f.get('title')} ({f.get('value')})"
                            for f in findings[:3])
            notify_health(conn, store,
                          target=kt["name"] if kt else str(kt_id),
                          prev_status=prev_status, status=status, summary=top)
        except Exception:  # noqa: BLE001 — notifications are best-effort
            pass


def maybe_enqueue_auto_health(cfg: Config, conn: sqlite3.Connection) -> int:
    """Continuous intelligence: enqueue a health check for every target whose
    auto-health interval has elapsed. Called from the worker loop."""
    from datetime import datetime, timezone
    n = 0
    for kt in queries.list_kube_targets(conn):
        interval = int(kt["auto_health_s"] or 0)
        if interval <= 0 or not kt["cr_name"]:
            continue
        last = kt["health_utc"]
        if last:
            try:
                ts = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ") \
                    .replace(tzinfo=timezone.utc).timestamp()
                if (datetime.now(timezone.utc).timestamp() - ts) < interval:
                    continue
            except ValueError:
                pass
        # Never mid-operation: a health check exec'd into pods during a
        # scenario/backup/restart produces garbage findings and false
        # transition alerts. Wait for the destructive op to finish.
        if queries.active_ops_jobs(conn, kt["id"],
                                   ("ops_health", "ops_scenario", "ops_backup",
                                    "ops_cr_apply", "ops_operate")):
            continue
        from pgbench_webapp.ops_routes import build_ops_spec_yaml
        spec_yaml = build_ops_spec_yaml(kt, "health", {},
                                        f"auto-health-{kt['name']}")
        if queries.enqueue_ops_job_atomic(conn, "ops_health", spec_yaml,
                                          "system:auto-health", kt["id"], ()):
            n += 1
    return n


def postprocess(cfg: Config, conn: sqlite3.Connection, job: sqlite3.Row,
                state: str, op_run_id: Optional[str], log_path: Path) -> None:
    """After an ops job ends: cache validate/discover results onto the kube
    target, persist schedule-pause state, and index/converge the op run."""
    kt_id = job["kube_target_id"]
    kind = job["kind"]
    if kt_id and kind == "ops_validate":
        summary = _marker_payload(log_path, SUMMARY_MARKER)
        if summary:
            fields: dict[str, Any] = {"api_server": summary.get("api_server", ""),
                                      "last_validated_utc": utc_now_iso(),
                                      "last_validation_ok":
                                          1 if summary.get("ok") else 0}
            kt = queries.get_kube_target(conn, kt_id)
            # Pre-fill discovered names only where the operator left them blank.
            if kt is not None:
                if summary.get("cr_kind") and not kt["cr_name"]:
                    fields["cr_kind"] = summary["cr_kind"]
                    if len(summary.get("cr_names") or []) == 1:
                        fields["cr_name"] = summary["cr_names"][0]
                if summary.get("pguser_secret") and not kt["pguser_secret"]:
                    fields["pguser_secret"] = summary["pguser_secret"]
            queries.update_kube_target(conn, kt_id, **fields)
    if kt_id and kind == "ops_discover":
        topo = _marker_payload(log_path, TOPOLOGY_MARKER)
        if topo:
            fields = {"topology_json": json.dumps(topo), "topology_utc": utc_now_iso()}
            kt = queries.get_kube_target(conn, kt_id)
            if kt is not None and topo.get("cr_name") and not kt["cr_name"]:
                fields["cr_kind"] = topo.get("cr_kind") or kt["cr_kind"]
                fields["cr_name"] = topo["cr_name"]
            queries.update_kube_target(conn, kt_id, **fields)
    if kt_id and kind == "ops_pg_params":
        params = _marker_payload(log_path, PARAMS_MARKER)
        if params and params.get("params"):
            queries.update_kube_target(conn, kt_id,
                                       params_json=json.dumps(params),
                                       params_utc=utc_now_iso())
    if kt_id and kind == "ops_health":
        health = _marker_payload(log_path, HEALTH_MARKER)
        if health:
            _record_health(cfg, conn, kt_id, health)
    if kt_id:
        sched = _marker_payload(log_path, SCHEDULES_MARKER)
        if sched is not None:
            if sched.get("paused"):
                queries.update_kube_target(
                    conn, kt_id,
                    schedules_snapshot=json.dumps(sched.get("snapshot") or {}),
                    schedules_paused_utc=utc_now_iso())
            else:
                queries.update_kube_target(conn, kt_id, schedules_snapshot=None,
                                           schedules_paused_utc=None)
    if op_run_id:
        converge_ops_run(cfg, op_run_id, state)
        index_ops_run(cfg, conn, op_run_id, job)
