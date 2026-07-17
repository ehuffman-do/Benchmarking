"""Cluster Ops HTTP API: Kube Targets, op runs, and op job launch.

Same security posture as every other route family: RBAC via ``require``,
CSRF on all mutations, audit on every action, and destructive operations
(CR patches, backups, scenario firing, schedule pausing) are admin-only AND
require a typed confirmation of the cluster (CR) name. The web tier never
runs kubectl — every cluster interaction is an enqueued job the worker
executes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)

from pgbench_harness.errors import SpecError
from pgbench_harness.ops.oprun import TERMINAL as OPS_TERMINAL
from pgbench_harness.ops.oprun import read_meta
from pgbench_harness.ops.opspec import parse_ops_spec
from pgbench_webapp import ops_support, queries
from pgbench_webapp.config import Config
from pgbench_webapp.secrets_store import SecretStore
from pgbench_webapp.security import CSRF_FIELD


def _kt_json(row: sqlite3.Row) -> dict[str, Any]:
    """Public view of a kube target. The kubeconfig itself is never returned —
    only where it lives (path) or that an imported copy exists (ref bool)."""
    return {
        "id": row["id"], "name": row["name"],
        "kubeconfig_path": row["kubeconfig_path"],
        "kubeconfig_imported": bool(row["kubeconfig_ref"]),
        "context": row["context"], "namespace": row["namespace"],
        "cr_kind": row["cr_kind"], "cr_name": row["cr_name"],
        "pguser_secret": row["pguser_secret"],
        "pguser_secret_key": row["pguser_secret_key"],
        "db_user": row["db_user"], "db_name": row["db_name"],
        "api_server": row["api_server"],
        "last_validated_utc": row["last_validated_utc"],
        "last_validation_ok": (None if row["last_validation_ok"] is None
                               else bool(row["last_validation_ok"])),
        "topology_utc": row["topology_utc"],
        "params_utc": row["params_utc"],
        "health_utc": row["health_utc"],
        "health_status": _health_status(row),
        "auto_health_s": int(row["auto_health_s"] or 0),
        "schedules_paused": bool(row["schedules_snapshot"]),
        "schedules_paused_utc": row["schedules_paused_utc"],
        "created_utc": row["created_utc"],
    }


def _health_status(row: sqlite3.Row) -> Optional[str]:
    """Worst-severity summary from the cached health document (list badges)."""
    if not row["health_json"]:
        return None
    try:
        return json.loads(row["health_json"]).get("status")
    except ValueError:
        return None


def _ops_run_json(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("params", "headline"):
        try:
            out[key] = json.loads(out.get(key) or "{}")
        except ValueError:
            out[key] = {}
    return out


def build_ops_spec_yaml(kt: sqlite3.Row, op: str, params: dict[str, Any],
                        label: str) -> str:
    """The ops job spec the worker hands to the CLI. Never contains a secret:
    the kubeconfig travels as KUBECONFIG in the child env, the DB password is
    read from the cluster Secret by the runner itself."""
    doc = {
        "op": op,
        "label": label or f"{op}-{kt['name']}",
        "target": {
            "name": kt["name"], "context": kt["context"],
            "namespace": kt["namespace"], "cr_kind": kt["cr_kind"],
            "cr_name": kt["cr_name"], "pguser_secret": kt["pguser_secret"],
            "pguser_secret_key": kt["pguser_secret_key"],
            "db_user": kt["db_user"], "db_name": kt["db_name"],
        },
        "params": params,
    }
    return yaml.safe_dump(doc, sort_keys=False)


# One destructive operation per target at a time — every kind in this tuple
# blocks (and is blocked by) every other while active on the same target.
DESTRUCTIVE_KINDS = ("ops_cr_apply", "ops_scenario", "ops_backup",
                     "ops_operate", "ops_pmm_enable", "ops_pmm_disable")


def register(app: FastAPI, cfg: Config, store: SecretStore) -> None:
    # Imported here (not at module top) purely for the shared dependency
    # helpers; app.py imports this module inside create_app, at which point
    # app.py is fully initialized.
    from pgbench_webapp.app import _check_csrf, _safe_segment, get_conn, require

    def _csrf(request: Request, payload: Optional[dict] = None) -> None:
        token = (payload or {}).get(CSRF_FIELD) if payload else None
        _check_csrf(request, token or request.headers.get("x-csrf-token"))

    def _kt_or_404(conn: sqlite3.Connection, target_id: int) -> sqlite3.Row:
        kt = queries.get_kube_target(conn, target_id)
        if kt is None:
            raise HTTPException(404, "kube target not found")
        return kt

    def _params(payload: dict) -> dict[str, Any]:
        """Extract params as a dict, rejecting a non-object with a clean 400
        instead of letting dict("string") raise an uncaught 500."""
        p = payload.get("params")
        if p is None:
            return {}
        if not isinstance(p, dict):
            raise HTTPException(400, "'params' must be a JSON object")
        return dict(p)

    def _require_confirm(kt: sqlite3.Row, payload: dict) -> None:
        """Typed confirmation: the operator must retype the cluster (CR) name."""
        expected = kt["cr_name"] or kt["name"]
        if (payload.get("confirm") or "").strip() != expected:
            raise HTTPException(400, f"confirmation mismatch: type the cluster name "
                                     f"'{expected}' to proceed")

    def _enqueue_ops(conn: sqlite3.Connection, kt: sqlite3.Row, op: str,
                     params: dict[str, Any], label: str, username: str,
                     mutex_kinds: tuple[str, ...] = ()) -> int:
        kind = "ops_" + op.replace("-", "_")
        spec_yaml = build_ops_spec_yaml(kt, op, params, label)
        # Validate the spec HERE (parse errors become a clean 400) instead of
        # letting a bad param enqueue a job that dies with "exit 2" and no run.
        try:
            parse_ops_spec(yaml.safe_load(spec_yaml))
        except SpecError as exc:
            raise HTTPException(400, str(exc))
        if mutex_kinds:
            # Self-heal any orphaned ops job (worker-crash leftover) so a stale
            # 'running' row can't wedge the mutex, then enqueue atomically.
            ops_support.reconcile_stale_ops_jobs(cfg, conn)
        job_id = queries.enqueue_ops_job_atomic(conn, kind, spec_yaml, username,
                                                kt["id"], mutex_kinds)
        if job_id is None:
            raise HTTPException(409, "another destructive operation is active on "
                                     "this target — wait for it to finish")
        queries.audit(conn, username, f"ops_{op}_enqueue", target=kt["name"],
                      detail=f"job={job_id} " + json.dumps(params)[:300])
        return job_id

    def _op_run_dir(op_run_id: str) -> Path:
        d = cfg.results_dir / "ops" / _safe_segment(op_run_id)
        if not (d / "meta.json").exists():
            raise HTTPException(404, "op run not found")
        return d

    # ── kube targets ──

    @app.get("/api/kube-targets")
    def kube_targets_list(conn: sqlite3.Connection = Depends(get_conn),
                          user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        return JSONResponse([_kt_json(r) for r in queries.list_kube_targets(conn)])

    @app.get("/api/kube-targets/{target_id}")
    def kube_target_get(target_id: int, conn: sqlite3.Connection = Depends(get_conn),
                        user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        return JSONResponse(_kt_json(_kt_or_404(conn, target_id)))

    @app.post("/api/kube-targets")
    def kube_target_create(request: Request, payload: dict,
                           conn: sqlite3.Connection = Depends(get_conn),
                           user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _csrf(request, payload)
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        if queries.get_kube_target_by_name(conn, name) is not None:
            raise HTTPException(409, f"kube target '{name}' already exists")
        path = (payload.get("kubeconfig_path") or "").strip()
        content = payload.get("kubeconfig_content") or ""
        if not path and not content:
            raise HTTPException(400, "provide kubeconfig_path (a file on the app host) "
                                     "or kubeconfig_content (direct upload)")
        ref = ""
        if content:
            # Uploaded copies live Fernet-encrypted in the secret store; the
            # worker decrypts to a 0600 temp file per job. Never in the DB.
            ref = ops_support.kubeconfig_ref(name)
            store.set(ref, content)
            path = ""
        fields = {k: (payload.get(k) or d) for k, d in (
            ("context", ""), ("namespace", "percona"),
            ("cr_kind", "perconapgcluster"), ("cr_name", ""),
            ("pguser_secret", ""), ("pguser_secret_key", "password"),
            ("db_user", "doadmin"), ("db_name", "defaultdb"))}
        tid = queries.create_kube_target(conn, name=name, kubeconfig_path=path,
                                         kubeconfig_ref=ref, **fields)
        queries.audit(conn, user["username"], "kube_target_create", target=name,
                      detail=("imported" if ref else path))
        # Validation is a worker job (the web tier never runs kubectl).
        kt = _kt_or_404(conn, tid)
        job_id = _enqueue_ops(conn, kt, "validate", {}, f"validate-{name}",
                              user["username"])
        return JSONResponse({"id": tid, "validate_job_id": job_id}, status_code=201)

    @app.post("/api/kube-targets/{target_id}")
    def kube_target_update(target_id: int, request: Request, payload: dict,
                           conn: sqlite3.Connection = Depends(get_conn),
                           user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        allowed = ("kubeconfig_path", "context", "namespace", "cr_kind", "cr_name",
                   "pguser_secret", "pguser_secret_key", "db_user", "db_name")
        fields = {k: payload[k] for k in allowed if k in payload}
        if "auto_health_s" in payload:
            try:
                iv = int(payload["auto_health_s"])
            except (TypeError, ValueError):
                raise HTTPException(400, "auto_health_s must be an integer (seconds)")
            if iv != 0 and not 300 <= iv <= 86400:
                raise HTTPException(400, "auto_health_s must be 0 (off) or 300–86400")
            fields["auto_health_s"] = iv
        if payload.get("kubeconfig_content"):
            ref = kt["kubeconfig_ref"] or ops_support.kubeconfig_ref(kt["name"])
            store.set(ref, payload["kubeconfig_content"])
            fields["kubeconfig_ref"] = ref
            fields["kubeconfig_path"] = ""
        elif fields.get("kubeconfig_path"):
            # Switching (back) to path mode: the worker prefers an imported
            # copy over the path, so drop the stale encrypted copy or the new
            # path would silently never be used.
            if kt["kubeconfig_ref"]:
                store.delete(kt["kubeconfig_ref"])
                fields["kubeconfig_ref"] = ""
        if fields:
            # Kubeconfig or coordinates changed — the previous validation
            # verdict no longer applies; reset it until the next validate runs.
            # (auto_health_s alone is a scheduling knob, not a coordinate.)
            if any(k in fields for k in allowed + ("kubeconfig_ref",)):
                conn.execute("UPDATE kube_targets SET last_validation_ok=NULL "
                             "WHERE id=?", (target_id,))
            queries.update_kube_target(conn, target_id, **fields)
        queries.audit(conn, user["username"], "kube_target_update", target=kt["name"],
                      detail=",".join(sorted(fields)))
        return JSONResponse(_kt_json(_kt_or_404(conn, target_id)))

    @app.delete("/api/kube-targets/{target_id}")
    def kube_target_delete(target_id: int, request: Request,
                           conn: sqlite3.Connection = Depends(get_conn),
                           user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _csrf(request)
        kt = _kt_or_404(conn, target_id)
        if queries.active_ops_jobs(conn, target_id):
            raise HTTPException(409, "target has queued/running ops jobs — stop them first")
        if kt["kubeconfig_ref"]:
            store.delete(kt["kubeconfig_ref"])
        queries.delete_kube_target(conn, target_id)
        queries.audit(conn, user["username"], "kube_target_delete", target=kt["name"])
        return JSONResponse({"ok": True})

    # ── read-only ops: validate + discover + topology cache ──

    @app.post("/api/kube-targets/{target_id}/validate")
    def kube_target_validate(target_id: int, request: Request,
                             conn: sqlite3.Connection = Depends(get_conn),
                             user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _csrf(request)
        kt = _kt_or_404(conn, target_id)
        job_id = _enqueue_ops(conn, kt, "validate", {}, f"validate-{kt['name']}",
                              user["username"])
        return JSONResponse({"job_id": job_id})

    @app.post("/api/kube-targets/{target_id}/discover")
    def kube_target_discover(target_id: int, request: Request,
                             conn: sqlite3.Connection = Depends(get_conn),
                             user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _csrf(request)
        kt = _kt_or_404(conn, target_id)
        job_id = _enqueue_ops(conn, kt, "discover", {}, f"discover-{kt['name']}",
                              user["username"])
        return JSONResponse({"job_id": job_id})

    @app.get("/api/kube-targets/{target_id}/topology")
    def kube_target_topology(target_id: int,
                             conn: sqlite3.Connection = Depends(get_conn),
                             user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        kt = _kt_or_404(conn, target_id)
        topo: Any = None
        if kt["topology_json"]:
            try:
                topo = json.loads(kt["topology_json"])
            except ValueError:
                topo = None
        return JSONResponse({"topology": topo, "collected_utc": kt["topology_utc"],
                             "schedules_paused": bool(kt["schedules_snapshot"])})

    # ── parameter map (introspected pg_settings catalog) ──

    @app.post("/api/kube-targets/{target_id}/pg-params")
    def kube_target_pg_params_refresh(target_id: int, request: Request,
                                      conn: sqlite3.Connection = Depends(get_conn),
                                      user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _csrf(request)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        job_id = _enqueue_ops(conn, kt, "pg-params", {},
                              f"pg-params-{kt['name']}", user["username"])
        return JSONResponse({"job_id": job_id})

    @app.get("/api/kube-targets/{target_id}/pg-params")
    def kube_target_pg_params(target_id: int,
                              conn: sqlite3.Connection = Depends(get_conn),
                              user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        kt = _kt_or_404(conn, target_id)
        catalog: Any = None
        if kt["params_json"]:
            try:
                catalog = json.loads(kt["params_json"])
            except ValueError:
                catalog = None
        return JSONResponse({"catalog": catalog, "collected_utc": kt["params_utc"]})

    # ── diagnostics workbench (read-only, operator-level) ──

    @app.get("/api/ops/diag-catalog")
    def ops_diag_catalog(user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        from pgbench_harness.ops.diag import catalog_json
        return JSONResponse({"checks": catalog_json()})

    @app.get("/api/ops/sidecar-catalog")
    def ops_sidecar_catalog(user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        from pgbench_harness.ops.params import sidecar_catalog
        return JSONResponse(sidecar_catalog())

    @app.post("/api/kube-targets/{target_id}/diag")
    def ops_diag(target_id: int, request: Request, payload: dict,
                 conn: sqlite3.Connection = Depends(get_conn),
                 user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        params = _params(payload)
        from pgbench_harness.ops.diag import CHECKS_BY_KEY
        checks = params.get("checks")
        if checks is not None:
            if not isinstance(checks, list) or not checks:
                raise HTTPException(400, "'checks' must be a non-empty list")
            bad = [c for c in checks if c not in CHECKS_BY_KEY]
            if bad:
                raise HTTPException(400, f"unknown checks: {', '.join(map(str, bad))}")
        try:
            watch_s = float(params.get("watch_s") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "'watch_s' must be a number")
        if not 0 <= watch_s <= 3600:
            raise HTTPException(400, "'watch_s' must be between 0 and 3600")
        job_id = _enqueue_ops(conn, kt, "diag", params,
                              payload.get("label") or "", user["username"])
        return JSONResponse({"job_id": job_id})

    # ── health checks (built-in intelligence) ──

    @app.post("/api/kube-targets/{target_id}/health")
    def ops_health_run(target_id: int, request: Request,
                       conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _csrf(request)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        job_id = _enqueue_ops(conn, kt, "health", {},
                              f"health-{kt['name']}", user["username"])
        return JSONResponse({"job_id": job_id})

    @app.get("/api/kube-targets/{target_id}/health")
    def ops_health_get(target_id: int,
                       conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        kt = _kt_or_404(conn, target_id)
        health: Any = None
        if kt["health_json"]:
            try:
                health = json.loads(kt["health_json"])
            except ValueError:
                health = None
        return JSONResponse({"health": health, "collected_utc": kt["health_utc"]})

    # ── operations (destructive: admin + typed confirmation) ──

    @app.post("/api/kube-targets/{target_id}/cr-apply")
    def ops_cr_apply(target_id: int, request: Request, payload: dict,
                     conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        params = _params(payload)
        dry_run = bool(params.get("dry_run"))
        # A dry-run makes no cluster change, so it needs neither confirmation nor
        # the mutex; a real apply needs both (enforced atomically at enqueue).
        mutex: tuple[str, ...] = ()
        if not dry_run:
            _require_confirm(kt, payload)
            mutex = DESTRUCTIVE_KINDS
        job_id = _enqueue_ops(conn, kt, "cr-apply", params,
                              payload.get("label") or "", user["username"], mutex)
        return JSONResponse({"job_id": job_id})

    @app.post("/api/kube-targets/{target_id}/backup")
    def ops_backup(target_id: int, request: Request, payload: dict,
                   conn: sqlite3.Connection = Depends(get_conn),
                   user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        _require_confirm(kt, payload)
        params = _params(payload)
        job_id = _enqueue_ops(conn, kt, "backup", params,
                              payload.get("label") or "", user["username"],
                              DESTRUCTIVE_KINDS)
        return JSONResponse({"job_id": job_id})

    @app.post("/api/kube-targets/{target_id}/scenario")
    def ops_scenario(target_id: int, request: Request, payload: dict,
                     conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        _require_confirm(kt, payload)
        # Safety rail: one destructive op per target at a time (enforced
        # atomically at enqueue). The runner additionally refuses to FIRE if a
        # pgBackRest lock is held.
        params = _params(payload)
        job_id = _enqueue_ops(conn, kt, "scenario", params,
                              payload.get("label") or "", user["username"],
                              DESTRUCTIVE_KINDS)
        return JSONResponse({"job_id": job_id})

    @app.post("/api/kube-targets/{target_id}/operate")
    def ops_operate(target_id: int, request: Request, payload: dict,
                    conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        """Day-2 operation (restart/switchover/failover/scale/resize/schedules).
        Dry-run needs no confirmation or mutex; a real execution needs both."""
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        params = _params(payload)
        dry_run = bool(params.get("dry_run"))
        mutex: tuple[str, ...] = ()
        if not dry_run:
            _require_confirm(kt, payload)
            mutex = DESTRUCTIVE_KINDS
        job_id = _enqueue_ops(conn, kt, "operate", params,
                              payload.get("label") or
                              f"{params.get('operation', 'operate')}-{kt['name']}",
                              user["username"], mutex)
        return JSONResponse({"job_id": job_id})

    # ── PMM monitoring (enable = rolling restart, so destructive rules) ──

    def _latest_pmm_enable_run(kt: sqlite3.Row) -> Optional[str]:
        """Newest pmm-enable run for this target that still has its CR backup
        — the default restore point for pmm-disable."""
        ops_dir = cfg.results_dir / "ops"
        if not ops_dir.exists():
            return None
        candidates = []
        for d in ops_dir.iterdir():
            if not d.name.startswith("pmm-enable-"):
                continue
            meta = read_meta(d)
            if meta is None or (meta.get("target") or {}).get("name") != kt["name"]:
                continue
            if not (d / "backup" / f"cr-{kt['cr_name']}.yaml").exists():
                continue
            candidates.append((meta.get("created_utc") or "", d.name))
        return max(candidates)[1] if candidates else None

    @app.post("/api/kube-targets/{target_id}/pmm/enable")
    def ops_pmm_enable(target_id: int, request: Request, payload: dict,
                       conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        """Enable PMM 3.x monitoring end to end. Rolls every instance pod, so
        it follows the destructive contract: typed confirmation + mutex (a
        dry-run needs neither). The token never transits the web tier — the
        worker reads $PGB_PMM_TOKEN from its own environment."""
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        params = _params(payload)
        if not str(params.get("server_host") or "").strip():
            raise HTTPException(400, "params.server_host is required "
                                     "(the PMM server address)")
        dry_run = bool(params.get("dry_run"))
        mutex: tuple[str, ...] = ()
        if not dry_run:
            _require_confirm(kt, payload)
            mutex = DESTRUCTIVE_KINDS
        job_id = _enqueue_ops(conn, kt, "pmm-enable", params,
                              payload.get("label") or f"pmm-enable-{kt['name']}",
                              user["username"], mutex)
        return JSONResponse({"job_id": job_id})

    @app.post("/api/kube-targets/{target_id}/pmm/status")
    def ops_pmm_status(target_id: int, request: Request, payload: dict,
                       conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        """PMM validation report only — zero cluster mutations, so operator
        role and no confirmation/mutex (like diag)."""
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        params = _params(payload)
        if not str(params.get("server_host") or "").strip():
            raise HTTPException(400, "params.server_host is required "
                                     "(the PMM server address)")
        job_id = _enqueue_ops(conn, kt, "pmm-status", params,
                              payload.get("label") or f"pmm-status-{kt['name']}",
                              user["username"])
        return JSONResponse({"job_id": job_id})

    @app.post("/api/kube-targets/{target_id}/pmm/disable")
    def ops_pmm_disable(target_id: int, request: Request, payload: dict,
                        conn: sqlite3.Connection = Depends(get_conn),
                        user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        """Restore the CR backed up by a pmm-enable run + delete the secret.
        rollback_of defaults to the newest pmm-enable run with a backup."""
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        params = _params(payload)
        rollback_of = str(params.get("rollback_of") or "").strip()
        if rollback_of:
            _safe_segment(rollback_of)          # raises 400 on traversal shapes
        else:
            src = _latest_pmm_enable_run(kt)
            if not src:
                raise HTTPException(400, "no pmm-enable run with a CR backup "
                                         "found for this target — nothing to "
                                         "restore")
            params["rollback_of"] = src
        _require_confirm(kt, payload)
        job_id = _enqueue_ops(conn, kt, "pmm-disable", params,
                              payload.get("label") or f"pmm-disable-{kt['name']}",
                              user["username"], DESTRUCTIVE_KINDS)
        return JSONResponse({"job_id": job_id})

    @app.get("/api/kube-targets/{target_id}/health-history")
    def ops_health_history(target_id: int,
                           conn: sqlite3.Connection = Depends(get_conn),
                           user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        _kt_or_404(conn, target_id)
        rows = queries.list_health_history(conn, target_id, limit=200)
        out = []
        for r in rows:
            try:
                metrics = json.loads(r["metrics"] or "{}")
            except ValueError:
                metrics = {}
            out.append({"ts_utc": r["ts_utc"], "status": r["status"],
                        "crit": r["crit"], "warn": r["warn"], "metrics": metrics})
        return JSONResponse({"history": out})

    @app.post("/api/kube-targets/{target_id}/monitor")
    def ops_monitor_start(target_id: int, request: Request, payload: dict,
                          conn: sqlite3.Connection = Depends(get_conn),
                          user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _csrf(request, payload)
        kt = _kt_or_404(conn, target_id)
        params = _params(payload)
        job_id = _enqueue_ops(conn, kt, "monitor", params,
                              payload.get("label") or "", user["username"],
                              ("ops_monitor",))
        return JSONResponse({"job_id": job_id})

    @app.post("/api/kube-targets/{target_id}/schedules/{action}")
    def ops_schedules(target_id: int, action: str, request: Request, payload: dict,
                      conn: sqlite3.Connection = Depends(get_conn),
                      user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _csrf(request, payload)
        if action not in ("pause", "restore"):
            raise HTTPException(404, "unknown schedules action")
        kt = _kt_or_404(conn, target_id)
        if not kt["cr_name"]:
            raise HTTPException(400, "target has no CR name — run discover first")
        _require_confirm(kt, payload)
        params: dict[str, Any] = {"action": f"{action}_schedules"}
        if action == "restore":
            if not kt["schedules_snapshot"]:
                raise HTTPException(400, "no schedules snapshot recorded — nothing to restore")
            params["snapshot"] = json.loads(kt["schedules_snapshot"])
        job_id = _enqueue_ops(conn, kt, "cr-apply", params,
                              f"{action}-schedules-{kt['name']}", user["username"])
        return JSONResponse({"job_id": job_id})

    # ── op runs: index, artifacts, live stream ──

    @app.get("/api/ops/runs")
    def ops_runs_list(target: Optional[int] = None,
                      conn: sqlite3.Connection = Depends(get_conn),
                      user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        rows = queries.list_ops_runs(conn, kube_target_id=target)
        return JSONResponse([_ops_run_json(r) for r in rows])

    @app.get("/api/ops/runs/{op_run_id}")
    def ops_run_get(op_run_id: str, conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        run_dir = _op_run_dir(op_run_id)
        meta = read_meta(run_dir) or {}
        row = queries.get_ops_run(conn, op_run_id)
        job = queries.job_for_run(conn, op_run_id)
        stitched: Any = None
        sp = run_dir / "stitched.json"
        if sp.exists():
            try:
                stitched = json.loads(sp.read_text(encoding="utf-8"))
            except ValueError:
                stitched = None
        files = sorted(p.name for p in run_dir.iterdir() if p.is_file())
        raw_dir = run_dir / "raw"
        raw = sorted(p.name for p in raw_dir.iterdir() if p.is_file()) \
            if raw_dir.is_dir() else []
        return JSONResponse({"meta": meta, "index": _ops_run_json(row) if row else None,
                             "job_id": job["id"] if job else None,
                             "job_state": job["state"] if job else None,
                             "stitched": stitched, "files": files, "raw_files": raw})

    @app.delete("/api/ops/runs/{op_run_id}")
    def ops_run_delete(op_run_id: str, request: Request,
                       conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _csrf(request)
        run_dir = _op_run_dir(op_run_id)
        meta = read_meta(run_dir) or {}
        if meta.get("status") not in OPS_TERMINAL:
            raise HTTPException(409, "op run is still active — stop its job first")
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
        queries.delete_ops_run(conn, op_run_id)
        queries.audit(conn, user["username"], "ops_run_delete", target=op_run_id)
        return JSONResponse({"ok": True})

    @app.get("/ops/runs/{op_run_id}/file")
    def ops_run_file(op_run_id: str, name: str,
                     user: sqlite3.Row = Depends(require("viewer"))) -> Any:
        run_dir = _op_run_dir(op_run_id)
        rel = name
        if rel.startswith("raw/"):
            path = run_dir / "raw" / _safe_segment(rel[4:])
        elif rel.startswith("parsed/"):
            path = run_dir / "parsed" / _safe_segment(rel[7:])
        else:
            path = run_dir / _safe_segment(rel)
        if not path.is_file():
            raise HTTPException(404, f"no such artifact: {name}")
        media = "text/csv" if path.suffix == ".csv" else "text/plain"
        return FileResponse(path, media_type=media, filename=path.name)

    @app.get("/ops/runs/{op_run_id}/report", response_class=HTMLResponse)
    def ops_run_report(op_run_id: str, regen: int = 0,
                       user: sqlite3.Row = Depends(require("viewer"))) -> HTMLResponse:
        run_dir = _op_run_dir(op_run_id)
        report = run_dir / "report.html"
        if regen or not report.exists():
            from pgbench_harness.ops.report_ops import generate_ops_report
            try:
                generate_ops_report(run_dir)
            except Exception:  # noqa: BLE001 — don't echo internal paths/traces
                raise HTTPException(500, "report generation failed")
        if not report.exists():
            raise HTTPException(404, "report not available for this run")
        return HTMLResponse(report.read_text(encoding="utf-8"))

    @app.get("/api/ops/compare")
    def ops_compare(runs: str, conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        """Cross-scenario comparison payload (trigger, downtime, election, TL)."""
        ids = [_safe_segment(r) for r in runs.split(",") if r]
        if not 2 <= len(ids) <= 8:
            raise HTTPException(400, "compare 2–8 op runs")
        from pgbench_harness.ops.report_ops import comparison_payload
        rows = []
        for rid in ids:
            run_dir = cfg.results_dir / "ops" / rid
            if not (run_dir / "meta.json").exists():
                raise HTTPException(404, f"op run not found: {rid}")
            rows.append(comparison_payload(run_dir))
        return JSONResponse({"runs": rows})

    @app.get("/ops/runs/{op_run_id}/stream")
    def ops_run_stream(op_run_id: str,
                       user: sqlite3.Row = Depends(require("viewer"))) -> StreamingResponse:
        run_dir = _op_run_dir(op_run_id)
        return StreamingResponse(_ops_sse(run_dir), media_type="text/event-stream")

    @app.get("/api/ops/timeline/{op_run_id}")
    def ops_run_timeline(op_run_id: str,
                         user: sqlite3.Row = Depends(require("viewer"))) -> PlainTextResponse:
        run_dir = _op_run_dir(op_run_id)
        p = run_dir / "TIMELINE.txt"
        if not p.exists():
            raise HTTPException(404, "no timeline for this run")
        return PlainTextResponse(p.read_text(encoding="utf-8"))


def _ops_sse(run_dir: Path, max_ticks: int = 12 * 3600) -> Iterator[str]:
    """SSE for the ops cockpit: log tail, event feed, live status snapshot,
    and incremental sampler CSVs — all file-tailing with offsets, exactly like
    the benchmark cockpit, so EventSource reconnects reset cleanly."""
    from pgbench_webapp.app import _event, _read_tail

    log = run_dir / "ops.log"
    events = run_dir / "events.jsonl"
    status = run_dir / "status.json"
    sent_log = 0
    sent_events = 0
    status_mtime = 0.0
    csv_sent: dict[str, int] = {}
    meta = read_meta(run_dir) or {}
    yield _event("hello", {"op_run_id": run_dir.name, "op": meta.get("op", ""),
                           "status": meta.get("status", ""),
                           "created_utc": meta.get("created_utc", "")})
    for _ in range(max_ticks):
        if log.exists():
            chunk, sent_log = _read_tail(log, sent_log)
            if chunk:
                yield _event("log", chunk)
        if events.exists():
            text = events.read_text(encoding="utf-8", errors="replace")
            # Only consider COMPLETE lines (those terminated by \n). A line read
            # mid-append would otherwise fail json.loads, get skipped, and be
            # counted — permanently dropping that event once it completes.
            complete = text.count("\n")
            if complete > sent_events:
                lines = text.split("\n")[:complete]
                out = []
                for ln in lines[sent_events:]:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
                yield _event("events", {"offset": sent_events, "items": out})
                sent_events = complete
        if status.exists():
            try:
                mt = status.stat().st_mtime
                if mt > status_mtime:
                    status_mtime = mt
                    yield _event("status", json.loads(status.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        parsed = run_dir / "parsed"
        if parsed.is_dir():
            terminal = (read_meta(run_dir) or {}).get("status") in OPS_TERMINAL
            for p in sorted(parsed.glob("*.csv")):
                text = p.read_text(encoding="utf-8", errors="replace")
                # Only emit complete rows (\n-terminated). Once the run is
                # terminal the file is fully flushed, so include the last row too.
                complete = text.count("\n") if not terminal \
                    else len(text.splitlines())
                lines = text.splitlines()
                if complete > 1:
                    sent = csv_sent.get(p.name, 1)   # row 0 is the header
                    if complete > sent:
                        yield _event("csv", {"file": p.name, "header": lines[0],
                                             "offset": sent - 1,
                                             "rows": lines[sent:complete]})
                        csv_sent[p.name] = complete
        meta = read_meta(run_dir) or meta
        if meta.get("status") in OPS_TERMINAL:
            # final drain of the log, then done
            if log.exists():
                chunk, sent_log = _read_tail(log, sent_log, include_partial=True)
                if chunk:
                    yield _event("log", chunk)
            yield _event("done", {"status": meta.get("status", "")})
            return
        yield _event("progress", {"status": meta.get("status", ""),
                                  "ts": int(time.time())})
        time.sleep(1)
