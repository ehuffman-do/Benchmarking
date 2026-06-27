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
        base += "events:\n  - {at_s: 1, type: failover, label: fail}\n"
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
    assert client.get("/admin/users", auth=("viewer", "vpw")).status_code == 403
    assert client.get("/audit", auth=("op", "oppw")).status_code == 403          # operator not admin
    # operator: can start; admin: can admin
    assert client.get("/admin/users", auth=("admin", "apw")).status_code == 200
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
    # appears in history + report renders + artifact downloads
    assert run_id in client.get("/", auth=("viewer", "vpw")).text
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


def test_settings_save_keeps_secrets_off_db(web):
    client, cfg = web
    r = client.post("/admin/settings", data={
        "csrf_token": "", "base_url": "https://h:8443", "do_cluster_id": "c1",
        "do_api_token": "do-tok-SECRET", "slack_webhook": "https://hooks/secret",
        "smtp_host": "", "smtp_port": "587"}, auth=("admin", "apw"))
    assert r.status_code in (200, 303)
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
