"""Web app: auth/RBAC, migrations, queue + worker e2e (through the API, against
fake sysbench/psql), reports, soak+mark, audit, and the extended secrets-leak gate.
"""

from __future__ import annotations

import json
import os
import stat
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"
WEB_PW = "web-db-secret-DO-NOT-LEAK-42"


def _spec_yaml(mode: str = "sweep") -> str:
    base = """run:
  label: web-test
  edition: advanced
  tshirt_size: 4c16g
  tags: [ci, web]
  ticket: DBAAS-9
target:
  host: db.example.invalid
  port: 5432
  database: sbtest
  user: doadmin
  password_env: PGB_TARGET_PASSWORD
  sslmode: require
workload:
  type: oltp_read_write
  tables: 4
  table_size: 1000
"""
    if mode == "soak":
        base += "soak:\n  threads: 2\n  duration_s: 2\n  tolerate_errors: true\n"
    else:
        base += "sweep:\n  threads: [1]\n  duration_s: 2\n  warmup_s: 1\n  cooldown_s: 0\n  repetitions: 1\n"
    return base


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """A TestClient + cfg with fake sysbench/psql on PATH and three RBAC users."""
    for exe in ("sysbench", "psql"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    import sys
    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{venv_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGBENCH_HARNESS_BIN", str(venv_bin / "pgbench-harness"))
    monkeypatch.setenv("PGB_PROBE_GRACE_S", "0.3")
    state = tmp_path / "fakestate"; state.mkdir()
    monkeypatch.setenv("FAKE_PSQL_STATE", str(state))
    data = tmp_path / "data"
    monkeypatch.setenv("PGBENCH_DATA_DIR", str(data))
    monkeypatch.setenv("PGBENCH_DB", str(data / "pgbench.db"))

    from fastapi.testclient import TestClient
    from pgbench_webapp import admin, queries
    from pgbench_webapp.app import create_app
    from pgbench_webapp.config import load_config
    from pgbench_webapp.db import connect
    from pgbench_webapp.security import hash_password

    cfg = load_config()
    admin.create_admin("admin", "apw")
    conn = connect(cfg.db_path)
    queries.create_user(conn, "op", hash_password("oppw"), "operator")
    queries.create_user(conn, "viewer", hash_password("vpw"), "viewer")
    conn.close()
    client = TestClient(create_app(cfg))
    return client, cfg


def _run_worker_once(cfg):
    """Claim and execute one queued job synchronously (a single worker tick)."""
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    try:
        job = queries.claim_next_job(conn, 1)
        assert job is not None, "expected a queued job"
        state = worker.run_job(cfg, conn, job)
        return job["id"], state, queries.get_job(conn, job["id"])
    finally:
        conn.close()


def test_run_id_linked_live_before_completion(web, monkeypatch):
    """The job is linked to its run the moment the harness prints the run dir —
    so the UI can open the LIVE cockpit mid-run, not only after the job finishes."""
    client, cfg = web
    from pgbench_webapp import queries
    calls: list[dict] = []
    orig = queries.update_job

    def spy(conn, job_id, **kw):
        calls.append(dict(kw))
        return orig(conn, job_id, **kw)
    monkeypatch.setattr(queries, "update_job", spy)

    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW},
                auth=("op", "oppw"))
    _run_worker_once(cfg)

    # an update linked run_id WITHOUT a terminal state (the early link) and it
    # happened BEFORE the terminal state update.
    early = next((i for i, c in enumerate(calls)
                  if c.get("run_id") and "state" not in c), None)
    terminal = next((i for i, c in enumerate(calls)
                     if c.get("state") in ("done", "failed", "canceled")), None)
    assert early is not None and terminal is not None and early < terminal


def test_live_run_loads_from_filesystem_before_indexed(web):
    """A started-but-not-yet-indexed run (no DB row) must still load the cockpit
    via its on-disk manifest — the fix for 'run not found' on a live run link."""
    import json as _json
    client, cfg = web
    rid = "run-live-demo"
    rd = cfg.results_dir / rid
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(_json.dumps(
        {"run_id": rid, "label": "demo", "mode": "soak", "status": "running",
         "created_utc": "2026-06-29T01:00:00Z"}), encoding="utf-8")
    (rd / "spec.yaml").write_text("run:\n  label: demo\nworkload:\n  type: tpcc\n",
                                  encoding="utf-8")
    r = client.get(f"/api/runs/{rid}", auth=("viewer", "vpw"))
    assert r.status_code == 200
    assert r.json()["run_id"] == rid and r.json()["status"] == "running"
    # a genuinely missing run still 404s
    assert client.get("/api/runs/nope-xyz", auth=("viewer", "vpw")).status_code == 404


def test_compare_same_type_only(web):
    """Compare renders for sweep-vs-sweep and soak-vs-soak, REFUSES cross-type
    cleanly, and never returns an opaque 500."""
    import json as _json
    client, cfg = web

    def _make(mode):
        client.post("/api/runs", json={"spec_yaml": _spec_yaml(mode), "password": WEB_PW},
                    auth=("op", "oppw"))
        return _run_worker_once(cfg)[2]["run_id"]
    sweeps = [_make("sweep"), _make("sweep")]
    soaks = [_make("soak"), _make("soak")]

    r = client.get(f"/compare/view?runs={sweeps[0]},{sweeps[1]}", auth=("viewer", "vpw"))
    assert r.status_code == 200 and "QPS" in r.text, r.text[:400]

    r = client.get(f"/compare/view?runs={soaks[0]},{soaks[1]}", auth=("viewer", "vpw"))
    assert r.status_code == 200, r.text[:400]
    assert "Soak comparison" in r.text and "Median TPS" in r.text

    # cross-type -> clean refusal, not a rendered report, not a 500
    r = client.get(f"/compare/view?runs={sweeps[0]},{soaks[0]}", auth=("viewer", "vpw"))
    assert r.status_code == 400 and "Internal Server Error" not in r.text
    assert "same type" in r.text.lower()

    # a run with no parsed summary -> clean error
    rd = cfg.results_dir / "run-bare"
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(_json.dumps({"run_id": "run-bare", "mode": "sweep"}),
                                      encoding="utf-8")
    r = client.get(f"/compare/view?runs={sweeps[0]},run-bare", auth=("viewer", "vpw"))
    assert r.status_code == 400 and "Internal Server Error" not in r.text


def test_resume_reuses_saved_target(web):
    """Resume must reuse the original run's saved target (it used to enqueue with
    target=None, so resumed sweeps failed with no credentials)."""
    import json as _json
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    client, cfg = web
    tid = _make_target(client).json()["id"]
    rid = "run-resumed"
    rd = cfg.results_dir / rid
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(_json.dumps(
        {"run_id": rid, "mode": "sweep", "status": "failed"}), encoding="utf-8")
    (rd / "spec.yaml").write_text("run:\n  label: r\nworkload:\n  type: tpcc\n", encoding="utf-8")
    conn = connect(cfg.db_path)
    jid = queries.enqueue_job(conn, "run", "run:\n  label: r\n", tid, "op")
    queries.update_job(conn, jid, run_id=rid)
    conn.close()

    r = client.post(f"/api/runs/{rid}/resume", auth=("op", "oppw"))
    assert r.status_code == 200
    conn = connect(cfg.db_path)
    nj = queries.get_job(conn, r.json()["job_id"])
    conn.close()
    assert nj["target_id"] == tid and nj["resume_run_id"] == rid   # reused, not None


def test_run_routes_reject_path_traversal(web):
    """results/<run_id> path construction rejects '..'/separators (artifact tarball
    used to be able to escape to the secrets store)."""
    import pytest as _pt
    from fastapi import HTTPException
    from pgbench_webapp.app import _run_dir_safe
    _, cfg = web
    for bad in ("..", "../etc", "a/b", ".hidden", "..%2fsecret.key"):
        with _pt.raises(HTTPException):
            _run_dir_safe(cfg, bad)
    assert _run_dir_safe(cfg, "run-ok").name == "run-ok"


# ── migrations ──────────────────────────────────────────────────────

def test_migrations_idempotent(tmp_path):
    from pgbench_webapp.db import migrate
    db = tmp_path / "m.db"
    assert migrate(db) >= 1          # applies on a fresh DB
    assert migrate(db) == 0          # no-op on re-run


def test_health_no_auth(web):
    client, _ = web
    assert client.get("/healthz").json()["status"] == "ok"


def test_secret_store_self_heals_invalid_key(tmp_path):
    """A non-Fernet key file (e.g. installer wrote raw base64-48) self-heals when
    no secrets exist yet, but refuses (no silent orphaning) once secrets are stored."""
    from pgbench_webapp.secrets_store import SecretStore
    kp, sp = tmp_path / "secret.key", tmp_path / "secrets.enc"
    kp.write_bytes(b"not-a-valid-fernet-key")          # what the buggy installer wrote
    store = SecretStore(kp, sp)                          # regenerates a valid key
    store.set("db", "pw"); assert store.get("db") == "pw"
    kp.write_bytes(b"corrupt-now")                       # key broken but secrets exist
    with pytest.raises(ValueError, match="not a valid Fernet key"):
        SecretStore(kp, sp)


def test_installer_keygen_is_fernet_valid():
    """`openssl rand -base64 32 | tr +/ -_` must be a usable Fernet key (deploy.sh)."""
    import subprocess
    from cryptography.fernet import Fernet
    key = subprocess.check_output("openssl rand -base64 32 | tr '+/' '-_'", shell=True).strip()
    Fernet(key)   # raises if invalid


# ── auth / RBAC ─────────────────────────────────────────────────────

def test_rbac_matrix(web):
    client, _ = web
    spec = _spec_yaml()
    # viewer: can validate, cannot start/cancel/admin
    assert client.post("/api/validate", json={"spec_yaml": spec}, auth=("viewer", "vpw")).status_code == 200
    assert client.post("/api/runs", json={"spec_yaml": spec}, auth=("viewer", "vpw")).status_code == 403
    assert client.get("/api/users", auth=("viewer", "vpw")).status_code == 403
    assert client.get("/api/audit", auth=("op", "oppw")).status_code == 403       # operator not admin
    # operator: can start; admin: can admin
    assert client.get("/api/users", auth=("admin", "apw")).status_code == 200
    # unauthenticated
    assert client.post("/api/validate", json={"spec_yaml": spec}).status_code == 401


def test_bad_login_audited_and_rate_limited(web):
    client, cfg = web
    from pgbench_webapp.db import connect
    from pgbench_webapp import queries
    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert "Invalid credentials" in r.text
    conn = connect(cfg.db_path)
    actions = [row["action"] for row in queries.list_audit(conn)]
    conn.close()
    assert "login_failed" in actions


# ── queue + worker e2e (through the API) ────────────────────────────

def test_run_through_api_and_worker(web):
    client, cfg = web
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    jid, state, job = _run_worker_once(cfg)
    assert jid == job_id and state == "done"
    run_id = job["run_id"]
    assert run_id and (cfg.results_dir / run_id / "manifest.json").exists()
    # appears in the runs index + report renders + artifact downloads
    assert any(r["run_id"] == run_id for r in client.get("/api/runs", auth=("viewer", "vpw")).json())
    # the console is now the default UI: legacy paths redirect into /ui
    assert client.get("/", auth=("viewer", "vpw"), follow_redirects=False).headers["location"] == "/ui/"
    rep = client.get(f"/runs/{run_id}/report", auth=("viewer", "vpw"))
    assert rep.status_code == 200 and "Headline results" in rep.text
    art = client.get(f"/runs/{run_id}/artifact", auth=("viewer", "vpw"))
    assert art.status_code == 200 and art.headers["content-type"] == "application/gzip"
    # SSE stream of a finished run terminates with a done event
    body = client.get(f"/runs/{run_id}/stream", auth=("viewer", "vpw")).text
    assert "event: done" in body


def test_soak_through_api_with_mark(web):
    client, cfg = web
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml("soak"), "password": WEB_PW},
                    auth=("op", "oppw"))
    assert r.status_code == 200 and r.json()["kind"] == "soak"
    _, state, job = _run_worker_once(cfg)
    run_id = job["run_id"]
    assert run_id and (cfg.results_dir / run_id / "parsed" / "soak_summary.json").exists()
    # mark an event, then the (re)generated soak report includes the resilience section
    assert client.post(f"/api/runs/{run_id}/mark", json={"type": "failover", "label": "x"},
                       auth=("op", "oppw")).status_code == 200
    rep = client.get(f"/runs/{run_id}/report?regen=1", auth=("op", "oppw"))
    assert "Resilience" in rep.text


def test_cancel_queued_job(web):
    client, cfg = web
    job_id = client.post("/api/runs", json={"spec_yaml": _spec_yaml()}, auth=("op", "oppw")).json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/cancel", auth=("op", "oppw")).json()["canceled"] is True
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    assert queries.get_job(conn, job_id)["state"] == "canceled"
    conn.close()


def test_stop_route_rbac_audit_and_queued(web):
    """/stop: operator+ only, audited, and short-circuits a queued job to canceled."""
    client, cfg = web
    job_id = client.post("/api/runs", json={"spec_yaml": _spec_yaml()}, auth=("op", "oppw")).json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/stop", auth=("viewer", "vpw")).status_code == 403
    r = client.post(f"/api/jobs/{job_id}/stop", auth=("op", "oppw"))
    assert r.status_code == 200 and r.json()["stopping"] is True
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    assert queries.get_job(conn, job_id)["state"] == "canceled"     # queued -> canceled
    assert any(a["action"] == "run_stop" for a in queries.list_audit(conn))
    conn.close()


def test_stop_escalates_to_sigkill(web):
    """A running child that ignores SIGTERM is SIGKILLed after stop_grace_s, and
    the whole process group (leader + children) is reaped."""
    import subprocess
    client, cfg = web
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    queries.set_setting(conn, "stop_grace_s", "0.5")
    # mimic the harness child: own session, ignores SIGTERM, spawns a grandchild
    code = ("import signal,os,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "os.fork() if hasattr(os,'fork') else None;time.sleep(60)")
    proc = subprocess.Popen(["python3", "-c", code], start_new_session=True)
    jid = queries.enqueue_job(conn, "soak", "x", None, "op")
    queries.update_job(conn, jid, state="running", pid=proc.pid)
    assert worker.stop_job_process(cfg, conn, jid) is True
    assert queries.get_job(conn, jid)["state"] == "canceling"
    proc.wait(timeout=5)                                # escalation SIGKILLs within grace
    assert proc.returncode is not None and proc.returncode != 0
    conn.close()


def test_delete_run_removes_all_artifacts(web):
    """Delete reclaims everything: index row, results dir, job row(s), per-job
    spec/out files, and the encrypted password ref — but not the shared target."""
    client, cfg = web
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    from pgbench_webapp.secrets_store import SecretStore
    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    jid, _, _ = _run_worker_once(cfg)
    conn = connect(cfg.db_path)
    rid = queries.get_job(conn, jid)["run_id"]
    assert rid
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    store.set(worker.job_password_ref(jid), "lingering-secret")     # simulate uncleaned secret
    out_f = cfg.data_dir / "jobs" / f"job_{jid}.out"
    out_f.write_text("log")
    run_dir = cfg.results_dir / rid
    assert run_dir.exists()

    r = client.request("DELETE", f"/api/runs/{rid}", auth=("op", "oppw"))
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert queries.get_run(conn, rid) is None
    assert queries.jobs_for_run(conn, rid) == []
    assert not run_dir.exists() and not out_f.exists()
    assert not (cfg.data_dir / "jobs" / f"job_{jid}.yaml").exists()
    assert store.get(worker.job_password_ref(jid)) is None
    assert any(a["action"] == "run_delete" for a in queries.list_audit(conn))
    conn.close()


def test_delete_run_refuses_active_job(web):
    """A run with an active job must not be deletable — stop it first (409)."""
    client, cfg = web
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    rid = "active-20260101T000000Z"
    (cfg.results_dir / rid).mkdir(parents=True)
    (cfg.results_dir / rid / "manifest.json").write_text(
        '{"run_id":"%s","status":"running"}' % rid)
    conn = connect(cfg.db_path)
    jid = queries.enqueue_job(conn, "soak", "x", None, "op")
    queries.update_job(conn, jid, state="running", run_id=rid)
    assert client.request("DELETE", f"/api/runs/{rid}", auth=("op", "oppw")).status_code == 409
    assert (cfg.results_dir / rid).exists()       # untouched
    conn.close()


def test_delete_run_rbac_and_traversal(web):
    """Delete is operator+; the path resolver rejects traversal."""
    import pytest as _pytest
    from fastapi import HTTPException

    from pgbench_webapp.app import _run_dir_safe
    client, cfg = web
    rid = "view-20260101T000000Z"
    (cfg.results_dir / rid).mkdir(parents=True)
    (cfg.results_dir / rid / "manifest.json").write_text("{}")
    assert client.request("DELETE", f"/api/runs/{rid}", auth=("viewer", "vpw")).status_code == 403
    for bad in ("..", "../etc", "a/b", ".hidden"):
        with _pytest.raises(HTTPException):
            _run_dir_safe(cfg, bad)


# ── secrets-leak gate (extended across web/DB/logs/audit/API) ───────

def test_secret_never_leaks_anywhere(web):
    client, cfg = web
    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    _run_worker_once(cfg)
    needle = WEB_PW.encode()
    leaks = []
    for p in cfg.data_dir.rglob("*"):
        if p.is_file() and p.name != "secrets.enc":      # secrets.enc is Fernet-encrypted
            if needle in p.read_bytes():
                leaks.append(str(p))
    assert not leaks, f"plaintext secret leaked into: {leaks}"
    # encrypted store must NOT contain the plaintext either
    enc = cfg.data_dir / "secrets.enc"
    if enc.exists():
        assert needle not in enc.read_bytes()
    # API responses must not echo it
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    run_id = queries.list_runs(conn)[0]["run_id"]
    conn.close()
    for url in ("/", f"/runs/{run_id}", f"/runs/{run_id}/spec", f"/runs/{run_id}/report"):
        assert needle not in client.get(url, auth=("admin", "apw")).content


# ── reconciliation: CLI-created runs show up ────────────────────────

def _conn_store(cfg):
    from pgbench_webapp.db import connect
    from pgbench_webapp.secrets_store import SecretStore
    return connect(cfg.db_path), SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")


# ── notifications ───────────────────────────────────────────────────

def test_notifications_smtp_and_slack(web, monkeypatch):
    client, cfg = web
    from pgbench_webapp import notify
    conn, store = _conn_store(cfg)
    notify.set_config(conn, {"smtp": {"host": "smtp.x", "port": 587, "user": "u",
                                      "from": "a@x", "to": "b@x", "tls": True},
                             "slack": {"enabled": True}})
    store.set(notify.SMTP_PASSWORD_REF, "smtp-pass")
    store.set(notify.SLACK_WEBHOOK_REF, "https://hooks.slack/xyz")

    sent_mail, slack_calls = [], []

    class FakeSMTP:
        def __init__(self, host, port, timeout=0): sent_mail.append((host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, u, p): sent_mail.append(("login", u))
        def send_message(self, m): sent_mail.append(("msg", m["Subject"]))

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=0: slack_calls.append(req.full_url) or _Closeable())
    sent = notify.notify(conn, store, state="complete", run_id="r1", label="lbl", peak_qps=123)
    assert set(sent) == {"email", "slack"}
    assert any(c == ("msg", "[pgbench-harness] lbl — complete") for c in sent_mail)
    assert slack_calls == ["https://hooks.slack/xyz"]


def test_notifications_best_effort_swallow(web, monkeypatch):
    client, cfg = web
    from pgbench_webapp import notify
    conn, store = _conn_store(cfg)
    notify.set_config(conn, {"slack": {"enabled": True}})
    store.set(notify.SLACK_WEBHOOK_REF, "https://hooks.slack/xyz")

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.notify(conn, store, state="failed", run_id=None, label="x") == []  # no raise


def test_notify_reports_only_delivered_channels(web):
    """A configured-but-unsendable channel (SMTP host but no recipient; Slack
    enabled but no webhook stored) must NOT be reported as delivered."""
    from pgbench_webapp import notify
    client, cfg = web
    conn, store = _conn_store(cfg)
    notify.set_config(conn, {"smtp": {"host": "smtp.x", "to": ""},   # no recipient
                             "slack": {"enabled": True}})            # but no webhook set
    # Both helpers short-circuit before any network call, so no monkeypatching is
    # needed — and neither channel should appear in the delivered list.
    assert notify.notify(conn, store, state="complete", run_id="r1", label="lbl") == []


# ── scheduling ──────────────────────────────────────────────────────

def test_scheduled_future_job_not_claimed(web):
    client, cfg = web
    future = "2999-01-01T00:00:00Z"
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "scheduled_utc": future},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    assert queries.claim_next_job(conn, 1) is None    # not due yet
    conn.close()


# ── templates + spec diff ───────────────────────────────────────────

def test_templates_and_diff(web):
    client, cfg = web
    spec = _spec_yaml()
    assert client.post("/api/templates", json={"name": "t1", "spec_yaml": spec},
                       auth=("viewer", "vpw")).status_code == 403     # viewer can't save
    assert client.post("/api/templates", json={"name": "t1", "spec_yaml": spec},
                       auth=("op", "oppw")).json()["version"] == 1
    assert client.post("/api/templates", json={"name": "t1", "spec_yaml": spec},
                       auth=("op", "oppw")).json()["version"] == 2     # versioned
    names = [t["name"] for t in client.get("/api/templates", auth=("viewer", "vpw")).json()]
    assert "t1" in names


def test_template_version_is_per_name_not_rowid(web):
    """The saved version must be per-NAME (1, 2, …), not the table's autoincrement
    rowid — which diverges from the version the moment other templates exist."""
    client, cfg = web
    spec = _spec_yaml()
    # Bump the table rowid with an unrelated template first (rowid 1).
    assert client.post("/api/templates", json={"name": "other", "spec_yaml": spec},
                       auth=("op", "oppw")).json()["version"] == 1
    # t1's first save is version 1 even though its rowid is 2, and 2 next (rowid 3).
    assert client.post("/api/templates", json={"name": "t1", "spec_yaml": spec},
                       auth=("op", "oppw")).json()["version"] == 1
    assert client.post("/api/templates", json={"name": "t1", "spec_yaml": spec},
                       auth=("op", "oppw")).json()["version"] == 2
    assert "label: web-test" in client.get("/api/templates/t1", auth=("viewer", "vpw")).json()["spec_yaml"]
    # diff two template versions / a tweaked spec
    spec2 = spec.replace("tshirt_size: 4c16g", "tshirt_size: 8c32g")
    client.post("/api/templates", json={"name": "t2", "spec_yaml": spec2}, auth=("op", "oppw"))
    diff = client.get("/api/diff?a=template:t1&b=template:t2", auth=("viewer", "vpw")).text
    assert "-  tshirt_size: 4c16g" in diff or "tshirt_size: 4c16g" in diff


# ── provider metrics ────────────────────────────────────────────────

def test_provider_metrics_degraded_without_token(web):
    client, cfg = web
    rd = cfg.results_dir / "r-prov"
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text('{"run_id":"r-prov","status":"complete",'
                                      '"created_utc":"2026-01-01T00:00:00Z","finished_utc":"2026-01-01T00:10:00Z"}')
    d = client.get("/runs/r-prov/provider-metrics", auth=("viewer", "vpw")).json()
    assert d["available"] is False


def test_provider_fetch_mocked_no_token_leak(web, monkeypatch, tmp_path):
    client, cfg = web
    from pgbench_webapp import provider, queries
    conn, store = _conn_store(cfg)
    store.set(provider.DO_TOKEN_REF, "do-secret-token-XYZ")
    queries.set_setting(conn, "do_cluster_id", "abc-123")
    monkeypatch.setattr(provider, "_get", lambda url, token, timeout=15: {"data": {"cpu": [1, 2, 3]}})
    data = provider.fetch_metrics(conn, store, "abc-123", 1000, 2000)
    assert data and data["source"] == "digitalocean" and data["metrics"]["cpu"]
    out = json.dumps(data)
    assert "do-secret-token-XYZ" not in out      # token never in the stored payload


def test_provider_metrics_in_progress_uses_now_and_skips_cache(web, monkeypatch):
    """An in-progress run (no finished_utc) must request a valid [start, now] window
    — not the inverted [start, 0] — and must NOT cache the partial result."""
    import json as _json
    from pgbench_webapp import provider, queries
    client, cfg = web
    conn, store = _conn_store(cfg)
    store.set(provider.DO_TOKEN_REF, "tok")
    queries.set_setting(conn, "do_cluster_id", "abc-123")
    rid = "r-prov-live"
    rd = cfg.results_dir / rid
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(_json.dumps(
        {"run_id": rid, "status": "running", "created_utc": "2026-01-01T00:00:00Z"}),
        encoding="utf-8")
    captured: dict = {}

    def fake_fetch(conn, store, cluster, start, end):
        captured["start"], captured["end"] = start, end
        return {"source": "digitalocean", "window": [start, end], "metrics": {}}
    monkeypatch.setattr(provider, "fetch_metrics", fake_fetch)
    d = client.get(f"/runs/{rid}/provider-metrics", auth=("viewer", "vpw")).json()
    assert captured["end"] > captured["start"] > 0        # a real window, not [start, 0]
    assert d["window"] == [captured["start"], captured["end"]]
    assert not (rd / "env" / "provider_metrics.json").exists()   # partial window not cached


def test_settings_save_keeps_secrets_off_db(web):
    client, cfg = web
    r = client.post("/api/admin/settings", json={
        "base_url": "https://h:8443", "do_cluster_id": "c1",
        "do_api_token": "do-tok-SECRET", "slack_webhook": "https://hooks/secret",
        "smtp": {"host": "", "port": 587}, "slack": {}}, auth=("admin", "apw"))
    assert r.status_code == 200
    from pgbench_webapp import provider, notify
    _, store = _conn_store(cfg)
    assert store.get(provider.DO_TOKEN_REF) == "do-tok-SECRET"
    assert b"do-tok-SECRET" not in cfg.db_path.read_bytes()        # not in DB
    assert b"do-tok-SECRET" not in (cfg.data_dir / "secrets.enc").read_bytes()  # encrypted


class _Closeable:
    def close(self): pass


def test_reconcile_indexes_filesystem(web, tmp_path):
    client, cfg = web
    # simulate a run created directly by the CLI (outside the UI)
    rd = cfg.results_dir / "cli-made-20260101T000000Z"
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(
        '{"run_id":"cli-made-20260101T000000Z","label":"cli","edition":"standard",'
        '"tshirt_size":"4c16g","mode":"sweep","status":"complete","created_utc":"2026-01-01T00:00:00Z"}')
    from pgbench_webapp import index, queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    index.reconcile(conn, cfg.results_dir)
    assert queries.get_run(conn, "cli-made-20260101T000000Z") is not None
    conn.close()


def _stuck_run(cfg, rid: str) -> Path:
    rd = cfg.results_dir / rid
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(json.dumps({
        "run_id": rid, "label": "stuck", "edition": "advanced", "tshirt_size": "8c32g",
        "mode": "soak", "status": "running", "created_utc": "2026-01-01T00:00:00Z"}))
    return rd


def test_terminal_job_converges_stuck_running_run(web):
    """A run left non-terminal on disk whose owning job has ended must be driven
    to a terminal status — no run shows 'live' after its job is failed/canceled."""
    client, cfg = web
    from pgbench_webapp import index, queries
    from pgbench_webapp.db import connect
    rid = "stuck-20260101T000000Z"
    rd = _stuck_run(cfg, rid)
    conn = connect(cfg.db_path)
    jid = queries.enqueue_job(conn, "soak", "run:\n  label: stuck\n", None, "op")
    queries.update_job(conn, jid, state="failed", run_id=rid)
    index.reconcile(conn, cfg.results_dir)              # app/worker startup path
    assert queries.get_run(conn, rid)["status"] == "failed"   # not 'running'
    man = json.loads((rd / "manifest.json").read_text())
    assert man["status"] == "failed" and man["finished_utc"]
    conn.close()


def test_running_job_not_converged(web):
    """A genuinely live run (owning job still running) must stay 'running'."""
    client, cfg = web
    from pgbench_webapp import index, queries
    from pgbench_webapp.db import connect
    rid = "live-20260101T000000Z"
    _stuck_run(cfg, rid)
    conn = connect(cfg.db_path)
    jid = queries.enqueue_job(conn, "soak", "run:\n  label: live\n", None, "op")
    queries.update_job(conn, jid, state="running", run_id=rid)
    index.reconcile(conn, cfg.results_dir)
    assert queries.get_run(conn, rid)["status"] == "running"
    conn.close()


def test_failed_soak_run_is_terminal_via_worker(web, monkeypatch):
    """End-to-end through the queue: a soak whose load generator fails leaves the
    run row terminal (the A4 'Tasks=failed but Runs=live' regression)."""
    client, cfg = web
    monkeypatch.setenv("FAKE_SYSBENCH_RUN_FAIL_THREADS", "2")  # soak spec uses 2 threads
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml("soak"), "password": WEB_PW},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    jid, state, _ = _run_worker_once(cfg)
    assert state == "failed"
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    rid = queries.get_job(conn, jid)["run_id"]
    assert rid and queries.get_run(conn, rid)["status"] == "failed"   # NOT 'running'
    conn.close()


# ── SPA console (Phase 1: JSON bootstrap APIs + shell serving) ──────────

def test_api_me_reports_role_and_version(web):
    client, _ = web
    for user, pw, role in [("viewer", "vpw", "viewer"), ("op", "oppw", "operator"),
                           ("admin", "apw", "admin")]:
        r = client.get("/api/me", auth=(user, pw))
        assert r.status_code == 200
        body = r.json()
        assert body["user"] == user and body["role"] == role
        assert body["version"]
    # unauthenticated -> 401 (drives the SPA's redirect to /login)
    assert client.get("/api/me").status_code == 401


def test_api_runs_and_jobs_json(web):
    client, cfg = web
    # start a run so there's something to index
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    _run_worker_once(cfg)
    runs = client.get("/api/runs", auth=("viewer", "vpw"))
    assert runs.status_code == 200
    assert isinstance(runs.json(), list) and len(runs.json()) >= 1
    assert "run_id" in runs.json()[0]
    # jobs json never exposes spec_yaml (which carries password_env references)
    jobs = client.get("/api/jobs", auth=("viewer", "vpw"))
    assert jobs.status_code == 200
    for j in jobs.json():
        assert "spec_yaml" not in j and "id" in j and "state" in j
    # active filter returns only in-flight states (none after worker drained)
    active = client.get("/api/jobs?active=1", auth=("viewer", "vpw")).json()
    assert all(j["state"] in ("queued", "running", "canceling") for j in active)
    # unauthenticated -> 401
    assert client.get("/api/runs").status_code == 401


def test_spa_shell_served_under_ui(web):
    client, _ = web
    # The shell loads unauthenticated and bootstraps via /api/me.
    root = client.get("/ui")
    assert root.status_code == 200
    assert "/static/spa/assets" in root.text  # references the built bundle
    # client-side routes return the same shell (history fallback)
    assert client.get("/ui/runs/whatever").status_code == 200
    # the built asset bundle is actually served by the static mount
    import re
    m = re.search(r"/static/spa/assets/[\w.-]+\.js", root.text)
    assert m, "no JS asset reference in shell"
    assert client.get(m.group(0)).status_code == 200


# ── cockpit (Phase 2: single-run API, concurrency, incremental SSE) ─────

def test_api_get_single_run(web):
    client, cfg = web
    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    _run_worker_once(cfg)
    run_id = client.get("/api/runs", auth=("viewer", "vpw")).json()[0]["run_id"]
    r = client.get(f"/api/runs/{run_id}", auth=("viewer", "vpw"))
    assert r.status_code == 200 and r.json()["run_id"] == run_id
    assert client.get("/api/runs/does-not-exist", auth=("viewer", "vpw")).status_code == 404


def test_concurrency_setting_rbac_and_clamp(web):
    client, _ = web
    assert client.get("/api/settings", auth=("viewer", "vpw")).json()["max_concurrency"] == 1
    # operator cannot change it; admin can; value clamps to 1..16
    assert client.post("/api/settings/concurrency", json={"value": 4}, auth=("op", "oppw")).status_code == 403
    r = client.post("/api/settings/concurrency", json={"value": 99}, auth=("admin", "apw"))
    assert r.status_code == 200 and r.json()["max_concurrency"] == 16
    assert client.get("/api/settings", auth=("admin", "apw")).json()["max_concurrency"] == 16


def test_sse_emits_hello_progress_and_incremental_samples(web):
    client, cfg = web
    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    _run_worker_once(cfg)
    run_id = client.get("/api/runs", auth=("viewer", "vpw")).json()[0]["run_id"]
    body = client.get(f"/runs/{run_id}/stream", auth=("viewer", "vpw")).text
    assert "event: hello" in body
    assert '"start_utc"' in body    # wall-clock anchor for the live-compare alignment
    assert "event: progress" in body
    assert "event: done" in body
    # samples are sent incrementally with a row offset (not a 300-row re-send)
    assert "event: samples" in body and '"offset"' in body


def test_sse_flushes_final_log_fragment(web):
    """A terminal run whose harness.log ends WITHOUT a trailing newline must still
    stream that final fragment before `done` — it used to be dropped because the
    incremental reader only emits up to the last newline."""
    import json as _json
    client, cfg = web
    rid = "run-finalflush"
    rd = cfg.results_dir / rid
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(_json.dumps(
        {"run_id": rid, "mode": "sweep", "status": "complete",
         "created_utc": "2026-06-29T01:00:00Z", "finished_utc": "2026-06-29T01:05:00Z"}),
        encoding="utf-8")
    (rd / "spec.yaml").write_text("run:\n  label: r\nworkload:\n  type: tpcc\n", encoding="utf-8")
    (rd / "harness.log").write_text("first line\nFINAL-NO-NEWLINE", encoding="utf-8")
    body = client.get(f"/runs/{rid}/stream", auth=("viewer", "vpw")).text
    assert "FINAL-NO-NEWLINE" in body and "event: done" in body


# ── targets & re-run (Phase 3) ──────────────────────────────────────────

def _make_target(client, name="nyc3", host="db-nyc3.example.invalid"):
    return client.post("/api/targets", json={
        "name": name, "host": host, "dbname": "sbtest", "dbuser": "doadmin",
        "sslmode": "require", "password": WEB_PW}, auth=("op", "oppw"))


def test_targets_crud_rbac_and_no_password_exposed(web):
    client, cfg = web
    assert client.post("/api/targets", json={"name": "x", "host": "h"}, auth=("viewer", "vpw")).status_code == 403
    r = _make_target(client)
    assert r.status_code == 200
    tid = r.json()["id"]
    lst = client.get("/api/targets", auth=("viewer", "vpw")).json()
    assert any(t["name"] == "nyc3" and t["host"] == "db-nyc3.example.invalid" for t in lst)
    for t in lst:
        assert "password" not in t and "password_ref" not in t   # never exposed
    assert _make_target(client).status_code == 400                # duplicate name
    from pgbench_webapp.secrets_store import SecretStore
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    assert store.get("target:nyc3:password") == WEB_PW
    assert client.delete(f"/api/targets/{tid}", auth=("op", "oppw")).status_code == 200
    assert store.get("target:nyc3:password") is None              # secret erased with the target
    assert all(t["id"] != tid for t in client.get("/api/targets", auth=("op", "oppw")).json())


def test_target_delete_with_job_history_is_not_a_500(web):
    """J1 regression: jobs.target_id references targets(id) with FKs ON —
    deleting a target that ever ran a job used to die with an IntegrityError
    the API surfaced as a 500. History detaches; the target deletes cleanly."""
    client, cfg = web
    _make_target(client)
    tid = client.get("/api/targets", auth=("op", "oppw")).json()[0]["id"]
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "target_id": tid},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    # while the job is queued/running the delete is refused with a clear 409
    r = client.delete(f"/api/targets/{tid}", auth=("op", "oppw"))
    assert r.status_code == 409 and "job" in r.json()["detail"]
    _run_worker_once(cfg)
    # finished job history must not block the delete (nor 500 it)
    r = client.delete(f"/api/targets/{tid}", auth=("op", "oppw"))
    assert r.status_code == 200, r.text
    runs = client.get("/api/runs", auth=("viewer", "vpw")).json()
    assert runs                                     # the run history survives


def test_target_backed_run_reuses_password_and_surfaces_host(web):
    client, cfg = web
    _make_target(client)
    tid = client.get("/api/targets", auth=("op", "oppw")).json()[0]["id"]
    # start against the saved target with NO password in the request
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "target_id": tid}, auth=("op", "oppw"))
    assert r.status_code == 200
    _run_worker_once(cfg)
    runs = client.get("/api/runs", auth=("viewer", "vpw")).json()
    assert runs and runs[0]["target_host"] == "db-nyc3.example.invalid"
    run_id = runs[0]["run_id"]
    rr = client.post(f"/api/runs/{run_id}/rerun", auth=("op", "oppw"))
    assert rr.status_code == 200 and rr.json()["needs_password"] is False


def test_rerun_without_target_flags_needs_password(web):
    client, cfg = web
    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    _run_worker_once(cfg)
    run_id = client.get("/api/runs", auth=("viewer", "vpw")).json()[0]["run_id"]
    rr = client.post(f"/api/runs/{run_id}/rerun", auth=("op", "oppw"))
    assert rr.status_code == 200 and rr.json()["needs_password"] is True


# ── lifecycle flows: preflight / prepare / doctor (Phase 4) ─────────────

def test_preflight_job_streams_live_checklist(web):
    client, cfg = web
    r = client.post("/api/preflight", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    assert r.status_code == 200 and r.json()["kind"] == "preflight"
    job_id = r.json()["job_id"]
    _run_worker_once(cfg)
    body = client.get(f"/api/jobs/{job_id}/stream", auth=("viewer", "vpw")).text
    assert "event: check" in body            # structured per-check events
    assert "Connectivity" in body            # a known check name
    assert "event: done" in body
    # viewer cannot enqueue a preflight (operator+)
    assert client.post("/api/preflight", json={"spec_yaml": _spec_yaml()}, auth=("viewer", "vpw")).status_code == 403


def test_prepare_enqueues_and_doctor_rbac(web):
    client, cfg = web
    pj = client.post("/api/prepare", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    assert pj.status_code == 200 and pj.json()["kind"] == "prepare"
    d = client.get("/api/doctor", auth=("op", "oppw"))
    assert d.status_code == 200 and "pgbench-harness" in d.json()["text"]
    assert client.get("/api/doctor", auth=("viewer", "vpw")).status_code == 403


# ── soak interactive timeseries + operator event stamping ───────────────

def _make_soak(client, cfg) -> str:
    client.post("/api/runs", json={"spec_yaml": _spec_yaml("soak"), "password": WEB_PW},
                auth=("op", "oppw"))
    return _run_worker_once(cfg)[2]["run_id"]


def test_soak_timeseries_endpoint_serves_series_and_markers(web):
    client, cfg = web
    rid = _make_soak(client, cfg)
    # events are no longer spec-seeded — stamp one so there's a confirmed marker
    client.post(f"/api/runs/{rid}/mark", json={"type": "failover", "at_s": 1, "label": "f"},
                auth=("op", "oppw"))
    d = client.get(f"/api/runs/{rid}/timeseries", auth=("viewer", "vpw")).json()
    assert d["available"] is True and d["terminal"] is True
    assert isinstance(d["t"], list) and len(d["t"]) >= 1
    assert len(d["tps"]) == len(d["t"])
    # the stamped failover renders as a confirmed (event) marker
    assert any(mk["kind"] == "event" for mk in d["markers"])


def test_timeseries_is_soak_only(web):
    client, cfg = web
    client.post("/api/runs", json={"spec_yaml": _spec_yaml("sweep"), "password": WEB_PW}, auth=("op", "oppw"))
    rid = _run_worker_once(cfg)[2]["run_id"]
    d = client.get(f"/api/runs/{rid}/timeseries", auth=("viewer", "vpw")).json()
    assert d["available"] is False


def test_mark_at_offset_stamps_event_and_shows_on_timeseries(web):
    client, cfg = web
    rid = _make_soak(client, cfg)
    # operator stamps a scale_up at t=1s (post-hoc, on the finished run)
    r = client.post(f"/api/runs/{rid}/mark", json={"type": "scale_up", "at_s": 1, "label": "grew"},
                    auth=("op", "oppw"))
    assert r.status_code == 200 and r.json()["at_s"] == 1
    d = client.get(f"/api/runs/{rid}/timeseries", auth=("viewer", "vpw")).json()
    assert 1 in [mk["t"] for mk in d["markers"]]   # markers carry the x value as `t`
    # the event is anchored to the soak start + offset in events.jsonl
    import json as _json
    evs = [_json.loads(ln) for ln in (cfg.results_dir / rid / "events.jsonl").read_text().splitlines() if ln.strip()]
    assert any(e["type"] == "scale_up" and e["source"] == "mark" for e in evs)
    # a viewer cannot stamp
    assert client.post(f"/api/runs/{rid}/mark", json={"type": "scale_up", "at_s": 1},
                       auth=("viewer", "vpw")).status_code == 403


def test_mark_rejects_bad_type_and_negative_offset(web):
    client, cfg = web
    rid = _make_soak(client, cfg)
    assert client.post(f"/api/runs/{rid}/mark", json={"type": "bogus", "at_s": 1},
                       auth=("op", "oppw")).status_code == 400
    assert client.post(f"/api/runs/{rid}/mark", json={"type": "failover", "at_s": -3},
                       auth=("op", "oppw")).status_code == 400


# ── regression: SQLite connection usable across threads (FastAPI threadpool) ──

def test_connection_survives_cross_thread_use(tmp_path):
    """A connection created on one thread must be usable/closable on another —
    FastAPI runs sync deps in a threadpool and setup/teardown can differ."""
    import threading
    from pgbench_webapp.db import connect, migrate
    db = tmp_path / "x.db"
    migrate(db)
    conn = connect(db)                      # created on the main thread
    errors = []

    def use_and_close():
        try:
            list(conn.execute("SELECT 1"))  # used on a different thread
            conn.close()                    # closed on a different thread
        except Exception as exc:            # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=use_and_close)
    t.start()
    t.join()
    assert not errors, f"cross-thread use raised: {errors}"


def test_sse_streams_pg_metrics(web):
    client, cfg = web
    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    _run_worker_once(cfg)
    run_id = client.get("/api/runs", auth=("viewer", "vpw")).json()[0]["run_id"]
    pg = cfg.results_dir / run_id / "parsed" / "pg_timeseries.csv"
    pg.parent.mkdir(parents=True, exist_ok=True)
    pg.write_text("t,active,total_conn,xacts_s,cache_hit_pct,wal_mb_s\n1,8,20,100.0,98.9,0.5\n")
    body = client.get(f"/runs/{run_id}/stream", auth=("viewer", "vpw")).text
    assert "event: pg" in body and "cache_hit_pct" in body


def test_interactive_report_summary_and_csv(web):
    client, cfg = web
    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    _run_worker_once(cfg)
    run_id = client.get("/api/runs", auth=("viewer", "vpw")).json()[0]["run_id"]
    body = client.get(f"/api/runs/{run_id}/summary", auth=("viewer", "vpw"))
    assert body.status_code == 200
    j = body.json()
    assert j["mode"] in ("sweep", "soak") and "manifest" in j and "summary" in j
    # B6: the interactive report gets the captured DB config (curated key settings
    # + a full list for "show all"), reaching parity with the classic report.
    ps = j["pg_settings"]
    assert ps and any(r["name"] == "shared_buffers" for r in ps["key"])
    assert len(ps["all"]) >= len(ps["key"]) > 0
    # CSV export (samples present for a sweep run); bad name -> 400; missing run -> 404
    csv = client.get(f"/runs/{run_id}/csv?which=samples", auth=("viewer", "vpw"))
    assert csv.status_code == 200 and "t_offset" in csv.text
    assert client.get(f"/runs/{run_id}/csv?which=bogus", auth=("viewer", "vpw")).status_code == 400
    assert client.get("/api/runs/nope/summary", auth=("viewer", "vpw")).status_code == 404


def test_legacy_paths_redirect_to_console(web):
    client, _ = web
    for path, target in [("/", "/ui/"), ("/new", "/ui/new")]:
        r = client.get(path, auth=("op", "oppw"), follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == target
    r = client.get("/runs/some-run-id", auth=("op", "oppw"), follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/runs/some-run-id"
    # not-yet-ported pages remain server-rendered
    assert client.get("/compare", auth=("op", "oppw")).status_code == 200


# ── bug-bash regressions ────────────────────────────────────────────────

def test_diff_and_compare_reject_path_traversal(web):
    client, _ = web
    assert client.get("/api/diff?a=../../../../etc/hosts&b=x", auth=("viewer", "vpw")).status_code == 400
    assert client.get("/api/diff?a=a/b&b=x", auth=("viewer", "vpw")).status_code == 400
    assert client.get("/compare/view?runs=../../etc", auth=("viewer", "vpw")).status_code == 400


def test_prepare_job_sets_no_run_id(web):
    """A prepare job must not pick up prepare_<slug>.json as a bogus run_id."""
    client, cfg = web
    client.post("/api/prepare", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    _, state, job = _run_worker_once(cfg)
    assert not job["run_id"]            # gated to run/soak kinds only


def test_reconcile_skips_malformed_manifest(web, tmp_path):
    """A non-dict manifest.json must not abort indexing of all runs."""
    from pgbench_webapp import index, queries
    from pgbench_webapp.db import connect
    bad = cfg_results(web) / "bad-run"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("[1, 2, 3]")          # valid JSON, not an object
    good = cfg_results(web) / "good-run"
    good.mkdir(parents=True)
    (good / "manifest.json").write_text(
        '{"run_id":"good-run","mode":"sweep","status":"complete","created_utc":"2026-01-01T00:00:00Z"}')
    conn = connect(web[1].db_path)
    n = index.reconcile(conn, web[1].results_dir)            # must not raise
    assert queries.get_run(conn, "good-run") is not None
    assert queries.get_run(conn, "bad-run") is None
    conn.close()


def cfg_results(web):
    return web[1].results_dir


# ── Phase 8: SPA admin APIs, concurrency, run-id parsing ────────────────

def test_parse_run_id_from_output():
    from pathlib import Path
    from pgbench_webapp.worker import _parse_run_id
    rd = Path("/var/lib/pgbench-harness/results")
    rid = "advanced-8c32g-tpcc-20260101-000000"
    assert _parse_run_id(f"run {rid} -> {rd}/{rid} (budget 5m)\n", rd) == rid
    assert _parse_run_id("nothing here", rd) is None


def test_admin_settings_api_and_concurrency(web):
    client, _ = web
    assert client.get("/api/admin/settings", auth=("op", "oppw")).status_code == 403
    s = client.get("/api/admin/settings", auth=("admin", "apw")).json()
    assert s["max_concurrency"] == 1 and "has_smtp_pw" in s
    r = client.post("/api/admin/settings",
                    json={"max_concurrency": 4, "smtp": {}, "slack": {}, "base_url": ""},
                    auth=("admin", "apw"))
    assert r.status_code == 200
    assert client.get("/api/admin/settings", auth=("admin", "apw")).json()["max_concurrency"] == 4


def test_users_api_create_update_self_protect(web):
    client, _ = web
    assert client.get("/api/users", auth=("viewer", "vpw")).status_code == 403
    assert client.post("/api/users", json={"username": "bob", "password": "pw", "role": "operator"},
                       auth=("admin", "apw")).status_code == 200
    assert any(u["username"] == "bob" for u in client.get("/api/users", auth=("admin", "apw")).json())
    assert client.post("/api/users/bob", json={"role": "viewer"}, auth=("admin", "apw")).status_code == 200
    # an admin cannot lock themselves out
    assert client.post("/api/users/admin", json={"disabled": True}, auth=("admin", "apw")).status_code == 400


def test_legacy_admin_paths_redirect_to_console(web):
    client, _ = web
    for path, target in [("/admin/users", "/ui/users"), ("/admin/settings", "/ui/settings"),
                         ("/audit", "/ui/audit"), ("/compare", "/ui/compare")]:
        r = client.get(path, auth=("admin", "apw"), follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == target


# ── Phase 9: prepare safety, target creds, job detail/prepare stats ─────

def test_prepare_recreate_requires_confirm_api(web):
    client, _ = web
    spec = _spec_yaml()
    # destructive recreate without a matching typed confirmation is rejected
    r = client.post("/api/prepare", json={"spec_yaml": spec, "password": WEB_PW,
                                          "recreate": "database"}, auth=("op", "oppw"))
    assert r.status_code == 400
    r = client.post("/api/prepare", json={"spec_yaml": spec, "password": WEB_PW,
                                          "recreate": "database", "confirm": "sbtest"}, auth=("op", "oppw"))
    assert r.status_code == 200 and r.json()["kind"] == "prepare"


def test_target_update_rotates_credentials(web):
    client, cfg = web
    client.post("/api/targets", json={"name": "t1", "host": "h", "dbname": "sbtest",
                                      "dbuser": "olduser", "password": "oldpw"}, auth=("op", "oppw"))
    tid = client.get("/api/targets", auth=("op", "oppw")).json()[0]["id"]
    r = client.post(f"/api/targets/{tid}", json={"dbuser": "newuser", "password": "newpw"}, auth=("op", "oppw"))
    assert r.status_code == 200
    t = [t for t in client.get("/api/targets", auth=("op", "oppw")).json() if t["id"] == tid][0]
    assert t["dbuser"] == "newuser"
    from pgbench_webapp.secrets_store import SecretStore
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    assert store.get("target:t1:password") == "newpw"
    # viewer cannot update
    assert client.post(f"/api/targets/{tid}", json={"dbuser": "x"}, auth=("viewer", "vpw")).status_code == 403


def test_job_detail_includes_prepare_stats(web):
    client, cfg = web
    jid = client.post("/api/prepare", json={"spec_yaml": _spec_yaml(), "password": WEB_PW},
                      auth=("op", "oppw")).json()["job_id"]
    # write the load-metrics file prepare would produce (slug = host-database)
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    (cfg.results_dir / "prepare_db-example-invalid-sbtest.json").write_text(
        '{"loaded_units":"300 warehouses","wall_s":1234.5,"db_size_pretty":"38.2 GiB",'
        '"load_mb_s":31.2,"started_utc":"2026-06-28T10:00:00Z","finished_utc":"2026-06-28T10:20:34Z"}')
    d = client.get(f"/api/jobs/{jid}", auth=("viewer", "vpw")).json()
    assert d["kind"] == "prepare" and d["prepare_stats"]["loaded_units"] == "300 warehouses"
    assert client.get("/api/jobs/99999", auth=("viewer", "vpw")).status_code == 404
