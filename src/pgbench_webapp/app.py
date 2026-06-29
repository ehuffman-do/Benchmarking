"""FastAPI application: browser UI + JSON API over the harness, with auth/RBAC,
CSRF, security headers, a SQLite-backed job queue, and live SSE streaming.

The web tier never runs benchmarks itself — it enqueues jobs the separate worker
executes — so bouncing this process never interrupts a run.
"""

from __future__ import annotations

import base64
import difflib
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import yaml
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pgbench_harness.capture import KEY_SETTINGS
from pgbench_webapp import __version__, harness_api, index, notify, provider, queries
from pgbench_webapp.config import Config, ensure_dirs, load_config
from pgbench_webapp.db import connect, migrate
from pgbench_webapp.secrets_store import SecretStore
from pgbench_webapp.security import (CSRF_FIELD, SECURITY_HEADERS, SESSION_COOKIE,
                                     hash_password, new_token, verify_password)
from pgbench_webapp.util import utc_now_iso
from pgbench_webapp.worker import job_password_ref, stop_job_process

_PKG = Path(__file__).resolve().parent
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_MAX, LOGIN_WINDOW_S = 10, 300


# ── dependencies ────────────────────────────────────────────────────

def get_cfg(request: Request) -> Config:
    return request.app.state.cfg


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.cfg.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_store(request: Request) -> SecretStore:
    return request.app.state.store


def _basic_user(request: Request, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    hdr = request.headers.get("authorization", "")
    if not hdr.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(hdr[6:]).decode()
        user, _, pw = raw.partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    row = queries.get_user(conn, user)
    if row and not row["disabled"] and verify_password(pw, row["pw_hash"]):
        return row
    return None


def current_user(request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)) -> Optional[sqlite3.Row]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = queries.session_user(conn, token)
        if row:
            return row
    return _basic_user(request, conn)


def require(min_role: str) -> Callable:
    """Dependency factory enforcing a minimum RBAC role at the route."""
    def dep(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Row:
        user = current_user(request, conn)
        if user is None:
            raise HTTPException(401, "authentication required")
        if ROLE_RANK.get(user["role"], 0) < ROLE_RANK[min_role]:
            raise HTTPException(403, f"requires {min_role} role")
        return user
    return dep


def _check_csrf(request: Request, submitted: Optional[str]) -> None:
    """Double-submit CSRF for cookie-authenticated browsers; Basic-auth API exempt."""
    if request.cookies.get(SESSION_COOKIE) is None:
        return  # not a cookie session (API/Basic) — CSRF not applicable
    cookie = request.cookies.get("pgbench_csrf")
    if not cookie or not submitted or cookie != submitted:
        raise HTTPException(403, "CSRF token missing or invalid")


# ── app factory ─────────────────────────────────────────────────────

def create_app(cfg: Optional[Config] = None) -> FastAPI:
    cfg = cfg or load_config()
    ensure_dirs(cfg)
    migrate(cfg.db_path)
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    # Index any runs already on disk (incl. CLI-created) at startup.
    conn0 = connect(cfg.db_path)
    try:
        index.reconcile(conn0, cfg.results_dir)
    finally:
        conn0.close()

    app = FastAPI(title="pgbench-harness", version=__version__)
    app.state.cfg = cfg
    app.state.store = store
    templates = Jinja2Templates(directory=str(_PKG / "templates"))
    app.state.templates = templates
    static_dir = _PKG / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable) -> Response:
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    _register_routes(app, cfg, store, templates)
    return app


def _register_routes(app: FastAPI, cfg: Config, store: SecretStore,
                     templates: Jinja2Templates) -> None:

    def page(request: Request, name: str, user: Optional[sqlite3.Row], **ctx: Any) -> HTMLResponse:
        ctx.update(version=__version__, csrf=request.cookies.get("pgbench_csrf", ""),
                   user=(user["username"] if user else None),
                   role=(user["role"] if user else None))
        return templates.TemplateResponse(request, name, ctx)

    # health (no auth)
    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    # ── identity (SPA bootstrap) ──
    @app.get("/api/me")
    def api_me(user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        return JSONResponse({"user": user["username"], "role": user["role"],
                             "version": __version__})

    # ── auth ──
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return page(request, "login.html", None)

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...),
              conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        ip = request.client.host if request.client else "?"
        now = time.monotonic()
        attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW_S]
        if len(attempts) >= LOGIN_MAX:
            raise HTTPException(429, "too many login attempts; wait a few minutes")
        row = queries.get_user(conn, username)
        if not row or row["disabled"] or not verify_password(password, row["pw_hash"]):
            _LOGIN_ATTEMPTS[ip] = attempts + [now]
            queries.audit(conn, username, "login_failed", detail=f"ip={ip}")
            return page(request, "login.html", None, error="Invalid credentials")
        _LOGIN_ATTEMPTS.pop(ip, None)
        token = new_token()
        expires = (datetime.now(timezone.utc) + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        queries.create_session(conn, token, row["id"], expires)
        queries.audit(conn, username, "login", detail=f"ip={ip}")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=True, samesite="lax")
        resp.set_cookie("pgbench_csrf", new_token(), secure=True, samesite="lax")
        return resp

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form(""),
               conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        _check_csrf(request, csrf_token)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            queries.delete_session(conn, token)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ── pages ──
    # The operator console (SPA, /ui) is now the default UI. These three pages
    # were fully ported, so the legacy paths redirect into the console; deep links
    # keep working. (Compare and the admin pages are not yet ported, so they
    # remain server-rendered and are reached from the console's nav.)
    @app.get("/")
    def root_to_console() -> Response:
        return RedirectResponse("/ui/", status_code=307)

    @app.get("/new")
    def new_to_console() -> Response:
        return RedirectResponse("/ui/new", status_code=307)

    @app.get("/runs/{run_id}")
    def detail_to_console(run_id: str) -> Response:
        return RedirectResponse(f"/ui/runs/{run_id}", status_code=307)

    # ── JSON API: runs / jobs index (SPA data) ──
    @app.get("/api/runs")
    def api_list_runs(conn: sqlite3.Connection = Depends(get_conn),
                      user: sqlite3.Row = Depends(require("viewer")),
                      q: str = "", status: str = "") -> JSONResponse:
        where, params = [], []
        if q:
            where.append("(label LIKE ? OR tags LIKE ? OR ticket LIKE ? OR owner LIKE ?)")
            params += [f"%{q}%"] * 4
        if status:
            where.append("status=?")
            params.append(status)
        rows = queries.list_runs(conn, " AND ".join(where), tuple(params))
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/runs/{run_id}")
    def api_get_run(run_id: str, conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        r = queries.get_run(conn, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return JSONResponse(dict(r))

    # Concurrency: how many runs the worker executes at once (the max_concurrency
    # guard). Default 1; raise it to run against several clusters simultaneously.
    @app.get("/api/settings")
    def api_settings(conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        return JSONResponse({"max_concurrency":
                             int(queries.get_setting(conn, "max_concurrency", "1") or 1)})

    @app.post("/api/settings/concurrency")
    def api_set_concurrency(request: Request, payload: dict,
                            conn: sqlite3.Connection = Depends(get_conn),
                            user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        try:
            value = max(1, min(16, int(payload.get("value", 1))))
        except (TypeError, ValueError):
            raise HTTPException(400, "value must be an integer 1–16")
        queries.set_setting(conn, "max_concurrency", str(value))
        queries.audit(conn, user["username"], "settings_update", target="max_concurrency",
                      detail=str(value))
        return JSONResponse({"max_concurrency": value})

    _JOB_FIELDS = ("id", "kind", "state", "run_id", "requested_by", "scheduled_utc",
                   "created_utc", "started_utc", "finished_utc", "exit_code", "error")

    @app.get("/api/jobs")
    def api_list_jobs(conn: sqlite3.Connection = Depends(get_conn),
                      user: sqlite3.Row = Depends(require("viewer")),
                      active: int = 0) -> JSONResponse:
        states = ("queued", "running", "canceling") if active else ()
        rows = queries.list_jobs(conn, states=states)
        # Never expose spec_yaml here (large, and the source for password_env names).
        return JSONResponse([{k: r[k] for k in _JOB_FIELDS} for r in rows])

    @app.get("/api/jobs/{job_id}")
    def api_job_detail(job_id: int, conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        job = queries.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        out = {k: job[k] for k in _JOB_FIELDS}
        # For a prepare job, surface its data-load metrics (wall time, size, MB/s).
        if job["kind"] == "prepare":
            out["prepare_stats"] = harness_api.prepare_stats(job["spec_yaml"], cfg.results_dir)
        return JSONResponse(out)

    # ── JSON API: validate / dry-run ──
    @app.post("/api/validate")
    def api_validate(payload: dict,
                     user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        return JSONResponse(harness_api.validate_yaml(payload.get("spec_yaml", "")))

    @app.post("/api/dry-run")
    def api_dry_run(payload: dict, user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        try:
            return JSONResponse(harness_api.dry_run(payload.get("spec_yaml", "")))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"invalid spec: {exc}")

    # ── start a run/soak ──
    @app.post("/api/runs")
    def api_start_run(request: Request, payload: dict,
                      conn: sqlite3.Connection = Depends(get_conn),
                      user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        clean_yaml, target_id = _spec_with_target(conn, payload)
        v = harness_api.validate_yaml(clean_yaml)
        if not v.get("ok"):
            raise HTTPException(400, v.get("error", "invalid spec"))
        kind = "soak" if v["mode"] == "soak" else "run"
        job_id = queries.enqueue_job(conn, kind, clean_yaml, target_id, user["username"],
                                     scheduled_utc=payload.get("scheduled_utc") or None)
        password = payload.get("password")
        if password:
            store.set(job_password_ref(job_id), password)  # encrypted, off-DB
        queries.audit(conn, user["username"], "run_enqueue", target=v["label"],
                      detail=f"job={job_id} kind={kind}")
        return JSONResponse({"job_id": job_id, "kind": kind})

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel(job_id: int, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn),
                   user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, request.headers.get("x-csrf-token"))
        ok = stop_job_process(cfg, conn, job_id)   # one escalating path (SIGTERM->SIGKILL)
        queries.audit(conn, user["username"], "run_cancel", target=str(job_id))
        return JSONResponse({"canceled": ok})

    @app.post("/api/jobs/{job_id}/stop")
    def api_stop(job_id: int, request: Request,
                 conn: sqlite3.Connection = Depends(get_conn),
                 user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        """Stop an active run: graceful SIGTERM to the process group, escalating to
        SIGKILL after stop_grace_s. The run converges to 'canceled' when the child
        dies (no run stays 'live' after a stop)."""
        _check_csrf(request, request.headers.get("x-csrf-token"))
        ok = stop_job_process(cfg, conn, job_id)
        queries.audit(conn, user["username"], "run_stop", target=str(job_id),
                      detail="sigterm+escalate")
        return JSONResponse({"stopping": ok})

    @app.post("/api/runs/{run_id}/mark")
    def api_mark(run_id: str, request: Request, payload: dict,
                 conn: sqlite3.Connection = Depends(get_conn),
                 user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        run_dir = cfg.results_dir / run_id
        if not (run_dir / "manifest.json").exists():
            raise HTTPException(404, "run not found")
        etype = payload.get("type", "note")
        harness_api.mark_event(run_dir, etype, payload.get("label", ""), payload.get("note", ""))
        queries.audit(conn, user["username"], "soak_mark", target=run_id, detail=etype)
        return JSONResponse({"marked": etype})

    @app.post("/api/runs/{run_id}/resume")
    def api_resume(run_id: str, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn),
                   user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, request.headers.get("x-csrf-token"))
        run_dir = cfg.results_dir / run_id
        spec_path = run_dir / "spec.yaml"
        if not spec_path.exists():
            raise HTTPException(404, "run/spec not found")
        job_id = queries.enqueue_job(conn, "run", spec_path.read_text(), None,
                                     user["username"], resume_run_id=run_id)
        queries.audit(conn, user["username"], "run_resume", target=run_id, detail=f"job={job_id}")
        return JSONResponse({"job_id": job_id})

    @app.post("/api/runs/{run_id}/rerun")
    def api_rerun(run_id: str, request: Request,
                  conn: sqlite3.Connection = Depends(get_conn),
                  store: SecretStore = Depends(get_store),
                  user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, request.headers.get("x-csrf-token"))
        run_dir = cfg.results_dir / run_id
        spec_path = run_dir / "spec.yaml"
        if not spec_path.exists():
            raise HTTPException(404, "run/spec not found")
        kind = "soak" if _run_mode(run_dir) == "soak" else "run"
        # Reuse the original run's saved target so the password needn't be re-entered.
        prev = queries.job_for_run(conn, run_id)
        target_id = prev["target_id"] if prev else None
        has_pw = False
        if target_id:
            tgt = queries.get_target(conn, target_id)
            has_pw = bool(tgt and store.get(tgt["password_ref"]))
        job_id = queries.enqueue_job(conn, kind, spec_path.read_text(encoding="utf-8"),
                                     target_id, user["username"])
        queries.audit(conn, user["username"], "run_rerun", target=run_id, detail=f"job={job_id}")
        return JSONResponse({"job_id": job_id, "kind": kind, "needs_password": not has_pw})

    # ── targets (saved clusters: connection + persistent encrypted password) ──
    @app.get("/api/targets")
    def api_targets(conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        # Never returns the password — only the reference lives in the DB anyway.
        return JSONResponse([dict(r) for r in queries.list_targets(conn)])

    @app.post("/api/targets")
    def api_create_target(request: Request, payload: dict,
                          conn: sqlite3.Connection = Depends(get_conn),
                          store: SecretStore = Depends(get_store),
                          user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        name = str(payload.get("name", "")).strip()
        host = str(payload.get("host", "")).strip()
        if not name or not host:
            raise HTTPException(400, "name and host are required")
        ref = f"target:{name}:password"
        password = payload.get("password") or ""
        if password:
            store.set(ref, password)  # encrypted, off-DB; only the ref is stored
        try:
            tid = queries.create_target(
                conn, name, host, int(payload.get("port") or 5432),
                str(payload.get("dbname", "")).strip() or "defaultdb",
                str(payload.get("dbuser", "")).strip() or "doadmin",
                str(payload.get("sslmode", "require")).strip() or "require", ref)
        except sqlite3.IntegrityError:
            raise HTTPException(400, "a target with that name already exists")
        queries.audit(conn, user["username"], "target_create", target=name, detail=host)
        return JSONResponse({"id": tid, "name": name})

    @app.delete("/api/targets/{target_id}")
    def api_delete_target(target_id: int, request: Request,
                          conn: sqlite3.Connection = Depends(get_conn),
                          store: SecretStore = Depends(get_store),
                          user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, request.headers.get("x-csrf-token"))
        tgt = queries.get_target(conn, target_id)
        if tgt is None:
            raise HTTPException(404, "target not found")
        queries.delete_target(conn, target_id)
        store.delete(tgt["password_ref"])
        queries.audit(conn, user["username"], "target_delete", target=tgt["name"])
        return JSONResponse({"deleted": True})

    @app.post("/api/targets/{target_id}")
    def api_update_target(target_id: int, request: Request, payload: dict,
                          conn: sqlite3.Connection = Depends(get_conn),
                          store: SecretStore = Depends(get_store),
                          user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        """Update a saved target's connection and/or rotate its password."""
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        tgt = queries.get_target(conn, target_id)
        if tgt is None:
            raise HTTPException(404, "target not found")
        fields = {k: payload[k] for k in ("host", "port", "dbname", "dbuser", "sslmode")
                  if k in payload and payload[k] not in (None, "")}
        if "port" in fields:
            try:
                fields["port"] = int(fields["port"])
            except (TypeError, ValueError):
                raise HTTPException(400, "port must be an integer")
        queries.update_target(conn, target_id, **fields)
        if payload.get("password"):                 # rotate credential, reusing the ref
            store.set(tgt["password_ref"], payload["password"])
        queries.audit(conn, user["username"], "target_update", target=tgt["name"],
                      detail=",".join(list(fields) + (["password"] if payload.get("password") else [])))
        return JSONResponse({"ok": True})

    # ── lifecycle tasks: preflight / prepare / doctor (live via the job queue) ──
    @app.post("/api/preflight")
    def api_preflight(request: Request, payload: dict,
                      conn: sqlite3.Connection = Depends(get_conn),
                      store: SecretStore = Depends(get_store),
                      user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        return _enqueue_task(request, payload, conn, store, user, "preflight")

    @app.post("/api/prepare")
    def api_prepare(request: Request, payload: dict,
                    conn: sqlite3.Connection = Depends(get_conn),
                    store: SecretStore = Depends(get_store),
                    user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        return _enqueue_task(request, payload, conn, store, user, "prepare")

    def _enqueue_task(request: Request, payload: dict, conn: sqlite3.Connection,
                      store: SecretStore, user: sqlite3.Row, kind: str) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        clean_yaml, target_id = _spec_with_target(conn, payload)
        v = harness_api.validate_yaml(clean_yaml)
        if not v.get("ok"):
            raise HTTPException(400, v.get("error", "invalid spec"))
        options = None
        if kind == "prepare":
            doc = yaml.safe_load(clean_yaml) or {}
            db = (doc.get("target") or {}).get("database", "")
            recreate = payload.get("recreate") or ""
            if recreate not in ("", "database", "tables"):
                raise HTTPException(400, "recreate must be 'database' or 'tables'")
            if recreate and str(payload.get("confirm", "")) != db:
                raise HTTPException(400, "type the exact database name to confirm a destructive recreate")
            opt = {"create_db": bool(payload.get("create_db")),
                   "recreate": recreate, "confirm": payload.get("confirm", "")}
            options = json.dumps(opt)
        job_id = queries.enqueue_job(conn, kind, clean_yaml, target_id, user["username"],
                                     options=options)
        password = payload.get("password")
        if password:
            store.set(job_password_ref(job_id), password)
        queries.audit(conn, user["username"], f"{kind}_enqueue", target=v["label"],
                      detail=f"job={job_id}" + (f" {payload.get('recreate')}" if payload.get("recreate") else ""))
        return JSONResponse({"job_id": job_id, "kind": kind})

    @app.get("/api/doctor")
    def api_doctor(user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        # Quick, no DB/password: harness version, git SHA, sysbench/psql availability.
        try:
            out = subprocess.run([cfg.harness_bin, "doctor"], capture_output=True,
                                 text=True, timeout=30)
            return JSONResponse({"text": (out.stdout or out.stderr).strip(), "ok": out.returncode == 0})
        except (OSError, subprocess.SubprocessError) as exc:
            return JSONResponse({"text": f"doctor failed: {exc}", "ok": False})

    @app.get("/api/jobs/{job_id}/stream")
    def job_stream(job_id: int, user: sqlite3.Row = Depends(require("viewer"))) -> StreamingResponse:
        return StreamingResponse(_job_sse(cfg, job_id), media_type="text/event-stream")

    # ── reports / artifacts ──
    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    def run_report(run_id: str, request: Request, regen: int = 0,
                   user: sqlite3.Row = Depends(require("viewer"))) -> Response:
        run_dir = cfg.results_dir / run_id
        if not (run_dir / "manifest.json").exists():
            raise HTTPException(404, "run not found")
        out = run_dir / harness_api.report_filename(run_dir)
        if regen or not out.exists():
            out = harness_api.generate_report(run_dir)
        return HTMLResponse(out.read_text(encoding="utf-8"))

    @app.get("/runs/{run_id}/report/download")
    def run_report_download(run_id: str, user: sqlite3.Row = Depends(require("viewer"))) -> Response:
        run_dir = cfg.results_dir / run_id
        out = run_dir / harness_api.report_filename(run_dir)
        if not out.exists():
            out = harness_api.generate_report(run_dir)
        return Response(out.read_text(encoding="utf-8"), media_type="text/html",
                        headers={"Content-Disposition": f'attachment; filename="{run_id}-{out.name}"'})

    @app.get("/api/runs/{run_id}/summary")
    def api_run_summary(run_id: str, user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        """Parsed run data for the interactive in-app report (manifest + summary)."""
        run_dir = cfg.results_dir / run_id
        if not (run_dir / "manifest.json").exists():
            raise HTTPException(404, "run not found")
        manifest = _manifest(run_dir)          # tolerant of a malformed manifest
        mode = manifest.get("mode", "sweep")
        sp = run_dir / "parsed" / ("soak_summary.json" if mode == "soak" else "summary.json")
        summary: dict = {}
        if sp.exists():
            try:
                summary = json.loads(sp.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                summary = {}
        return JSONResponse({"mode": mode, "manifest": manifest, "summary": summary,
                             "pg": (run_dir / "parsed" / "pg_timeseries.csv").exists(),
                             "pg_settings": _read_pg_settings(run_dir)})

    _CSV_FILES = {"samples": "parsed/samples.csv",
                  "timeseries": "parsed/soak_timeseries.csv",
                  "pg": "parsed/pg_timeseries.csv"}

    @app.get("/runs/{run_id}/csv")
    def run_csv(run_id: str, which: str = "samples",
                user: sqlite3.Row = Depends(require("viewer"))) -> Response:
        rel = _CSV_FILES.get(which)
        if rel is None:
            raise HTTPException(400, f"unknown csv '{which}'")
        p = cfg.results_dir / run_id / rel
        if not p.exists():
            raise HTTPException(404, "no such data for this run")
        return Response(p.read_text(encoding="utf-8"), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{run_id}-{which}.csv"'})

    @app.get("/runs/{run_id}/spec")
    def run_spec(run_id: str, user: sqlite3.Row = Depends(require("viewer"))) -> Response:
        p = cfg.results_dir / run_id / "spec.yaml"
        if not p.exists():
            raise HTTPException(404, "spec not found")
        return PlainTextResponse(p.read_text(encoding="utf-8"))

    @app.get("/runs/{run_id}/artifact")
    def run_artifact(run_id: str, user: sqlite3.Row = Depends(require("viewer"))) -> Response:
        run_dir = cfg.results_dir / run_id
        if not run_dir.exists():
            raise HTTPException(404, "run not found")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(run_dir, arcname=run_id)
        buf.seek(0)
        return Response(buf.read(), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{run_id}.tar.gz"'})

    @app.get("/runs/{run_id}/stream")
    def run_stream(run_id: str, conn: sqlite3.Connection = Depends(get_conn),
                   user: sqlite3.Row = Depends(require("viewer"))) -> StreamingResponse:
        run_dir = cfg.results_dir / run_id
        return StreamingResponse(_sse(cfg, run_dir), media_type="text/event-stream")

    @app.delete("/api/runs/{run_id}")
    def api_delete_run(run_id: str, request: Request,
                       conn: sqlite3.Connection = Depends(get_conn),
                       store: SecretStore = Depends(get_store),
                       user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        """Delete a run and reclaim its disk: the index row, results/<run_id>/, and
        ALL owning job rows + their spec/out files + encrypted password refs.
        Refuses while any owning job is still active (stop it first)."""
        _check_csrf(request, request.headers.get("x-csrf-token"))
        run_dir = _run_dir_safe(cfg, run_id)            # traversal guard
        jobs = queries.jobs_for_run(conn, run_id)
        if queries.get_run(conn, run_id) is None and not run_dir.exists() and not jobs:
            raise HTTPException(404, "run not found")
        if any(j["state"] in ("queued", "running", "canceling") for j in jobs):
            raise HTTPException(409, "run has an active job; stop it first")
        # Index/control plane first, bytes second: a crash mid-delete leaves
        # reclaimable filesystem garbage, never a dangling index row.
        queries.delete_jobs_for_run(conn, run_id)
        queries.delete_run(conn, run_id)
        for j in jobs:                                   # per-job secret + spec/out files
            try:
                store.delete(job_password_ref(j["id"]))   # never the shared target secret
            except Exception:  # noqa: BLE001
                pass
            for name in (f"job_{j['id']}.yaml", f"job_{j['id']}.out"):
                (cfg.data_dir / "jobs" / name).unlink(missing_ok=True)
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        queries.audit(conn, user["username"], "run_delete", target=run_id,
                      detail=f"jobs={[j['id'] for j in jobs]}")
        return JSONResponse({"deleted": True})

    # ── compare ──
    @app.get("/compare")
    def compare_to_console() -> Response:
        return RedirectResponse("/ui/compare", status_code=307)

    @app.get("/compare/view", response_class=HTMLResponse)
    def compare_view(runs: str, user: sqlite3.Row = Depends(require("viewer"))) -> Response:
        ids = [_safe_segment(r) for r in runs.split(",") if r]
        dirs = [cfg.results_dir / r for r in ids]
        for d in dirs:
            if not (d / "manifest.json").exists():
                raise HTTPException(404, f"run not found: {d.name}")
        out = cfg.data_dir / "tmp"
        out.mkdir(parents=True, exist_ok=True)
        path = harness_api.compare(dirs, out / f"compare-{'-'.join(ids)[:80]}.html")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    # ── admin: users / audit (legacy paths redirect into the console) ──
    @app.get("/admin/users")
    def users_to_console() -> Response:
        return RedirectResponse("/ui/users", status_code=307)

    @app.post("/admin/users")
    def users_create(request: Request, username: str = Form(...), password: str = Form(...),
                     role: str = Form("viewer"), csrf_token: str = Form(""),
                     conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("admin"))) -> Response:
        _check_csrf(request, csrf_token)
        if role not in ROLE_RANK:
            raise HTTPException(400, "bad role")
        try:
            queries.create_user(conn, username, hash_password(password), role)
        except sqlite3.IntegrityError:
            raise HTTPException(400, "user exists")
        queries.audit(conn, user["username"], "user_create", target=username, detail=role)
        return RedirectResponse("/admin/users", status_code=303)

    @app.get("/audit")
    def audit_to_console() -> Response:
        return RedirectResponse("/ui/audit", status_code=307)

    @app.get("/audit/export.csv")
    def audit_export(conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("admin"))) -> Response:
        import csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts_utc", "username", "action", "target", "detail"])
        for r in queries.list_audit(conn, limit=100000):
            w.writerow([r["ts_utc"], r["username"], r["action"], r["target"], r["detail"]])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="audit.csv"'})

    @app.get("/admin/settings")
    def settings_to_console() -> Response:
        return RedirectResponse("/ui/settings", status_code=307)

    # ── JSON admin APIs (consumed by the SPA Users/Audit/Settings pages) ──
    @app.get("/api/users")
    def api_users(conn: sqlite3.Connection = Depends(get_conn),
                  user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        return JSONResponse([dict(r) for r in queries.list_users(conn)])

    @app.post("/api/users")
    def api_create_user2(request: Request, payload: dict,
                         conn: sqlite3.Connection = Depends(get_conn),
                         user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        username = str(payload.get("username", "")).strip()
        password = payload.get("password") or ""
        role = str(payload.get("role", "viewer"))
        if not username or not password:
            raise HTTPException(400, "username and password are required")
        if role not in ROLE_RANK:
            raise HTTPException(400, "bad role")
        try:
            queries.create_user(conn, username, hash_password(password), role)
        except sqlite3.IntegrityError:
            raise HTTPException(400, "a user with that name already exists")
        queries.audit(conn, user["username"], "user_create", target=username, detail=role)
        return JSONResponse({"ok": True})

    @app.post("/api/users/{username}")
    def api_update_user(username: str, request: Request, payload: dict,
                        conn: sqlite3.Connection = Depends(get_conn),
                        user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        if queries.get_user(conn, username) is None:
            raise HTTPException(404, "user not found")
        if username == user["username"] and (payload.get("disabled")
                                             or payload.get("role") not in (None, "admin")):
            raise HTTPException(400, "you can't disable or demote your own admin account")
        if "role" in payload:
            if payload["role"] not in ROLE_RANK:
                raise HTTPException(400, "bad role")
            queries.set_user_role(conn, username, payload["role"])
        if "disabled" in payload:
            queries.set_user_disabled(conn, username, bool(payload["disabled"]))
        if payload.get("password"):
            queries.set_user_password(conn, username, hash_password(payload["password"]))
        queries.audit(conn, user["username"], "user_update", target=username,
                      detail=",".join(k for k in ("role", "disabled", "password") if k in payload))
        return JSONResponse({"ok": True})

    @app.get("/api/audit")
    def api_audit(limit: int = 500, conn: sqlite3.Connection = Depends(get_conn),
                  user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        rows = queries.list_audit(conn, limit=min(max(limit, 1), 5000))
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/admin/settings")
    def api_admin_settings(conn: sqlite3.Connection = Depends(get_conn),
                           user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        return JSONResponse({
            "notify": notify.get_config(conn),
            "base_url": queries.get_setting(conn, "base_url", ""),
            "do_cluster_id": queries.get_setting(conn, "do_cluster_id", ""),
            "max_concurrency": int(queries.get_setting(conn, "max_concurrency", "1") or 1),
            "has_smtp_pw": bool(store.get(notify.SMTP_PASSWORD_REF)),
            "has_slack": bool(store.get(notify.SLACK_WEBHOOK_REF)),
            "has_do_token": bool(store.get(provider.DO_TOKEN_REF))})

    @app.post("/api/admin/settings")
    def api_admin_settings_save(request: Request, payload: dict,
                                conn: sqlite3.Connection = Depends(get_conn),
                                user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        smtp = payload.get("smtp") or {}
        slack = payload.get("slack") or {}
        try:
            port = int(smtp.get("port") or 587)
        except (TypeError, ValueError):
            port = 587
        notify.set_config(conn, {
            "smtp": {"host": str(smtp.get("host", "")), "port": port,
                     "user": str(smtp.get("user", "")), "from": str(smtp.get("from", "")),
                     "to": str(smtp.get("to", "")), "tls": bool(smtp.get("tls", True))},
            "slack": {"enabled": bool(slack.get("enabled"))}})
        queries.set_setting(conn, "base_url", str(payload.get("base_url", "")))
        queries.set_setting(conn, "do_cluster_id", str(payload.get("do_cluster_id", "")))
        if payload.get("max_concurrency") is not None:
            try:
                mc = max(1, min(16, int(payload["max_concurrency"])))
                queries.set_setting(conn, "max_concurrency", str(mc))
            except (TypeError, ValueError):
                raise HTTPException(400, "max_concurrency must be an integer 1–16")
        # Secrets only updated when a new value is supplied (blank leaves as-is).
        if payload.get("smtp_password"):
            store.set(notify.SMTP_PASSWORD_REF, payload["smtp_password"])
        if payload.get("slack_webhook"):
            store.set(notify.SLACK_WEBHOOK_REF, payload["slack_webhook"])
        if payload.get("do_api_token"):
            store.set(provider.DO_TOKEN_REF, payload["do_api_token"])
        queries.audit(conn, user["username"], "settings_update", detail="via console settings")
        return JSONResponse({"ok": True})

    @app.post("/api/notify/test")
    def notify_test(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("admin"))) -> JSONResponse:
        _check_csrf(request, request.headers.get("x-csrf-token"))
        sent = notify.notify(conn, store, state="test", run_id=None,
                             label="notification test", peak_qps=None)
        return JSONResponse({"sent": sent})

    # ── config templates (versioned) + spec diff ──
    @app.post("/api/templates")
    def template_save(request: Request, payload: dict,
                      conn: sqlite3.Connection = Depends(get_conn),
                      user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD) or request.headers.get("x-csrf-token"))
        name, spec_yaml = payload.get("name", "").strip(), payload.get("spec_yaml", "")
        if not name:
            raise HTTPException(400, "template name required")
        v = harness_api.validate_yaml(spec_yaml)
        if not v.get("ok"):
            raise HTTPException(400, v.get("error", "invalid spec"))
        ver = queries.save_template(conn, name, spec_yaml, user["username"])
        queries.audit(conn, user["username"], "template_save", target=name, detail=f"v{ver}")
        return JSONResponse({"name": name, "version": ver})

    @app.get("/api/templates")
    def templates_list(conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        return JSONResponse([{"name": r["name"], "version": r["version"]}
                             for r in queries.list_templates(conn)])

    @app.get("/api/templates/{name}")
    def template_get(name: str, conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        row = queries.get_template(conn, name)
        if row is None:
            raise HTTPException(404, "template not found")
        return JSONResponse({"name": row["name"], "version": row["version"],
                             "spec_yaml": row["spec_yaml"]})

    def _spec_text(conn: sqlite3.Connection, ref: str) -> str:
        """Resolve a diff ref to spec YAML. ref = run_id or template:NAME."""
        if ref.startswith("template:"):
            row = queries.get_template(conn, ref.split(":", 1)[1])
            if row is None:
                raise HTTPException(404, f"template not found: {ref}")
            return str(row["spec_yaml"])
        p = cfg.results_dir / _safe_segment(ref) / "spec.yaml"
        if not p.exists():
            raise HTTPException(404, f"spec not found: {ref}")
        return p.read_text(encoding="utf-8")

    @app.get("/api/diff")
    def spec_diff(a: str, b: str, conn: sqlite3.Connection = Depends(get_conn),
                  user: sqlite3.Row = Depends(require("viewer"))) -> Response:
        diff = difflib.unified_diff(_spec_text(conn, a).splitlines(),
                                    _spec_text(conn, b).splitlines(),
                                    fromfile=a, tofile=b, lineterm="")
        return PlainTextResponse("\n".join(diff) or "(specs are identical)")

    # ── provider (DigitalOcean) metrics for a run window ──
    @app.get("/runs/{run_id}/provider-metrics")
    def run_provider_metrics(run_id: str, conn: sqlite3.Connection = Depends(get_conn),
                             user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        run_dir = cfg.results_dir / run_id
        cached = run_dir / "env" / "provider_metrics.json"
        if cached.exists():
            try:
                return JSONResponse(json.loads(cached.read_text()))
            except (ValueError, OSError):
                pass   # fall through and refetch
        if not provider.configured(conn, store):
            return JSONResponse({"available": False,
                                 "reason": "no DO token/cluster configured (engine-side only)"})
        man = run_dir / "manifest.json"
        if not man.exists():
            raise HTTPException(404, "run not found")
        m = json.loads(man.read_text())
        data = provider.fetch_metrics(conn, store, queries.get_setting(conn, "do_cluster_id", ""),
                                      _epoch(m.get("created_utc")), _epoch(m.get("finished_utc")))
        if data is None:
            return JSONResponse({"available": False, "reason": "provider fetch failed"})
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(data))
        return JSONResponse(data)

    # ── SPA shell (served under /ui/*; assets via the /static mount) ──
    # The shell loads unauthenticated and bootstraps via /api/me, which 401s to
    # /login when there's no session — standard SPA auth, no secrets in the shell.
    _spa_index = _PKG / "static" / "spa" / "index.html"

    def _serve_spa() -> HTMLResponse:
        if _spa_index.exists():
            return HTMLResponse(_spa_index.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>pgbench console</title>"
            "<body style='font-family:system-ui;max-width:40rem;margin:4rem auto'>"
            "<h1>Console not built</h1><p>The SPA bundle is missing. Build it with "
            "<code>npm --prefix frontend ci &amp;&amp; npm --prefix frontend run build</code> "
            "or install a release that ships the built assets. The classic UI remains at "
            "<a href='/'>/</a>.</p>", status_code=200)

    @app.get("/ui", response_class=HTMLResponse)
    def spa_root() -> HTMLResponse:
        return _serve_spa()

    @app.get("/ui/{path:path}", response_class=HTMLResponse)
    def spa_path(path: str) -> HTMLResponse:
        return _serve_spa()


def _sse(cfg: Config, run_dir: Path, max_ticks: int = 6 * 3600) -> Iterator[str]:
    """Server-sent events for the live cockpit.

    Emits ``hello`` once, then incremental ``log`` (byte offset) and ``samples``
    (row offset — only *new* per-second rows, not a re-send each tick) plus a
    ``progress`` heartbeat, until the run reaches a terminal status. On
    EventSource auto-reconnect a fresh generator starts at offset 0, and the
    ``offset`` field tells the client to reset its buffers and catch up cleanly.
    """
    log = run_dir / "harness.log"
    sent_log = 0
    sent_rows = 0
    pg_sent = 0
    cur_file: Optional[str] = None
    budget_s = _planned_budget_s(run_dir)
    yield _event("hello", {"run_id": run_dir.name, "mode": _run_mode(run_dir),
                           "status": _run_status(run_dir), "budget_s": budget_s})
    for _ in range(max_ticks):
        if log.exists():
            chunk, sent_log = _read_tail(log, sent_log)   # byte offset; incremental
            if chunk:
                yield _event("log", chunk)
        rel, header, data = _read_samples(run_dir)
        if rel is not None:
            if rel != cur_file:          # first/swapped file -> client resets
                cur_file, sent_rows = rel, 0
            if len(data) > sent_rows:
                yield _event("samples", {"file": rel, "header": header,
                                         "offset": sent_rows, "rows": data[sent_rows:]})
                sent_rows = len(data)
        pg_header, pg_data = _read_csv(run_dir / "parsed" / "pg_timeseries.csv")
        if pg_header and len(pg_data) > pg_sent:
            yield _event("pg", {"header": pg_header, "offset": pg_sent, "rows": pg_data[pg_sent:]})
            pg_sent = len(pg_data)
        yield _event("progress", _progress(run_dir, budget_s))
        status = _run_status(run_dir)
        if status in ("complete", "partial", "failed", "canceled"):
            yield _event("done", {"status": status})
            return
        time.sleep(1)


def _event(name: str, data: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _safe_segment(ref: str) -> str:
    """Validate a run-id used to build a filesystem path (no traversal).

    Used for query-param ids (path params are already constrained to one segment
    by Starlette, but query params are not). Rejects empty, separators, leading
    dots, and ``..`` so ``results_dir / ref`` can never escape the tree.
    """
    if not ref or "/" in ref or "\\" in ref or ".." in ref or ref.startswith("."):
        raise HTTPException(400, f"invalid id: {ref!r}")
    return ref


def _read_pg_settings(run_dir: Path) -> Optional[dict]:
    """Captured pg_settings (env/pg_settings.csv) split into the curated key WAL/
    checkpoint/memory settings + the full list (for a 'show all' expand) so the
    interactive report reaches parity with the classic one. None if not captured."""
    import csv as _csv
    p = run_dir / "env" / "pg_settings.csv"
    if not p.exists():
        return None
    rows: list[dict] = []
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                rows.append({"name": r.get("name", ""), "setting": r.get("setting", ""),
                             "unit": (r.get("unit") or ""), "source": (r.get("source") or "")})
    except OSError:
        return None
    keyset = set(KEY_SETTINGS)
    return {"key": [r for r in rows if r["name"] in keyset], "all": rows}


def _run_dir_safe(cfg: Config, run_id: str) -> Path:
    """Resolve results/<run_id> with traversal protection: reject separators/'..'/
    leading dot AND assert the resolved path stays strictly under results_dir."""
    seg = _safe_segment(run_id)
    base = cfg.results_dir.resolve()
    target = (base / seg).resolve()
    if target == base or base not in target.parents:
        raise HTTPException(400, f"invalid run id: {run_id!r}")
    return target


def _spec_with_target(conn: sqlite3.Connection, payload: dict) -> tuple[str, Optional[int]]:
    """Merge a saved target's connection into the spec and normalize password_env.

    A saved target is authoritative for the connection (and supplies the
    persistent password); the password itself never enters the spec. Shared by
    run/soak/preflight/prepare enqueue paths.
    """
    doc = yaml.safe_load(payload.get("spec_yaml", "")) or {}
    if not isinstance(doc, dict):
        raise HTTPException(400, "spec must be a YAML mapping")
    doc.setdefault("target", {})
    target_id = payload.get("target_id")
    if target_id:
        tgt = queries.get_target(conn, int(target_id))
        if tgt is None:
            raise HTTPException(400, "unknown target")
        doc["target"].update(host=tgt["host"], port=tgt["port"], database=tgt["dbname"],
                             user=tgt["dbuser"], sslmode=tgt["sslmode"])
    doc["target"]["password_env"] = "PGB_TARGET_PASSWORD"
    return yaml.safe_dump(doc, sort_keys=False), (int(target_id) if target_id else None)


def _job_sse(cfg: Config, job_id: int, max_ticks: int = 2 * 3600) -> Iterator[str]:
    """Stream a task job's captured output (preflight/prepare/doctor) line-by-line.

    Each new line is emitted as a ``check`` event when it parses as a structured
    preflight event, otherwise as a ``log`` line. Ends on terminal job state.
    """
    out = cfg.data_dir / "jobs" / f"job_{job_id}.out"
    conn = connect(cfg.db_path)
    try:
        lines_sent = 0
        for _ in range(max_ticks):
            if out.exists():
                lines = out.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines[lines_sent:]:
                    obj = None
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict) and "status" in obj and "name" in obj:
                        yield _event("check", obj)
                    else:
                        yield _event("log", line + "\n")
                lines_sent = len(lines)
            job = queries.get_job(conn, job_id)
            state = job["state"] if job else "failed"
            if state in ("done", "failed", "canceled"):
                yield _event("done", {"status": state})
                return
            time.sleep(1)
    finally:
        conn.close()


def _epoch(iso: Optional[str]) -> int:
    """Parse a UTC ISO string (second precision) to epoch seconds; 0 if absent."""
    if not iso:
        return 0
    try:
        return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def _manifest(run_dir: Path) -> dict:
    try:
        return dict(json.loads((run_dir / "manifest.json").read_text()))
    except (OSError, ValueError):
        return {}


def _run_status(run_dir: Path) -> str:
    return str(_manifest(run_dir).get("status", ""))


def _run_mode(run_dir: Path) -> str:
    return str(_manifest(run_dir).get("mode", "sweep"))


def _planned_budget_s(run_dir: Path) -> int:
    """Planned wall-clock budget from the spec (for live ETA); 0 if unknown."""
    spec = run_dir / "spec.yaml"
    if not spec.exists():
        return 0
    try:
        return int(harness_api.dry_run(spec.read_text(encoding="utf-8")).get("budget_s", 0))
    except Exception:  # noqa: BLE001  (ETA is best-effort, never breaks the stream)
        return 0


def _progress(run_dir: Path, budget_s: int) -> dict:
    """Live progress snapshot: status, elapsed, budget, and level completion."""
    m = _manifest(run_dir)
    status = str(m.get("status", ""))
    created = _epoch(m.get("created_utc"))
    if status in ("complete", "partial", "failed", "canceled"):
        elapsed = int(m.get("wall_time_s") or 0)
    else:
        now = int(datetime.now(timezone.utc).timestamp())
        elapsed = max(0, now - created) if created else 0
    levels = m.get("levels") or []
    done = sum(1 for lv in levels if lv.get("status") in ("ok", "failed"))
    current = next((f"{lv.get('threads')}t" for lv in levels if lv.get("status") == "running"), "")
    return {"status": status, "elapsed_s": elapsed, "budget_s": budget_s,
            "levels_total": len(levels), "levels_done": done, "current": current}


def _read_tail(path: Path, offset: int) -> tuple[str, int]:
    """Read complete new lines past *offset* bytes (incremental log streaming).

    Returns (text, new_offset). Avoids re-reading the whole (potentially huge,
    multi-hour) log each tick; only emits up to the last newline so a partial
    in-progress line waits for its terminator.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return "", offset
    nl = data.rfind(b"\n")
    if nl == -1:
        return "", offset
    consumed = data[: nl + 1]
    return consumed.decode("utf-8", "replace"), offset + len(consumed)


def _read_csv(path: Path) -> tuple[str, list[str]]:
    """Return (header, data_rows) for a CSV file, or ('', []) if absent/empty."""
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > 1:
            return lines[0], lines[1:]
    return "", []


def _read_samples(run_dir: Path) -> tuple[Optional[str], str, list[str]]:
    """Return (relpath, header, data_rows) for the active samples file, or (None,'',[])."""
    for rel in ("parsed/soak_timeseries.csv", "parsed/samples.csv"):
        p = run_dir / rel
        if p.exists():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > 1:
                return rel, lines[0], lines[1:]
    return None, "", []
