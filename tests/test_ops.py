"""Cluster Ops: kube targets CRUD/RBAC, validate/discover jobs against the fake
kubectl, topology caching, ops-run indexing, and the extended secret-leak gate
(kubeconfig contents + k8s-secret-derived password).
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

KUBE_TOKEN = "kube-token-SENTINEL-do-not-leak-abcdef123456"
K8S_PW = "k8s-pguser-password-SENTINEL-98765"

KUBECONFIG_CONTENT = f"""apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: TFMwdExTMUNSVWRKVGlCRFJWSlVTVVpKUTBGVVJTMHRMUzB0
    server: https://fake-k8s.example.com:6443
  name: do-nyc1-fake
contexts:
- context: {{cluster: do-nyc1-fake, user: do-nyc1-fake-admin}}
  name: do-nyc1-fake
current-context: do-nyc1-fake
users:
- name: do-nyc1-fake-admin
  user:
    token: {KUBE_TOKEN}
"""


@pytest.fixture()
def opsweb(tmp_path, monkeypatch):
    """TestClient + cfg with the fake kubectl cluster on PATH."""
    for exe in ("sysbench", "psql", "kubectl"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    import sys
    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{venv_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGBENCH_HARNESS_BIN", str(venv_bin / "pgbench-harness"))
    state = tmp_path / "fakekube"; state.mkdir()
    monkeypatch.setenv("FAKE_KUBE_STATE", str(state))
    monkeypatch.setenv("FAKE_KUBE_PGPASS", K8S_PW)
    # fast scenario/backup timings for tests
    monkeypatch.setenv("FAKE_KUBE_RESTART_S", "1.5")
    monkeypatch.setenv("FAKE_KUBE_BACKUP_S", "1.0")
    monkeypatch.setenv("FAKE_KUBE_LOG_FOLLOW_S", "6")
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
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    try:
        job = queries.claim_next_job(conn, 4)
        assert job is not None, "expected a queued job"
        state = worker.run_job(cfg, conn, job)
        return job["id"], state, queries.get_job(conn, job["id"])
    finally:
        conn.close()


def _drain_queue(cfg, max_jobs: int = 5):
    """Run queued jobs until the queue is empty; returns final states."""
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    states = []
    conn = connect(cfg.db_path)
    try:
        for _ in range(max_jobs):
            job = queries.claim_next_job(conn, 8)
            if job is None:
                break
            states.append(worker.run_job(cfg, conn, job))
    finally:
        conn.close()
    return states


def _create_target(client, upload: bool = True, **overrides):
    payload = {"name": "doks-test", "namespace": "percona", "db_user": "doadmin"}
    if upload:
        payload["kubeconfig_content"] = KUBECONFIG_CONTENT
    payload.update(overrides)
    r = client.post("/api/kube-targets", json=payload, auth=("admin", "apw"))
    assert r.status_code == 201, r.text
    return r.json()


# ── unit: patroni parser against real-format output ──

def test_patronictl_parse_real_format():
    from pgbench_harness.ops.patroni import parse_patronictl_list
    text = json.dumps([
        {"Cluster": "cluster1-ha", "Member": "cluster1-instance1-6kbw-0",
         "Host": "10.244.0.25", "Role": "Leader", "State": "running", "TL": 5},
        {"Cluster": "cluster1-ha", "Member": "cluster1-instance1-9zqp-0",
         "Host": "10.244.1.14", "Role": "Replica", "State": "streaming",
         "TL": 5, "Lag in MB": 0},
        {"Cluster": "cluster1-ha", "Member": "cluster1-instance1-x2vr-0",
         "Host": "10.244.2.8", "Role": "Sync Standby", "State": "streaming",
         "TL": 5, "Lag in MB": 12},
    ])
    view = parse_patronictl_list(text)
    assert view.leader_name == "cluster1-instance1-6kbw-0"
    assert view.timeline == 5
    assert len(view.members) == 3
    assert view.members[2].lag_mb == 12.0
    assert not view.members[1].is_leader


def test_patronictl_parse_rejects_garbage():
    from pgbench_harness.ops.patroni import parse_patronictl_list
    with pytest.raises(ValueError):
        parse_patronictl_list("{}")
    with pytest.raises(ValueError):
        parse_patronictl_list("[]")


def test_ops_spec_validation():
    from pgbench_harness.errors import SpecError
    from pgbench_harness.ops.opspec import parse_ops_spec
    with pytest.raises(SpecError):
        parse_ops_spec({"op": "nonsense", "target": {"name": "x"}})
    with pytest.raises(SpecError):    # scenario needs a case
        parse_ops_spec({"op": "scenario", "target": {"name": "x", "cr_name": "c"},
                        "params": {}})
    with pytest.raises(SpecError):    # destructive ops need a CR name
        parse_ops_spec({"op": "backup", "target": {"name": "x"},
                        "params": {"type": "full"}})
    spec = parse_ops_spec({"op": "discover", "target": {"name": "t"}})
    assert spec.target.namespace == "percona"
    assert spec.target.patroni_scope == "-ha"  # no cr yet


# ── kube target CRUD + RBAC ──

def test_kube_target_crud_rbac(opsweb):
    client, cfg = opsweb
    # viewer/operator cannot create
    r = client.post("/api/kube-targets", json={"name": "x", "kubeconfig_path": "/x"},
                    auth=("viewer", "vpw"))
    assert r.status_code == 403
    r = client.post("/api/kube-targets", json={"name": "x", "kubeconfig_path": "/x"},
                    auth=("op", "oppw"))
    assert r.status_code == 403
    created = _create_target(client)
    assert created["validate_job_id"]           # validation auto-enqueued
    # list: kubeconfig contents never surface; imported flag set
    r = client.get("/api/kube-targets", auth=("viewer", "vpw"))
    assert r.status_code == 200
    kt = r.json()[0]
    assert kt["kubeconfig_imported"] is True
    assert kt["kubeconfig_path"] == ""
    assert KUBE_TOKEN not in r.text
    # duplicate name refused
    r = client.post("/api/kube-targets",
                    json={"name": "doks-test", "kubeconfig_path": "/x"},
                    auth=("admin", "apw"))
    assert r.status_code == 409
    # update
    r = client.post(f"/api/kube-targets/{kt['id']}", json={"cr_name": "cluster1"},
                    auth=("admin", "apw"))
    assert r.status_code == 200 and r.json()["cr_name"] == "cluster1"
    # delete (queue is idle after we drain the auto-validate job)
    _drain_queue(cfg)
    r = client.delete(f"/api/kube-targets/{kt['id']}", auth=("admin", "apw"))
    assert r.status_code == 200
    assert client.get("/api/kube-targets", auth=("viewer", "vpw")).json() == []


def test_kube_target_requires_path_or_content(opsweb):
    client, _ = opsweb
    r = client.post("/api/kube-targets", json={"name": "nope"}, auth=("admin", "apw"))
    assert r.status_code == 400


# ── validate + discover through the worker ──

def test_validate_job_caches_summary(opsweb):
    client, cfg = opsweb
    created = _create_target(client)
    job_id, state, job = _run_worker_once(cfg)
    assert state == "done" and job_id == created["validate_job_id"]
    r = client.get(f"/api/kube-targets/{created['id']}", auth=("viewer", "vpw"))
    kt = r.json()
    assert kt["api_server"] == "https://fake-k8s.example.com:6443"
    assert kt["last_validated_utc"]
    assert kt["cr_name"] == "cluster1"          # auto-prefilled (single CR found)
    assert kt["pguser_secret"] == "cluster1-pguser-doadmin"
    # job stream shows structured checks
    r = client.get(f"/api/jobs/{job_id}", auth=("viewer", "vpw"))
    assert r.status_code == 200


def test_discover_caches_topology(opsweb):
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)                            # auto-validate
    r = client.post(f"/api/kube-targets/{created['id']}/discover",
                    auth=("op", "oppw"))
    assert r.status_code == 200
    _drain_queue(cfg)
    topo = client.get(f"/api/kube-targets/{created['id']}/topology",
                      auth=("viewer", "vpw")).json()
    assert topo["topology"]["patroni"]["leader"] == "cluster1-instance1-abcd-0"
    assert topo["topology"]["cr_name"] == "cluster1"
    assert len(topo["topology"]["pods"]["instances"]) == 3
    assert topo["topology"]["backups"]["schedules"][0]["schedules"]["incremental"]
    assert topo["collected_utc"]


def test_validate_reports_missing_kubeconfig(opsweb):
    client, cfg = opsweb
    _create_target(client, upload=False, name="badpath",
                   kubeconfig_path="/nonexistent/kubeconfig.yaml")
    job_id, state, job = _run_worker_once(cfg)
    assert state == "failed"
    out = (cfg.data_dir / "jobs" / f"job_{job_id}.out").read_text()
    assert "Traceback" not in out                # never crash — report cleanly
    assert "kubeconfig not found" in out
    assert "kubeconfigs/" in out                 # points at the sanctioned dir
    assert "OPS_SUMMARY_JSON" in out             # summary still emitted for the worker


def test_validation_verdict_recorded_and_reset(opsweb, monkeypatch):
    """last_validation_ok: True after a pass, None after any edit (stale
    verdict must not linger), False after a failing validate."""
    client, cfg = opsweb
    created = _create_target(client)
    _run_worker_once(cfg)                        # auto-validate → ok
    kt = client.get(f"/api/kube-targets/{created['id']}", auth=("viewer", "vpw")).json()
    assert kt["last_validation_ok"] is True

    r = client.post(f"/api/kube-targets/{created['id']}",
                    json={"db_name": "otherdb"}, auth=("admin", "apw"))
    assert r.status_code == 200
    assert r.json()["last_validation_ok"] is None    # verdict reset on edit

    monkeypatch.setenv("FAKE_KUBE_AUTH_FAIL", "1")
    r = client.post(f"/api/kube-targets/{created['id']}/validate",
                    auth=("op", "oppw"))
    assert r.status_code == 200
    _drain_queue(cfg)
    kt = client.get(f"/api/kube-targets/{created['id']}", auth=("viewer", "vpw")).json()
    assert kt["last_validation_ok"] is False
    assert kt["last_validated_utc"]


def test_discover_surfaces_auth_error(opsweb, monkeypatch):
    """An auth failure must be reported as such — not as 'no CR found'
    (the live-cluster confusion: exec-credential kubeconfigs that can't
    mint tokens under the sandbox look like an empty namespace)."""
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)                            # auto-validate (healthy)
    monkeypatch.setenv("FAKE_KUBE_AUTH_FAIL", "1")
    r = client.post(f"/api/kube-targets/{created['id']}/discover",
                    auth=("op", "oppw"))
    assert r.status_code == 200
    job_id, state, job = _run_worker_once(cfg)
    assert state == "failed"
    out = (cfg.data_dir / "jobs" / f"job_{job_id}.out").read_text()
    assert "could not query cluster CRs" in out
    assert "provide credentials" in out          # the real kubectl error, surfaced
    assert "run Validate on this target" in out  # actionable pointer
    assert "no perconapgcluster / postgrescluster found" not in out


def test_update_to_path_mode_clears_imported_copy(opsweb):
    """Switching an imported-kubeconfig target to path mode must drop the
    encrypted copy — the worker prefers the ref, so a stale one would make
    the new path silently unused."""
    client, cfg = opsweb
    created = _create_target(client)             # upload mode → ref set
    _drain_queue(cfg)
    kc = cfg.data_dir / "kubeconfigs" / "byhand.yaml"
    kc.parent.mkdir(parents=True, exist_ok=True)
    kc.write_text(KUBECONFIG_CONTENT)
    r = client.post(f"/api/kube-targets/{created['id']}",
                    json={"kubeconfig_path": str(kc)}, auth=("admin", "apw"))
    assert r.status_code == 200
    kt = r.json()
    assert kt["kubeconfig_imported"] is False
    assert kt["kubeconfig_path"] == str(kc)
    # and validate still works through the path
    client.post(f"/api/kube-targets/{created['id']}/validate", auth=("op", "oppw"))
    _drain_queue(cfg)
    kt = client.get(f"/api/kube-targets/{created['id']}", auth=("viewer", "vpw")).json()
    assert kt["last_validation_ok"] is True


# ── parameter map / diagnostics / health ──

def test_pg_params_snapshot_caches_catalog(opsweb):
    """The parameter map is INTROSPECTED from pg_settings (types, units,
    ranges, enums, contexts) and overlaid with the Patroni apply-channel."""
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)                            # auto-validate → cr_name known
    r = client.post(f"/api/kube-targets/{created['id']}/pg-params",
                    auth=("op", "oppw"))
    assert r.status_code == 200
    _drain_queue(cfg)
    doc = client.get(f"/api/kube-targets/{created['id']}/pg-params",
                     auth=("viewer", "vpw")).json()
    assert doc["collected_utc"]
    cat = doc["catalog"]
    assert cat["leader"] == "cluster1-instance1-abcd-0"
    assert cat["pg_version"] == "18.0"
    byname = {p["name"]: p for p in cat["params"]}
    # metadata straight from the server: units, ranges, enums, contexts
    assert byname["shared_buffers"]["unit"] == "8kB"
    assert byname["shared_buffers"]["restart_required"] is True
    assert byname["wal_level"]["enumvals"] == ["minimal", "replica", "logical"]
    assert byname["work_mem"]["min_val"] == "64"
    # the apply-channel overlay
    assert byname["listen_addresses"]["channel"] == "patroni-locked"
    assert byname["max_connections"]["channel"] == "dcs-coordinated"
    assert byname["archive_command"]["channel"] == "operator-managed"
    assert byname["work_mem"]["channel"] == "cr"
    assert byname["block_size"]["channel"] == "readonly"
    # CR-managed values are marked so the UI can show provenance
    assert byname["max_wal_size"]["cr_value"] == "4096"
    assert cat["cr_managed"]["max_wal_size"] == "4096"
    assert byname["work_mem"]["cr_value"] is None


def test_diag_checks_write_live_csvs(opsweb):
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)
    # catalog is served to the UI (no SQL in it)
    cat = client.get("/api/ops/diag-catalog", auth=("viewer", "vpw")).json()["checks"]
    keys = [c["key"] for c in cat]
    assert "slots" in keys and "patroni_list" in keys and "pvc_usage" in keys
    assert all("sql" not in c for c in cat)
    # unknown check → clean 400
    r = client.post(f"/api/kube-targets/{created['id']}/diag",
                    json={"params": {"checks": ["nope"]}}, auth=("op", "oppw"))
    assert r.status_code == 400
    # run a battery
    picked = ["connections", "long_running", "replication", "slots", "wraparound",
              "cache_hit", "patroni_list", "backup_info", "pods",
              "events_warnings", "pvc_usage"]
    r = client.post(f"/api/kube-targets/{created['id']}/diag",
                    json={"params": {"checks": picked}}, auth=("op", "oppw"))
    assert r.status_code == 200, r.text
    job_id, state, job = _run_worker_once(cfg)
    assert state == "done", (cfg.data_dir / "jobs" / f"job_{job_id}.out").read_text()
    parsed = cfg.results_dir / "ops" / job["run_id"] / "parsed"
    for key in picked:
        assert (parsed / f"{key}.csv").exists(), key
    conns = (parsed / "connections.csv").read_text().splitlines()
    assert conns[0] == "epoch_s,total,active,idle,idle_in_tx,waiting,max_connections,pct_used"
    assert len(conns) == 2
    pat = (parsed / "patroni_list.csv").read_text().splitlines()
    assert pat[0] == "epoch_s,member,role,state,timeline,lag_mb"
    assert len(pat) == 4                          # 3 members
    backups = (parsed / "backup_info.csv").read_text().splitlines()
    assert len(backups) >= 2                      # at least the seeded full
    # index shows the run with its headline
    runs = client.get("/api/ops/runs", auth=("viewer", "vpw")).json()
    diag_runs = [x for x in runs if x["kind"] == "diag"]
    assert diag_runs and diag_runs[0]["headline"]["checks"] == len(picked)


def test_diag_watch_mode_appends_samples(opsweb):
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)
    r = client.post(f"/api/kube-targets/{created['id']}/diag",
                    json={"params": {"checks": ["connections"], "watch_s": 2.5,
                                     "interval_s": 1}},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    job_id, state, job = _run_worker_once(cfg)
    assert state == "done"
    csv = (cfg.results_dir / "ops" / job["run_id"] / "parsed" /
           "connections.csv").read_text().splitlines()
    assert len(csv) >= 3                          # header + initial + watch rows
    # bounds enforced
    r = client.post(f"/api/kube-targets/{created['id']}/diag",
                    json={"params": {"watch_s": 999999}}, auth=("op", "oppw"))
    assert r.status_code == 400


def test_health_findings_and_target_badge(opsweb, monkeypatch):
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)
    # healthy cluster → ok, no findings
    client.post(f"/api/kube-targets/{created['id']}/health", auth=("op", "oppw"))
    _drain_queue(cfg)
    doc = client.get(f"/api/kube-targets/{created['id']}/health",
                     auth=("viewer", "vpw")).json()
    assert doc["health"]["status"] == "ok"
    assert doc["health"]["findings"] == []
    assert doc["health"]["checked"] >= 10
    # an inactive slot retaining WAL → warn finding with remediation + action
    monkeypatch.setenv("FAKE_KUBE_INACTIVE_SLOT", "1")
    client.post(f"/api/kube-targets/{created['id']}/health", auth=("op", "oppw"))
    _drain_queue(cfg)
    doc = client.get(f"/api/kube-targets/{created['id']}/health",
                     auth=("viewer", "vpw")).json()
    assert doc["health"]["status"] == "warn"
    slot = next(f for f in doc["health"]["findings"] if f["id"] == "slots")
    assert "pg_drop_replication_slot" in slot["remediation"]
    assert slot["action"] == {"type": "diag", "checks": ["slots"]}
    # the targets list badges the cached worst severity
    kt = client.get(f"/api/kube-targets/{created['id']}",
                    auth=("viewer", "vpw")).json()
    assert kt["health_status"] == "warn" and kt["health_utc"]


def test_sidecar_catalog_served(opsweb):
    """The curated pgBackRest/Patroni/pgBouncer option catalogs ship with the
    package and are served with per-operator CR apply paths."""
    client, _ = opsweb
    r = client.get("/api/ops/sidecar-catalog", auth=("viewer", "vpw"))
    assert r.status_code == 200
    doc = r.json()
    assert {"pgbackrest", "patroni", "pgbouncer"} <= set(doc)
    names = {o["name"] for o in doc["pgbackrest"]}
    assert "backup-standby" in names and "repoN-retention-full" in names
    pool = next(o for o in doc["pgbouncer"] if o["name"] == "pool_mode")
    assert pool["allowed"] == ["session", "transaction", "statement"]
    assert pool["percona_path"] and pool["crunchy_path"]
    assert any(o["name"] == "ttl" for o in doc["patroni"])


def test_params_diag_health_rbac(opsweb):
    """Read-only intelligence ops are operator-level; viewers can only read."""
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)
    for path in ("pg-params", "diag", "health"):
        r = client.post(f"/api/kube-targets/{created['id']}/{path}",
                        json={}, auth=("viewer", "vpw"))
        assert r.status_code == 403, path


# ── day-2 operations catalog ──

def _operate(client, tid, params, confirm="cluster1", dry=False):
    body = {"params": {**params, **({"dry_run": True} if dry else {})}}
    if not dry:
        body["confirm"] = confirm
    return client.post(f"/api/kube-targets/{tid}/operate", json=body,
                       auth=("admin", "apw"))


def test_operate_switchover_end_to_end(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    # dry-run needs no confirmation and makes no change
    r = _operate(client, tid, {"operation": "switchover"}, dry=True)
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "complete" and run["headline"]["dry_run"] is True
    # real switchover to an explicit target
    target = "cluster1-instance1-efgh-0"
    r = _operate(client, tid, {"operation": "switchover", "target": target,
                               "timeout_s": 30})
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    h = run["headline"]
    assert run["status"] == "complete", run
    assert h["flipped"] is True and h["leader_after"] == target
    assert h["tl_after"] == h["tl_before"] + 1
    # target-is-now-leader refused up front
    r = _operate(client, tid, {"operation": "switchover", "target": target})
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "aborted"
    assert run["headline"]["reason"] == "target-is-leader"


def test_operate_scale_up_then_down(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _operate(client, tid, {"operation": "scale", "replicas": 4,
                               "timeout_s": 30})
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "complete", run
    assert run["headline"] == {"operation": "scale", "from": 3, "to": 4,
                               "settled": True}
    # topology reflects 4 members
    client.post(f"/api/kube-targets/{tid}/discover", auth=("op", "oppw"))
    _drain_queue(cfg)
    topo = client.get(f"/api/kube-targets/{tid}/topology",
                      auth=("viewer", "vpw")).json()["topology"]
    assert len(topo["pods"]["instances"]) == 4
    # and back down
    r = _operate(client, tid, {"operation": "scale", "replicas": 3,
                               "timeout_s": 30})
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "complete" and run["headline"]["to"] == 3
    # no-change scale refused in preflight
    r = _operate(client, tid, {"operation": "scale", "replicas": 3})
    _drain_queue(cfg)
    assert _last_ops_run(client, "operate")["status"] == "aborted"


def test_operate_restart_member_and_cluster(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    member = "cluster1-instance1-ijkl-0"
    r = _operate(client, tid, {"operation": "restart", "scope": member,
                               "timeout_s": 30, "settle_grace_s": 0})
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "complete", run
    restarts = (Path(os.environ["FAKE_KUBE_STATE"]) / "restarts.log").read_text()
    assert member in restarts
    # cluster-wide: dry-run shows the annotation plan
    r = _operate(client, tid, {"operation": "restart"}, dry=True)
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "complete" and run["headline"]["scope"] == "cluster"
    assert "restartedAt" in run["headline"]["plan"]


def test_operate_resize_with_oom_preflight(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    resources = {"requests": {"cpu": "2", "memory": "4Gi"},
                 "limits": {"cpu": "4", "memory": "8Gi"}}
    r = _operate(client, tid, {"operation": "resize", "resources": resources,
                               "timeout_s": 30})
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "complete", run
    assert run["headline"]["resources"] == resources
    # garbage quantity refused in preflight
    r = _operate(client, tid, {"operation": "resize",
                               "resources": {"limits": {"memory": "lots"}}})
    _drain_queue(cfg)
    assert _last_ops_run(client, "operate")["status"] == "aborted"


def test_operate_schedules_and_retention(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _operate(client, tid, {"operation": "schedules", "repo": "repo1",
                               "schedules": {"incremental": "*/30 * * * *",
                                             "differential": None},
                               "retention": {"repo1-retention-full": "4"}})
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "complete", run
    assert run["headline"]["verified"] is True
    # bad cron refused
    r = _operate(client, tid, {"operation": "schedules",
                               "schedules": {"full": "whenever"}})
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "aborted" and run["headline"]["reason"] == "bad-cron"


def test_operate_validation_rbac_and_mutex(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    # unknown operation and out-of-range replicas → clean 400 at enqueue
    r = _operate(client, tid, {"operation": "explode"})
    assert r.status_code == 400
    r = _operate(client, tid, {"operation": "scale", "replicas": 99})
    assert r.status_code == 400
    # a real execution needs the typed confirmation
    r = client.post(f"/api/kube-targets/{tid}/operate",
                    json={"params": {"operation": "switchover"}},
                    auth=("admin", "apw"))
    assert r.status_code == 400
    # operator role cannot operate
    r = client.post(f"/api/kube-targets/{tid}/operate",
                    json={"params": {"operation": "switchover", "dry_run": True}},
                    auth=("op", "oppw"))
    assert r.status_code == 403
    # destructive mutex: an operate blocks a backup on the same target
    r = _operate(client, tid, {"operation": "switchover", "timeout_s": 30})
    assert r.status_code == 200
    r = client.post(f"/api/kube-targets/{tid}/backup",
                    json={"confirm": "cluster1", "params": {"type": "incr"}},
                    auth=("admin", "apw"))
    assert r.status_code == 409
    _drain_queue(cfg)


def test_pgbouncer_global_apply_and_verify(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    params = {"action": "pgbouncer_global",
              "global": {"pool_mode": "transaction", "max_client_conn": "500"}}
    # dry-run first: diff against the live CR
    r = client.post(f"/api/kube-targets/{tid}/cr-apply",
                    json={"params": {**params, "dry_run": True}},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["headline"]["changed"]["pool_mode"] == ["session", "transaction"]
    # apply & verify against the rendered ini in the pgbouncer pod
    r = client.post(f"/api/kube-targets/{tid}/cr-apply",
                    json={"confirm": "cluster1", "params": params},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["status"] == "complete", run
    assert run["headline"]["verified"] is True
    # a fresh parameter snapshot shows the LIVE CR value, not the catalog default
    client.post(f"/api/kube-targets/{tid}/pg-params", auth=("op", "oppw"))
    _drain_queue(cfg)
    cat = client.get(f"/api/kube-targets/{tid}/pg-params",
                     auth=("viewer", "vpw")).json()["catalog"]
    assert cat["pgbouncer_global"]["max_client_conn"] == "500"
    assert cat["pgbouncer_global"]["pool_mode"] == "transaction"


def test_patroni_dcs_apply_and_verify(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    params = {"action": "patroni_dcs",
              "settings": {"ttl": "45", "loop_wait": "15",
                           "synchronous_mode": "true",
                           "postgresql.use_slots": "true"}}
    r = client.post(f"/api/kube-targets/{tid}/cr-apply",
                    json={"params": {**params, "dry_run": True}},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["headline"]["dry_run"] is True
    assert run["headline"]["changed"]["ttl"][1] == "45"
    r = client.post(f"/api/kube-targets/{tid}/cr-apply",
                    json={"confirm": "cluster1", "params": params},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["status"] == "complete", run
    assert run["headline"]["verified"] is True
    # Percona routing: ttl/loop_wait went to the dedicated CR fields
    import json as _json
    st = _json.loads((Path(os.environ["FAKE_KUBE_STATE"]) / "state.json").read_text())
    pat = st["cr"]["spec"]["patroni"]
    assert pat["leaderLeaseDurationSeconds"] == 45
    assert pat["syncPeriodSeconds"] == 15
    assert pat["dynamicConfiguration"]["synchronous_mode"] is True
    assert pat["dynamicConfiguration"]["postgresql"]["use_slots"] is True
    # and the snapshot shows the live DCS values
    client.post(f"/api/kube-targets/{tid}/pg-params", auth=("op", "oppw"))
    _drain_queue(cfg)
    cat = client.get(f"/api/kube-targets/{tid}/pg-params",
                     auth=("viewer", "vpw")).json()["catalog"]
    assert cat["patroni_dcs"]["ttl"] == "45"
    assert cat["patroni_dcs"]["synchronous_mode"] == "True"
    assert cat["patroni_dcs"]["postgresql.use_slots"] == "True"


def test_operate_bugbash_guardrails(opsweb):
    """Bug-bash regressions: scale-to-0 crashed mid-verify; an empty resize
    resources dict would have REPLACED the pods' resources with {}."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _operate(client, tid, {"operation": "scale", "replicas": 0})
    assert r.status_code == 400 and "pause" in r.text
    r = _operate(client, tid, {"operation": "resize", "resources": {}})
    assert r.status_code == 400
    r = _operate(client, tid, {"operation": "resize",
                               "resources": {"limits": {}, "requests": {}}})
    assert r.status_code == 400
    # failover without a candidate dies inside patronictl — refuse in preflight
    r = _operate(client, tid, {"operation": "failover"})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "operate")
    assert run["status"] == "aborted"
    assert run["headline"]["reason"] == "failover-needs-target"


def test_patroni_dcs_rollback_restores_previous(opsweb):
    """Bug-bash regression: rollback of a patroni_dcs run hit 'unknown action'."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    for ttl in ("45", "60"):
        r = client.post(f"/api/kube-targets/{tid}/cr-apply",
                        json={"confirm": "cluster1",
                              "params": {"action": "patroni_dcs",
                                         "settings": {"ttl": ttl}}},
                        auth=("admin", "apw"))
        assert r.status_code == 200, r.text
        _drain_queue(cfg)
    src = _last_ops_run(client, "cr-apply")
    assert src["headline"]["verified"] is True
    r = client.post(f"/api/kube-targets/{tid}/cr-apply",
                    json={"confirm": "cluster1",
                          "params": {"action": "rollback",
                                     "rollback_of": src["op_run_id"]}},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["status"] == "complete", run
    import json as _json
    st = _json.loads((Path(os.environ["FAKE_KUBE_STATE"]) / "state.json").read_text())
    assert st["cr"]["spec"]["patroni"]["leaderLeaseDurationSeconds"] == 45


def test_auto_health_skips_during_destructive_op(opsweb):
    """Bug-bash regression: a health check exec'd into pods mid-backup/failover
    would produce garbage findings and false transition alerts."""
    from pgbench_webapp import ops_support
    from pgbench_webapp.db import connect
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = client.post(f"/api/kube-targets/{tid}", json={"auto_health_s": 900},
                    auth=("admin", "apw"))
    assert r.status_code == 200
    r = _fire_backup(client, tid, {"type": "incr", "sample_interval_s": 0.2,
                                   "settle_s": 0.2})
    assert r.status_code == 200
    conn = connect(cfg.db_path)
    try:
        assert ops_support.maybe_enqueue_auto_health(cfg, conn) == 0  # backup queued
    finally:
        conn.close()
    _drain_queue(cfg)
    conn = connect(cfg.db_path)
    try:
        assert ops_support.maybe_enqueue_auto_health(cfg, conn) == 1  # clear now
    finally:
        conn.close()
    _drain_queue(cfg)


# ── continuous intelligence ──

def test_auto_health_scheduling_and_history(opsweb):
    from pgbench_webapp import ops_support, queries
    from pgbench_webapp.db import connect
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    # interval bounds enforced
    r = client.post(f"/api/kube-targets/{tid}", json={"auto_health_s": 30},
                    auth=("admin", "apw"))
    assert r.status_code == 400
    r = client.post(f"/api/kube-targets/{tid}", json={"auto_health_s": 900},
                    auth=("admin", "apw"))
    assert r.status_code == 200 and r.json()["auto_health_s"] == 900
    # a scheduling-only edit must NOT reset the validation verdict
    assert r.json()["last_validation_ok"] is True

    conn = connect(cfg.db_path)
    try:
        assert ops_support.maybe_enqueue_auto_health(cfg, conn) == 1
        assert ops_support.maybe_enqueue_auto_health(cfg, conn) == 0  # already queued
    finally:
        conn.close()
    _drain_queue(cfg)
    hist = client.get(f"/api/kube-targets/{tid}/health-history",
                      auth=("viewer", "vpw")).json()["history"]
    assert len(hist) == 1 and hist[0]["status"] == "ok"
    assert hist[0]["metrics"]["disk_pct_max"] == 38.0
    conn = connect(cfg.db_path)
    try:
        # fresh health_utc → interval not yet elapsed → nothing enqueued
        assert ops_support.maybe_enqueue_auto_health(cfg, conn) == 0
    finally:
        conn.close()


def test_health_transitions_recorded(opsweb, monkeypatch):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    client.post(f"/api/kube-targets/{tid}/health", auth=("op", "oppw"))
    _drain_queue(cfg)
    monkeypatch.setenv("FAKE_KUBE_INACTIVE_SLOT", "1")
    client.post(f"/api/kube-targets/{tid}/health", auth=("op", "oppw"))
    _drain_queue(cfg)
    hist = client.get(f"/api/kube-targets/{tid}/health-history",
                      auth=("viewer", "vpw")).json()["history"]
    assert [h["status"] for h in hist] == ["warn", "ok"]    # newest first


def test_disk_trend_projection():
    import json as _json
    from pgbench_webapp.ops_support import _disk_trend_finding
    rows = [   # newest first: +1%/day at 84% → ~6 days to 90%
        {"metrics": _json.dumps({"disk_pct_max": 84}), "ts_utc": "2026-07-10T00:00:00Z"},
        {"metrics": _json.dumps({"disk_pct_max": 83}), "ts_utc": "2026-07-09T00:00:00Z"},
        {"metrics": _json.dumps({"disk_pct_max": 82}), "ts_utc": "2026-07-08T00:00:00Z"},
    ]
    f = _disk_trend_finding(rows, None)
    assert f is not None and f["severity"] == "warn"
    assert "days to 90%" in f["value"]
    # a flat series never projects
    flat = [{"metrics": _json.dumps({"disk_pct_max": 38}),
             "ts_utc": f"2026-07-0{d}T00:00:00Z"} for d in (9, 8, 7)]
    assert _disk_trend_finding(flat, 38) is None
    # 3 days out escalates to crit
    steep = [
        {"metrics": _json.dumps({"disk_pct_max": 88}), "ts_utc": "2026-07-10T00:00:00Z"},
        {"metrics": _json.dumps({"disk_pct_max": 86}), "ts_utc": "2026-07-09T00:00:00Z"},
        {"metrics": _json.dumps({"disk_pct_max": 84}), "ts_utc": "2026-07-08T00:00:00Z"},
    ]
    f = _disk_trend_finding(steep, None)
    assert f is not None and f["severity"] == "crit"


# ── the extended leak gate ──

def test_kube_secrets_never_leak_anywhere(opsweb):
    """Kubeconfig contents and the k8s-secret-derived DB password must never
    appear in the DB, job specs, logs, API responses, or any artifact."""
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)
    client.post(f"/api/kube-targets/{created['id']}/discover", auth=("op", "oppw"))
    _drain_queue(cfg)

    for sentinel in (KUBE_TOKEN, K8S_PW):
        for path in cfg.data_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name == "secrets.enc":       # Fernet ciphertext — checked below
                continue
            if "fakekube" in str(path):          # the fake cluster's own state
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            assert sentinel not in content, f"{sentinel[:12]}… leaked into {path}"
        enc = (cfg.data_dir / "secrets.enc").read_text()
        assert sentinel not in enc               # encrypted, not plaintext

    for url in ("/api/kube-targets", f"/api/kube-targets/{created['id']}",
                f"/api/kube-targets/{created['id']}/topology", "/api/ops/runs",
                "/api/jobs"):
        r = client.get(url, auth=("admin", "apw"))
        assert KUBE_TOKEN not in r.text and K8S_PW not in r.text, url


def test_ops_actions_audited(opsweb):
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)
    client.post(f"/api/kube-targets/{created['id']}/discover", auth=("op", "oppw"))
    r = client.get("/api/audit", auth=("admin", "apw"))
    actions = [a["action"] for a in r.json()]
    assert "kube_target_create" in actions
    assert "ops_validate_enqueue" in actions
    assert "ops_discover_enqueue" in actions


# ── Phase 2: CR configuration ──

def _apply_cr(client, cfg, target_id, params, confirm="cluster1", label=""):
    r = client.post(f"/api/kube-targets/{target_id}/cr-apply",
                    json={"params": params, "confirm": confirm, "label": label},
                    auth=("admin", "apw"))
    return r


def _ready_target(client, cfg):
    created = _create_target(client)
    _drain_queue(cfg)                            # auto-validate prefills cr_name
    return created["id"]


def _last_ops_run(client, kind=None):
    runs = client.get("/api/ops/runs", auth=("viewer", "vpw")).json()
    if kind:
        runs = [r for r in runs if r["kind"] == kind]
    assert runs, f"no ops runs of kind {kind}"
    return runs[0]


def test_cr_apply_dry_run_shows_diff_without_patching(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _apply_cr(client, cfg, tid, {"action": "patroni_params", "dry_run": True,
                                     "parameters": {"max_wal_size": "49152",
                                                    "checkpoint_timeout": "900"}})
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["status"] == "complete"
    assert run["headline"]["dry_run"] is True
    assert run["headline"]["changed"]["max_wal_size"] == ["4096", "49152"]
    # nothing was patched
    state = json.loads((cfg.data_dir.parent / "fakekube" / "state.json").read_text())
    params = state["cr"]["spec"]["patroni"]["dynamicConfiguration"]["postgresql"]["parameters"]
    assert params["max_wal_size"] == "4096"
    # artifacts present: exact patch + value diff + CR snapshot
    detail = client.get(f"/api/ops/runs/{run['op_run_id']}", auth=("viewer", "vpw")).json()
    assert "patch.json" in detail["files"] and "diff.json" in detail["files"]
    assert "cr_snapshot.yaml" in detail["files"]


def test_cr_apply_requires_typed_confirmation(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _apply_cr(client, cfg, tid, {"action": "patroni_params",
                                     "parameters": {"max_wal_size": "49152"}},
                  confirm="WRONG")
    assert r.status_code == 400
    assert "cluster1" in r.json()["detail"]
    # dry-run needs no confirmation
    r = _apply_cr(client, cfg, tid, {"action": "patroni_params", "dry_run": True,
                                     "parameters": {"max_wal_size": "49152"}},
                  confirm="")
    assert r.status_code == 200


def test_cr_apply_verifies_live_values(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _apply_cr(client, cfg, tid, {"action": "patroni_params",
                                     "parameters": {"max_wal_size": "49152",
                                                    "min_wal_size": "2048"},
                                     "verify_timeout_s": 10})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["status"] == "complete", run
    assert run["headline"]["verified"] is True
    assert run["headline"]["pending_restart"] == []
    detail = client.get(f"/api/ops/runs/{run['op_run_id']}", auth=("viewer", "vpw")).json()
    assert "verify.json" in detail["files"]
    assert "patronictl_show_config.txt" in detail["raw_files"]


def test_cr_apply_pending_restart_fails_loudly(opsweb, monkeypatch):
    client, cfg = opsweb
    monkeypatch.setenv("FAKE_KUBE_PENDING_PARAMS", "max_wal_size")
    tid = _ready_target(client, cfg)
    r = _apply_cr(client, cfg, tid, {"action": "patroni_params",
                                     "parameters": {"max_wal_size": "49152"},
                                     "verify_timeout_s": 6})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["status"] == "warning"            # NOT silent success
    assert run["headline"]["pending_restart"] == ["max_wal_size"]
    # the event feed carries the operator-facing warning
    events = (cfg.results_dir / "ops" / run["op_run_id"] / "events.jsonl").read_text()
    assert "EXPECT A FAILOVER" in events


def test_cr_apply_pgbackrest_global_rendered_verify(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _apply_cr(client, cfg, tid, {"action": "pgbackrest_global",
                                     "global": {"process-max": "4",
                                                "archive-async": "y",
                                                "spool-path": "/pgdata"},
                                     "verify_timeout_s": 10})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    assert run["status"] == "complete", run
    assert run["headline"]["verified"] is True
    assert run["headline"]["changed"]["process-max"] == [None, "4"]


def test_cr_apply_rollback_restores_previous_values(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    _apply_cr(client, cfg, tid, {"action": "patroni_params",
                                 "parameters": {"max_wal_size": "49152"},
                                 "verify_timeout_s": 10})
    _drain_queue(cfg)
    first = _last_ops_run(client, "cr-apply")
    assert first["headline"]["changed"]["max_wal_size"] == ["4096", "49152"]
    r = _apply_cr(client, cfg, tid, {"action": "rollback",
                                     "rollback_of": first["op_run_id"],
                                     "verify_timeout_s": 10})
    assert r.status_code == 200
    _drain_queue(cfg)
    rb = _last_ops_run(client, "cr-apply")
    assert rb["status"] == "complete", rb
    assert rb["headline"]["changed"]["max_wal_size"] == ["49152", "4096"]
    state = json.loads((cfg.data_dir.parent / "fakekube" / "state.json").read_text())
    params = state["cr"]["spec"]["patroni"]["dynamicConfiguration"]["postgresql"]["parameters"]
    assert params["max_wal_size"] == "4096"


def test_schedules_pause_and_restore_with_nag(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    # pause: snapshot recorded, CR schedules removed, nag flag set
    r = client.post(f"/api/kube-targets/{tid}/schedules/pause",
                    json={"confirm": "cluster1"}, auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    kt = client.get(f"/api/kube-targets/{tid}", auth=("viewer", "vpw")).json()
    assert kt["schedules_paused"] is True and kt["schedules_paused_utc"]
    state = json.loads((cfg.data_dir.parent / "fakekube" / "state.json").read_text())
    repos = state["cr"]["spec"]["backups"]["pgbackrest"]["repos"]
    assert "schedules" not in repos[0]
    # restore puts the snapshot back and clears the nag
    r = client.post(f"/api/kube-targets/{tid}/schedules/restore",
                    json={"confirm": "cluster1"}, auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    kt = client.get(f"/api/kube-targets/{tid}", auth=("viewer", "vpw")).json()
    assert kt["schedules_paused"] is False
    state = json.loads((cfg.data_dir.parent / "fakekube" / "state.json").read_text())
    repos = state["cr"]["spec"]["backups"]["pgbackrest"]["repos"]
    assert repos[0]["schedules"]["incremental"] == "0 * * * *"


def test_cr_apply_prep_actions(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _apply_cr(client, cfg, tid, {"action": "patroni_params",
                                     "parameters": {"max_wal_size": "8192"},
                                     "verify_timeout_s": 10,
                                     "prep": {"reset_checkpointer": True,
                                              "recreate_db": "sbtest",
                                              "confirm": "sbtest"}})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    events = (cfg.results_dir / "ops" / run["op_run_id"] / "events.jsonl").read_text()
    assert "checkpointer stats reset" in events
    assert "recreated" in events


# ── Phase 3: backups ──

def test_archiver_parse_nonempty_against_real_output():
    """The bash sampler's query came back empty in the field (quoting bug);
    the Python parser must produce a full row from real psql -A -t -F, output."""
    from pgbench_harness.ops.backup import parse_archiver_row
    real = "1751848201,4821,0,0000000500000000000000AB,3,,-1\n"
    row = parse_archiver_row(real)
    assert row is not None and row[1] == "4821" and row[3].startswith("00000005")
    assert parse_archiver_row("") is None
    assert parse_archiver_row("garbage|not|csv") is None


def test_pgbackrest_info_json_parse():
    from pgbench_harness.ops.backup import lock_held, parse_pgbackrest_info_json
    doc = json.dumps([{"name": "db", "status": {"code": 0, "message": "ok"},
                       "backup": [{"label": "20260707-010203F", "type": "full",
                                   "info": {"size": 13100000000, "delta": 13100000000,
                                            "repository": {"size": 2040000000,
                                                           "delta": 2040000000}},
                                   "timestamp": {"start": 1, "stop": 1800}}]}])
    parsed = parse_pgbackrest_info_json(doc)
    assert parsed["backups"][0]["label"] == "20260707-010203F"
    assert parsed["backups"][0]["repo_backup_size"] == 2040000000
    assert not lock_held("stanza: db\n    status: ok\n")
    assert lock_held("stanza: db\n    status: ok (backup/expire running)\n")


def _fire_backup(client, tid, params, confirm="cluster1"):
    return client.post(f"/api/kube-targets/{tid}/backup",
                       json={"params": params, "confirm": confirm},
                       auth=("admin", "apw"))


def test_backup_direct_full_with_samplers(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_backup(client, tid, {"type": "full", "path": "direct",
                                   "sample_interval_s": 0.2, "settle_s": 0.5})
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "backup")
    assert run["status"] == "complete", run
    h = run["headline"]
    assert h["type"] == "full" and h["path"] == "direct"
    assert h["source_role"] == "leader"
    assert h["label"].endswith("F")
    assert h["backup_start_epoch_ms"] and h["backup_end_epoch_ms"]
    assert h["peak_archive_queue"] >= 3          # sampler saw the queue
    run_dir = cfg.results_dir / "ops" / run["op_run_id"]
    arch = (run_dir / "parsed" / "archiver.csv").read_text().splitlines()
    assert arch[0].startswith("epoch_s,archived_count")
    assert len(arch) >= 2                         # non-empty samples (the bug fix)
    assert (run_dir / "raw" / "pgbackrest_info_before.txt").exists()
    assert (run_dir / "raw" / "pgbackrest_info_after.json").exists()


def test_backup_aborts_on_held_lock(opsweb, monkeypatch):
    client, cfg = opsweb
    monkeypatch.setenv("FAKE_KUBE_BACKUP_LOCKED", "1")
    tid = _ready_target(client, cfg)
    r = _fire_backup(client, tid, {"type": "incr", "path": "direct",
                                   "sample_interval_s": 0.2, "settle_s": 0.2})
    assert r.status_code == 200
    states = _drain_queue(cfg)
    assert states[-1] == "failed"                 # rc=4 aborted
    run = _last_ops_run(client, "backup")
    assert run["status"] == "aborted"
    events = (cfg.results_dir / "ops" / run["op_run_id"] / "events.jsonl").read_text()
    assert "ABORT: stanza lock held" in events
    assert "rc=50" in events                      # explains the field bug


def test_backup_operator_path_tracks_job(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_backup(client, tid, {"type": "diff", "path": "operator",
                                   "sample_interval_s": 0.2, "settle_s": 0.3,
                                   "timeout_s": 30})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "backup")
    assert run["status"] == "complete", run
    assert run["headline"]["path"] == "operator"
    assert run["headline"]["label"].endswith("D")
    out = (cfg.results_dir / "ops" / run["op_run_id"] / "raw" /
           "trigger_output.txt").read_text()
    assert "Job succeeded" in out


def test_backup_from_replica_records_source(opsweb):
    """A replica-source backup goes through the OPERATOR path (the repo-host Job
    coordinates primary+standby); it records which node and samples both."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_backup(client, tid, {"type": "incr", "path": "operator",
                                   "source": "replica", "timeout_s": 30,
                                   "sample_interval_s": 0.2, "settle_s": 0.3})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "backup")
    assert run["status"] == "complete", run
    h = run["headline"]
    assert h["source_role"] == "replica"
    assert h["source"] != h["leader"]             # the offload target is a replica
    # load sampler covered BOTH nodes
    load = (cfg.results_dir / "ops" / run["op_run_id"] / "parsed" /
            "load.csv").read_text()
    assert h["leader"] in load and h["source"] in load
    # the manual: block carried the one-off standby override (per-backup,
    # no CR-global change needed)
    patches = (Path(os.environ["FAKE_KUBE_STATE"]) / "patches.log").read_text()
    assert "--backup-standby=y" in patches


def test_backup_requires_confirmation_and_mutex(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_backup(client, tid, {"type": "incr"}, confirm="nope")
    assert r.status_code == 400
    r = _fire_backup(client, tid, {"type": "incr", "sample_interval_s": 0.2,
                                   "settle_s": 0.2})
    assert r.status_code == 200
    # second backup on the same target while one is queued -> 409
    r = _fire_backup(client, tid, {"type": "incr"})
    assert r.status_code == 409
    _drain_queue(cfg)


def test_backup_linked_run_id_recorded(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_backup(client, tid, {"type": "incr", "path": "direct",
                                   "sample_interval_s": 0.2, "settle_s": 0.2,
                                   "linked_run_id": "soak-xyz-123"})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "backup")
    assert run["linked_run_id"] == "soak-xyz-123"
    assert run["headline"]["linked_run_id"] == "soak-xyz-123"


# ── Phase 4: failover scenarios (full simulated runs vs the fake cluster) ──

def _fire_scenario(client, tid, case, extra=None, confirm="cluster1"):
    params = {"case": case, "baseline_s": 0.5, "settle_s": 6,
              "recovery_hold_s": 1,
              "probe": {"mode": "direct", "host": "127.0.0.1", "port": 5432,
                        "hz": 5, "sslmode": "disable"}}
    params.update(extra or {})
    return client.post(f"/api/kube-targets/{tid}/scenario",
                       json={"params": params, "confirm": confirm},
                       auth=("admin", "apw"))


def test_scenario_case_b_pgkill_restart_in_place(opsweb):
    """Case B: kill -9 the postmaster. Patroni restarts Postgres in place —
    NOT a failover. Classification must say so (leader name unchanged)."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_scenario(client, tid, "pgkill")
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "scenario")
    assert run["status"] == "complete", run
    h = run["headline"]
    assert h["case"] == "pgkill"
    assert h["flip"] is False and h["kind"] == "restart-in-place"
    assert h["leader_before"] == h["leader_after"]
    assert h["tl_before"] == h["tl_after"]
    assert 500 <= h["downtime_ms"] <= 6000       # ~1.5s simulated restart
    run_dir = cfg.results_dir / "ops" / run["op_run_id"]
    raw = {p.name for p in (run_dir / "raw").iterdir()}
    assert "fire.marker" in raw and "probe.log" in raw
    assert "patroni_samples.jsonl" in raw and "pods_watch.log" in raw
    assert any(n.startswith("patroni_cluster1-instance1") for n in raw)
    assert any(n.startswith("pgbouncer_") for n in raw)
    probe_log = (run_dir / "raw" / "probe.log").read_text()
    assert "FAIL" in probe_log and "OK" in probe_log
    tl = (run_dir / "TIMELINE.txt").read_text()
    assert "NO — restart in place" in tl
    assert (run_dir / "report.html").exists()
    assert (run_dir / "events.csv").exists()
    # the pguser password from the k8s secret never lands in any artifact
    for p in run_dir.rglob("*"):
        if p.is_file():
            assert K8S_PW not in p.read_text(errors="replace"), p


def test_scenario_case_a_switchover_is_election(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_scenario(client, tid, "switchover")
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "scenario")
    assert run["status"] == "complete", run
    h = run["headline"]
    assert h["flip"] is True and h["kind"] == "election"
    assert h["leader_after"] != h["leader_before"]
    assert h["tl_after"] == h["tl_before"] + 1
    tl = (cfg.results_dir / "ops" / run["op_run_id"] / "TIMELINE.txt").read_text()
    assert "YES — real election" in tl


def test_scenario_case_c1_pod_delete_election(opsweb, monkeypatch):
    """C1 under election mode: force-deleted leader loses the lock; a replica
    is promoted. (Default C1 is restart-in-place, covered by the fake's
    non-elect mode via case B semantics.)"""
    client, cfg = opsweb
    monkeypatch.setenv("FAKE_KUBE_C1_ELECT", "1")
    monkeypatch.setenv("FAKE_KUBE_ELECT_S", "2")
    monkeypatch.setenv("FAKE_KUBE_RECREATE_S", "3")
    tid = _ready_target(client, cfg)
    r = _fire_scenario(client, tid, "pod-delete", extra={"settle_s": 8})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "scenario")
    assert run["status"] == "complete", run
    h = run["headline"]
    assert h["flip"] is True and h["kind"] == "election"
    assert h["tl_after"] == h["tl_before"] + 1
    assert h["downtime_ms"] >= 1000


def test_scenario_refuses_to_fire_during_backup(opsweb, monkeypatch):
    """Safety rail: the lock preflight is reused — no fire while a backup
    holds the stanza lock."""
    client, cfg = opsweb
    monkeypatch.setenv("FAKE_KUBE_BACKUP_LOCKED", "1")
    tid = _ready_target(client, cfg)
    r = _fire_scenario(client, tid, "pgkill")
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "scenario")
    assert run["status"] == "aborted"
    run_dir = cfg.results_dir / "ops" / run["op_run_id"]
    assert not (run_dir / "raw" / "fire.marker").exists()   # never fired
    events = (run_dir / "events.jsonl").read_text()
    assert "ABORT: pgBackRest lock held" in events


def test_scenario_mutex_and_confirmation(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    assert _fire_scenario(client, tid, "pgkill", confirm="wrong").status_code == 400
    assert _fire_scenario(client, tid, "pgkill").status_code == 200
    # concurrent scenario on the same target refused
    assert _fire_scenario(client, tid, "switchover").status_code == 409
    # operator (non-admin) cannot fire
    r = client.post(f"/api/kube-targets/{tid}/scenario",
                    json={"params": {"case": "pgkill"}, "confirm": "cluster1"},
                    auth=("op", "oppw"))
    assert r.status_code == 403
    _drain_queue(cfg)


def test_ops_compare_across_scenarios(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    _fire_scenario(client, tid, "pgkill")
    _drain_queue(cfg)
    _fire_scenario(client, tid, "switchover")
    _drain_queue(cfg)
    runs = [r["op_run_id"] for r in
            client.get("/api/ops/runs", auth=("viewer", "vpw")).json()
            if r["kind"] == "scenario"]
    assert len(runs) == 2
    r = client.get(f"/api/ops/compare?runs={','.join(runs)}", auth=("viewer", "vpw"))
    assert r.status_code == 200
    rows = r.json()["runs"]
    by_case = {row["case"]: row for row in rows}
    assert by_case["pgkill"]["classification"] == "restart-in-place"
    assert by_case["switchover"]["classification"] == "election"
    assert by_case["switchover"]["new_primary"] != "—"


def test_ops_sse_streams_scenario_run(opsweb):
    """The live cockpit: hello -> status/events/log -> done over SSE."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    _fire_scenario(client, tid, "pgkill", extra={"settle_s": 3})
    _drain_queue(cfg)
    run = _last_ops_run(client, "scenario")
    with client.stream("GET", f"/ops/runs/{run['op_run_id']}/stream",
                       auth=("viewer", "vpw")) as resp:
        assert resp.status_code == 200
        got = ""
        for chunk in resp.iter_text():
            got += chunk
            if "event: done" in got:
                break
        assert "event: hello" in got
        assert "event: events" in got
        assert "event: status" in got


# ── Phase 5: telemetry monitor ──

def test_monitor_samples_and_survives_failover(opsweb):
    """The monitor re-detects the leader every cycle: a switchover mid-run
    must show BOTH leaders across cycles, with no blank rows (split queries)."""
    client, cfg = opsweb
    import subprocess as sp
    import threading
    tid = _ready_target(client, cfg)
    r = client.post(f"/api/kube-targets/{tid}/monitor",
                    json={"params": {"interval_s": 0.4, "max_duration_s": 5}},
                    auth=("op", "oppw"))
    assert r.status_code == 200, r.text
    # trigger a switchover ~2s into the monitor window, from a side thread
    state_dir = cfg.data_dir.parent / "fakekube"

    def switch_later():
        import time as _t
        _t.sleep(2)
        env = dict(os.environ)
        sp.run([str(FAKEBIN / "kubectl"), "exec", "cluster1-instance1-abcd-0",
                "-c", "database", "--", "patronictl", "switchover",
                "cluster1-ha", "--force"], env=env, capture_output=True)
    th = threading.Thread(target=switch_later)
    th.start()
    _drain_queue(cfg)
    th.join()
    run = _last_ops_run(client, "monitor")
    assert run["status"] == "complete", run
    assert run["headline"]["cycles"] >= 6
    run_dir = cfg.results_dir / "ops" / run["op_run_id"]
    mon = (run_dir / "parsed" / "monitor.csv").read_text().splitlines()
    assert mon[0].startswith("epoch_s,leader,timeline")
    leaders = {ln.split(",")[1] for ln in mon[1:] if ln.split(",")[1]}
    assert len(leaders) == 2                      # saw both leaders
    tls = {ln.split(",")[2] for ln in mon[1:] if ln.split(",")[2]}
    assert {"5", "6"} <= tls                      # TL bump captured
    # split queries: wal + archiver + queue populated on leader rows
    data_rows = [ln.split(",") for ln in mon[1:]]
    assert any(rw[3] for rw in data_rows)         # wal_bytes
    assert any(rw[6] for rw in data_rows)         # archived_count
    assert any(rw[8] for rw in data_rows)         # archive_queue
    repl = (run_dir / "parsed" / "replication.csv").read_text().splitlines()
    assert len(repl) >= 3                         # 2 replicas per cycle
    disk = (run_dir / "parsed" / "disk.csv").read_text().splitlines()
    assert disk[0] == "epoch_s,pod,pgdata_used,pgdata_use_pct"
    assert any(ln.endswith("38%") for ln in disk[1:])   # Use% from df -P


def test_monitor_lane_does_not_consume_concurrency(opsweb):
    """An ops_monitor job must not block benchmark jobs (its own lane)."""
    client, cfg = opsweb
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    tid = _ready_target(client, cfg)
    client.post(f"/api/kube-targets/{tid}/monitor",
                json={"params": {"interval_s": 0.5, "max_duration_s": 60}},
                auth=("op", "oppw"))
    conn = connect(cfg.db_path)
    try:
        job = queries.claim_next_job(conn, 1)
        assert job is not None and job["kind"] == "ops_monitor"
        queries.update_job(conn, job["id"], state="running")
        # with max_concurrency=1 and a monitor 'running', a benchmark job
        # must still be claimable
        assert queries.running_count(conn) == 0
        # duplicate monitor on the same target refused
        r = client.post(f"/api/kube-targets/{tid}/monitor",
                        json={"params": {}}, auth=("op", "oppw"))
        assert r.status_code == 409
        queries.update_job(conn, job["id"], state="canceled")
    finally:
        conn.close()


# ── bug-bash regressions (worker/queue/reconcile) ──

def test_crash_recovery_converges_ops_run_and_clears_mutex(opsweb):
    """After a worker crash mid-scenario, reconcile must drive the orphaned job
    AND its op run terminal, and the per-target mutex must not stay wedged."""
    client, cfg = opsweb
    from pgbench_webapp import ops_support, queries
    from pgbench_webapp.db import connect
    from pgbench_harness.ops.oprun import OpsRun
    tid = _ready_target(client, cfg)
    # simulate a scenario job that was claimed, started, wrote its run dir, then
    # the worker was SIGKILLed before it could finalize (run stuck 'running').
    conn = connect(cfg.db_path)
    run = OpsRun(cfg.results_dir, "scenario", "crash-test",
                 target={"name": "doks-test", "cr_name": "cluster1",
                         "cr_kind": "perconapgcluster", "namespace": "percona"},
                 params={"case": "pgkill"})
    jid = queries.enqueue_job(conn, "ops_scenario", "op: scenario\n", None, "admin",
                              kube_target_id=tid)
    queries.update_job(conn, jid, state="running", pid=2, run_id=run.op_run_id)
    ops_support.index_ops_run(cfg, conn, run.op_run_id, queries.get_job(conn, jid))
    # generic startup loop marks it failed first (the ordering bug); ops reconcile
    # must still converge the RUN.
    queries.update_job(conn, jid, state="failed")   # as the generic loop would
    n = ops_support.reconcile_stale_ops_jobs(cfg, conn, startup=True)
    assert n >= 1
    detail = client.get(f"/api/ops/runs/{run.op_run_id}", auth=("viewer", "vpw")).json()
    assert detail["meta"]["status"] in ("failed", "canceled")   # not stuck 'running'
    idx = queries.get_ops_run(conn, run.op_run_id)
    assert idx["status"] in ("failed", "canceled")
    # mutex is clear: a new scenario is accepted
    r = client.post(f"/api/kube-targets/{tid}/scenario",
                    json={"confirm": "cluster1",
                          "params": {"case": "pgkill", "baseline_s": 0.3, "settle_s": 2,
                                     "probe": {"mode": "off"}}},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    conn.close()
    _drain_queue(cfg)


def test_reconcile_spares_freshly_claimed_pidless_job(opsweb):
    """A running ops job with no pid yet (claimed, run_job hasn't recorded the
    pid) must NOT be reaped by the opportunistic (non-startup) reconcile."""
    client, cfg = opsweb
    from pgbench_webapp import ops_support, queries
    from pgbench_webapp.db import connect
    tid = _ready_target(client, cfg)
    conn = connect(cfg.db_path)
    jid = queries.enqueue_job(conn, "ops_monitor", "op: monitor\n", None, "op",
                              kube_target_id=tid)
    queries.update_job(conn, jid, state="running", pid=None)   # claim window
    ops_support.reconcile_stale_ops_jobs(cfg, conn, startup=False)
    assert queries.get_job(conn, jid)["state"] == "running"    # spared
    # but the startup pass (no concurrent claims) DOES reap it
    ops_support.reconcile_stale_ops_jobs(cfg, conn, startup=True)
    assert queries.get_job(conn, jid)["state"] == "failed"
    conn.close()


def test_mutex_atomic_enqueue_rejects_second(opsweb):
    """enqueue_ops_job_atomic must reject a second destructive op on a target
    that already has one active (the TOCTOU the plain check allowed)."""
    client, cfg = opsweb
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    tid = _ready_target(client, cfg)
    conn = connect(cfg.db_path)
    mutex = ("ops_scenario", "ops_backup", "ops_cr_apply")
    a = queries.enqueue_ops_job_atomic(conn, "ops_scenario", "op: scenario\n",
                                       "admin", tid, mutex)
    b = queries.enqueue_ops_job_atomic(conn, "ops_backup", "op: backup\n",
                                       "admin", tid, mutex)
    assert a is not None and b is None            # second blocked
    # a non-mutex op (validate) always enqueues
    c = queries.enqueue_ops_job_atomic(conn, "ops_validate", "op: validate\n",
                                       "admin", tid, ())
    assert c is not None
    conn.close()


def test_invalid_ops_params_rejected_at_api(opsweb):
    """Bad params get a clean 400 at enqueue, not a job that dies 'exit 2'."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    for path, body in [
        ("scenario", {"confirm": "cluster1", "params": {"case": "bogus"}}),
        ("scenario", {"confirm": "cluster1", "params": "notadict"}),
        ("backup", {"confirm": "cluster1", "params": {"type": "bogus"}}),
    ]:
        r = client.post(f"/api/kube-targets/{tid}/{path}", json=body,
                        auth=("admin", "apw"))
        assert r.status_code == 400, f"{path} {body} -> {r.status_code}"


def test_worker_loop_monitor_does_not_block_benchmarks(opsweb, monkeypatch):
    """The queue wedge fix: a 'running' monitor thread must not stop the loop
    from claiming other jobs (the len(active) gate now excludes monitors)."""
    client, cfg = opsweb
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    import threading, time
    tid = _ready_target(client, cfg)
    conn = connect(cfg.db_path)
    queries.set_setting(conn, "max_concurrency", "1")
    # a long-lived monitor already 'running' in a fake thread
    mid = queries.enqueue_job(conn, "ops_monitor", "op: monitor\n", None, "op",
                              kube_target_id=tid)
    queries.update_job(conn, mid, state="running", pid=1)
    # a queued benchmark run
    bid = queries.enqueue_job(conn, "run", _spec_yaml_bench(), None, "admin")
    # emulate the loop's admission test: slotted (non-monitor) active vs max_conc
    active = {mid: (threading.Thread(target=lambda: None), "ops_monitor")}
    slotted = sum(1 for _t, k in active.values() if k != "ops_monitor")
    assert slotted == 0                       # monitor doesn't fill the slot
    job = queries.claim_next_job(conn, 1)     # benchmark still claimable
    assert job is not None and job["id"] == bid
    conn.close()


def _spec_yaml_bench():
    return ("run:\n  label: t\n  edition: advanced\n  tshirt_size: 4c16g\n"
            "target:\n  host: h\n  port: 5432\n  database: d\n  user: u\n"
            "  password_env: PGB_TARGET_PASSWORD\n  sslmode: require\n"
            "workload:\n  type: oltp_read_write\n  tables: 1\n  table_size: 10\n"
            "sweep:\n  threads: [1]\n  duration_s: 1\n  warmup_s: 0\n"
            "  cooldown_s: 0\n  repetitions: 1\n")


def test_monitor_disk_records_usage_not_device(opsweb):
    """Regression: disk.csv must record Used/Use% from df -P, not the device."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = client.post(f"/api/kube-targets/{tid}/monitor",
                    json={"params": {"interval_s": 0.3, "max_duration_s": 1.5}},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "monitor")
    disk = (cfg.results_dir / "ops" / run["op_run_id"] / "parsed" / "disk.csv")
    lines = disk.read_text().splitlines()
    assert lines[0] == "epoch_s,pod,pgdata_used,pgdata_use_pct"
    row = lines[1].split(",")
    assert row[2].isdigit()                   # Used blocks, not "/dev/sda1"
    assert row[3].endswith("%")               # Use%


def test_recreate_db_rejects_injection_name(opsweb):
    """SQL-injection guard: a db name that isn't a plain identifier is refused,
    never interpolated into DROP/CREATE DATABASE."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = client.post(f"/api/kube-targets/{tid}/cr-apply",
                    json={"confirm": "cluster1",
                          "params": {"action": "patroni_params",
                                     "parameters": {"max_wal_size": "8192"},
                                     "verify_timeout_s": 8,
                                     "prep": {"recreate_db": 'x"; DROP DATABASE prod; --',
                                              "confirm": 'x"; DROP DATABASE prod; --'}}},
                    auth=("admin", "apw"))
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "cr-apply")
    events = (cfg.results_dir / "ops" / run["op_run_id"] / "events.jsonl").read_text()
    # The name is refused (never interpolated into DROP/CREATE DATABASE); it
    # only appears, truncated, inside the human-readable refusal reason.
    assert "recreate_db refused" in events
    assert "not a valid database name" in events
    assert "recreated" not in events     # no "database '<name>' recreated" event


# ── regression: validate must never crash on an unreadable/hidden kubeconfig ──

def test_probe_kubeconfig_handles_all_failures(tmp_path, monkeypatch):
    from pgbench_harness.ops.validate import _probe_kubeconfig
    import os as _os
    # missing
    ok, msg = _probe_kubeconfig(str(tmp_path / "nope.yaml"))
    assert ok is False and "not found" in msg
    # a directory, not a file
    ok, msg = _probe_kubeconfig(str(tmp_path))
    assert ok is False and "not a regular file" in msg
    # a real readable file
    kc = tmp_path / "kc.yaml"; kc.write_text("apiVersion: v1\n")
    ok, msg = _probe_kubeconfig(str(kc))
    assert ok is True
    # permission denied (the /root ProtectHome case) — simulated so it works as root
    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(_os, "stat", boom)
    ok, msg = _probe_kubeconfig("/root/kubeconfig-x")
    assert ok is False
    assert "not accessible" in msg and "kubeconfigs/" in msg   # the actionable hint


def test_validate_reports_cleanly_when_kubeconfig_not_a_file(opsweb, tmp_path):
    """A kubeconfig path that isn't a regular file (e.g. a directory) must make
    validate FAIL with a clear checklist line, never crash. The permission-denied
    (/root ProtectHome) branch is covered by test_probe_kubeconfig_handles_all_failures
    — it can't be exercised through the subprocess as root."""
    client, cfg = opsweb
    d = tmp_path / "notafile"; d.mkdir()
    _create_target(client, upload=False, name="dirkc", kubeconfig_path=str(d))
    _id, state, job = _run_worker_once(cfg)
    assert state == "failed"
    out = (cfg.data_dir / "jobs" / f"job_{job['id']}.out").read_text()
    assert "Traceback" not in out
    assert "not a regular file" in out
    assert "OPS_SUMMARY_JSON" in out


def test_backup_replica_direct_aborts_with_clear_message(opsweb):
    """PGO architecture guard: a direct-exec backup with source=replica can't
    reach the primary (rc=56); it must abort with a clear message pointing at
    the operator path, not fire a doomed backup."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_backup(client, tid, {"type": "full", "path": "direct",
                                   "source": "replica",
                                   "sample_interval_s": 0.2, "settle_s": 0.2})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "backup")
    assert run["status"] == "aborted"
    assert run["headline"]["source_role"] == "replica"
    events = (cfg.results_dir / "ops" / run["op_run_id"] / "events.jsonl").read_text()
    assert "replica-source backup needs the operator path" in events
    assert "Source = leader" in events
    # replica via the OPERATOR path is allowed (no abort at this guard)
    r = _fire_backup(client, tid, {"type": "incr", "path": "operator",
                                   "source": "replica",
                                   "sample_interval_s": 0.2, "settle_s": 0.3,
                                   "timeout_s": 30})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "backup")
    assert run["status"] in ("complete", "failed")   # not aborted by the guard
    assert run["headline"].get("reason") != "replica-direct-unsupported"


# ── PMM 3.x enablement (ops pmm-enable / pmm-status / pmm-disable) ──

PMM_TOKEN = "glsa_pmm-token-SENTINEL-do-not-leak-77aa88bb"


@pytest.fixture()
def pmmops(tmp_path, monkeypatch):
    """Fake cluster + PMM token env for direct ops-runner calls (no webapp:
    the pmm verbs are exercised at the framework layer, like the CLI would)."""
    for exe in ("psql", "kubectl"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    import sys
    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{venv_bin}{os.pathsep}{os.environ['PATH']}")
    state = tmp_path / "fakekube"; state.mkdir()
    monkeypatch.setenv("FAKE_KUBE_STATE", str(state))
    monkeypatch.setenv("FAKE_KUBE_RESTART_S", "0")   # no psql-outage windows
    monkeypatch.setenv("FAKE_KUBE_RECREATE_S", "0")  # deleted pods return instantly
    monkeypatch.setenv("FAKE_KUBE_ROLL_S", "0.2")    # staged PMM roll, fast
    monkeypatch.setenv("PGB_PMM_TOKEN", PMM_TOKEN)
    return tmp_path / "results"


def _pmm_ops_spec(op, **params):
    from pgbench_harness.ops.opspec import parse_ops_spec
    # 127.0.0.1:9 fails fast (connection refused, proxy-exempt) so tests that
    # don't stand up an inventory server hit the degrade-to-warning path quickly
    base = {"server_host": "http://127.0.0.1:9", "poll_s": 0.2,
            "rollout_timeout_s": 30, "discover_timeout_s": 10,
            "qan_timeout_s": 10}
    base.update(params)
    return parse_ops_spec({"op": op, "label": f"{op}-test",
                           "target": {"name": "doks-test", "namespace": "percona",
                                      "cr_name": "cluster1"},
                           "params": base})


def _only_pmm_run_dir(results, op):
    dirs = [d for d in (results / "ops").iterdir() if d.name.startswith(op + "-")]
    assert len(dirs) == 1, dirs
    return dirs[0]


def _fake_state():
    return json.loads((Path(os.environ["FAKE_KUBE_STATE"]) / "state.json").read_text())


def _start_pmm_inventory(services):
    """Minimal PMM3 inventory API: GET /v1/inventory/services with Bearer auth."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization", "")
            seen["path"] = self.path
            body = json.dumps({"services":
                               [{"service_name": s} for s in services]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_port, seen


def test_pmm_enable_end_to_end_with_inventory_confirmation(pmmops):
    from pgbench_harness.ops.pmm import run_pmm_enable
    pods = ["cluster1-instance1-abcd-0", "cluster1-instance1-efgh-0",
            "cluster1-instance1-ijkl-0"]
    srv, port, seen = _start_pmm_inventory(pods)
    try:
        rc = run_pmm_enable(
            _pmm_ops_spec("pmm-enable", server_host=f"http://127.0.0.1:{port}"),
            pmmops)
    finally:
        srv.shutdown()
    assert rc == 0
    run_dir = _only_pmm_run_dir(pmmops, "pmm-enable")
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["status"] == "complete", meta
    h = meta["headline"]
    assert h["qan"] is True and h["healthy"] is True and h["rolled"] is True
    assert h["inventory_nodes"] == 3
    # server-side confirmation hit the PMM3 inventory endpoint with Bearer auth
    assert seen["auth"] == f"Bearer {PMM_TOKEN}"
    assert "/v1/inventory/services" in seen["path"]
    # validation artifacts: per-node rows + human report
    val = json.loads((run_dir / "validation.json").read_text())
    assert len(val["nodes"]) == 3
    assert sum(1 for n in val["nodes"] if n["role"] == "LEADER") == 1
    assert all(n["extension"] and n["sidecar_ready"] for n in val["nodes"])
    assert val["pmm3_mode"]["prerun_query_source"] is True
    assert val["pmm3_mode"]["legacy_pmm2_path"] is False
    report = (run_dir / "validation-report.txt").read_text()
    assert "VALIDATION REPORT" in report and "QAN observed" in report
    # pre-mutation state backup exists (with the one-line restore path)
    assert (run_dir / "backup" / "cr-cluster1.yaml").exists()
    assert (run_dir / "backup" / "preload-and-extensions.txt").exists()
    assert (run_dir / "backup" / "patroni-show-config.yaml").exists()
    # cluster converged: pmm block, PMM3 secret, extension, SPL
    st = _fake_state()
    assert st["cr"]["spec"]["pmm"]["enabled"] is True
    assert st["cr"]["spec"]["pmm"]["querySource"] == "pgstatmonitor"
    assert st["pmm_secret"] == "cluster1-pmm-secret"
    assert "pg_stat_monitor" in st["extensions"]
    spl = (st["cr"]["spec"]["patroni"]["dynamicConfiguration"]["postgresql"]
           ["parameters"]["shared_preload_libraries"])
    assert spl == "pgaudit,pg_stat_monitor"
    # HA-preserving bounce: the leader is deleted LAST, after every replica
    deletes = [json.loads(ln) for ln in
               (run_dir / "events.jsonl").read_text().splitlines()
               if '"bounce"' in ln and "deleting pod" in ln]
    assert len(deletes) == 3
    assert "cluster1-instance1-abcd-0" in deletes[-1]["label"]   # the leader
    # DoD: the token never lands in anything the harness writes
    for p in pmmops.rglob("*"):
        if p.is_file():
            assert PMM_TOKEN not in p.read_text(errors="replace"), p


def test_pmm_enable_dry_run_redacts_token_and_mutates_nothing(pmmops):
    from pgbench_harness.ops.pmm import run_pmm_enable
    rc = run_pmm_enable(_pmm_ops_spec("pmm-enable", dry_run=True), pmmops)
    assert rc == 0
    run_dir = _only_pmm_run_dir(pmmops, "pmm-enable")
    events = (run_dir / "events.jsonl").read_text()
    assert "<token>" in events                     # placeholder, like the script
    assert PMM_TOKEN not in events
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["status"] == "complete" and meta["headline"]["dry_run"] is True
    st = _fake_state()
    assert "pmm" not in st["cr"]["spec"] and st["pmm_secret"] == ""
    assert not (Path(os.environ["FAKE_KUBE_STATE"]) / "patches.log").exists()


def test_pmm_rollout_wait_is_spec_aware_not_fooled_by_ready_old_pods(pmmops, monkeypatch):
    """Reference-script bug #1, fixed: every pod stays Running+Ready on the OLD
    spec the whole time (a naive readiness poll would sail through instantly).
    With the operator's roll stalled the spec-aware wait must time out, record
    why, and finish as a warning — never a silent success."""
    from pgbench_harness.ops.pmm import run_pmm_enable
    monkeypatch.setenv("FAKE_KUBE_ROLL_S", "600")   # pods never pick up the spec
    rc = run_pmm_enable(
        _pmm_ops_spec("pmm-enable", rollout_timeout_s=1.2, poll_s=0.3,
                      qan_timeout_s=0.5, discover_timeout_s=5), pmmops)
    assert rc == 1                                   # warning, not success
    run_dir = _only_pmm_run_dir(pmmops, "pmm-enable")
    events = (run_dir / "events.jsonl").read_text()
    assert "TIMEOUT waiting for rollout" in events
    assert "old spec" in events                      # the recorded reason
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["status"] == "warning"
    assert meta["headline"]["rolled"] is False
    assert meta["headline"]["healthy"] is False


def test_pmm_status_reports_without_mutating(pmmops):
    from pgbench_harness.ops.pmm import run_pmm_enable, run_pmm_status
    assert run_pmm_enable(_pmm_ops_spec("pmm-enable"), pmmops) == 0
    state_path = Path(os.environ["FAKE_KUBE_STATE"]) / "state.json"
    patches_path = Path(os.environ["FAKE_KUBE_STATE"]) / "patches.log"
    before_state, before_patches = state_path.read_text(), patches_path.read_text()
    rc = run_pmm_status(_pmm_ops_spec("pmm-status"), pmmops)
    assert rc == 0
    assert state_path.read_text() == before_state       # zero mutations
    assert patches_path.read_text() == before_patches   # no CR patches at all
    run_dir = _only_pmm_run_dir(pmmops, "pmm-status")
    val = json.loads((run_dir / "validation.json").read_text())
    assert val["qan_observed"] is True and len(val["nodes"]) == 3


def test_pmm_disable_restores_cr_and_deletes_secret(pmmops):
    from pgbench_harness.ops.pmm import run_pmm_disable, run_pmm_enable
    assert run_pmm_enable(_pmm_ops_spec("pmm-enable"), pmmops) == 0
    assert _fake_state()["cr"]["spec"].get("pmm", {}).get("enabled") is True
    enable_id = _only_pmm_run_dir(pmmops, "pmm-enable").name
    rc = run_pmm_disable(_pmm_ops_spec("pmm-disable", rollback_of=enable_id),
                         pmmops)
    assert rc == 0
    st = _fake_state()
    assert "pmm" not in st["cr"]["spec"]                # pre-enable CR restored
    assert st["pmm_secret"] == ""                       # secret deleted
    meta = json.loads((_only_pmm_run_dir(pmmops, "pmm-disable") / "meta.json")
                      .read_text())
    assert meta["status"] == "complete"
    assert meta["headline"]["restored_from"] == enable_id


def test_pmm_disable_requires_a_real_backup(pmmops):
    from pgbench_harness.ops.pmm import run_pmm_disable
    rc = run_pmm_disable(_pmm_ops_spec("pmm-disable", rollback_of="nope-123"),
                         pmmops)
    assert rc == 3
    meta = json.loads((_only_pmm_run_dir(pmmops, "pmm-disable") / "meta.json")
                      .read_text())
    assert meta["status"] == "aborted"
    assert meta["headline"]["reason"] == "no-backup"


def test_pmm_enable_aborts_cleanly_without_token(pmmops, monkeypatch):
    from pgbench_harness.ops.pmm import run_pmm_enable
    monkeypatch.delenv("PGB_PMM_TOKEN")
    rc = run_pmm_enable(_pmm_ops_spec("pmm-enable"), pmmops)
    assert rc == 3
    meta = json.loads((_only_pmm_run_dir(pmmops, "pmm-enable") / "meta.json")
                      .read_text())
    assert meta["status"] == "aborted"
    assert meta["headline"]["reason"] == "no-token"
    events = (_only_pmm_run_dir(pmmops, "pmm-enable") / "events.jsonl").read_text()
    assert "PGB_PMM_TOKEN is not set" in events


def test_pmm_token_prefix_warning_not_fatal(pmmops, monkeypatch):
    from pgbench_harness.ops.pmm import run_pmm_enable
    monkeypatch.setenv("PGB_PMM_TOKEN", "not-a-glsa-token-SENTINEL-123")
    rc = run_pmm_enable(_pmm_ops_spec("pmm-enable", dry_run=True), pmmops)
    assert rc == 0                                       # warn, don't fail
    events = (_only_pmm_run_dir(pmmops, "pmm-enable") / "events.jsonl").read_text()
    assert "does not start with 'glsa_'" in events
    assert "not-a-glsa-token-SENTINEL-123" not in events # still redacted


def test_resolve_leader_via_role_label_and_patronictl_fallback(pmmops, monkeypatch):
    """Reference-script bug #2, fixed: discovery prefers the operator's role
    label (no exec at all) and falls back to patronictl against every pod."""
    from pgbench_harness.ops.discover import resolve_leader_resilient
    from pgbench_harness.ops.kube import Kube
    kube = Kube(namespace="percona")
    instances, leader, view, attempts = resolve_leader_resilient(
        kube, "cluster1", timeout_s=5, poll_s=0.2)
    assert leader == "cluster1-instance1-abcd-0"
    assert len(instances) == 3
    assert "role label" in attempts[-1]
    # older operator without the label -> patronictl path finds the same leader
    monkeypatch.setenv("FAKE_KUBE_NO_ROLE_LABEL", "1")
    instances, leader, view, attempts = resolve_leader_resilient(
        kube, "cluster1", timeout_s=5, poll_s=0.2)
    assert leader == "cluster1-instance1-abcd-0"
    assert "patronictl" in attempts[-1]
    assert view is not None and view.leader_name == leader


def test_resolve_leader_deadline_error_carries_attempts(pmmops, monkeypatch):
    from pgbench_harness.ops.discover import resolve_leader_resilient
    from pgbench_harness.ops.kube import Kube, KubeError
    monkeypatch.setenv("FAKE_KUBE_AUTH_FAIL", "1")
    with pytest.raises(KubeError) as exc:
        resolve_leader_resilient(Kube(namespace="percona"), "cluster1",
                                 timeout_s=0.7, poll_s=0.2)
    msg = str(exc.value)
    assert "leader discovery failed" in msg and "attempts" in msg


def test_pmm_ops_spec_validation():
    from pgbench_harness.errors import SpecError
    from pgbench_harness.ops.opspec import parse_ops_spec
    tgt = {"name": "t", "cr_name": "cluster1"}
    with pytest.raises(SpecError, match="server_host"):
        parse_ops_spec({"op": "pmm-enable", "target": tgt, "params": {}})
    with pytest.raises(SpecError, match="query_source"):
        parse_ops_spec({"op": "pmm-enable", "target": tgt,
                        "params": {"server_host": "x", "query_source": "topsql"}})
    with pytest.raises(SpecError, match="extension"):
        parse_ops_spec({"op": "pmm-enable", "target": tgt,
                        "params": {"server_host": "x",
                                   "extension": "pg_buffercache"}})
    with pytest.raises(SpecError, match="rollback_of"):
        parse_ops_spec({"op": "pmm-disable", "target": tgt, "params": {}})
    ok = parse_ops_spec({"op": "pmm-status", "target": tgt,
                         "params": {"server_host": "pmm.example.com"}})
    assert ok.op == "pmm-status"


def test_build_pmm_links_scoped_to_run_window():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from pgbench_harness.report import build_pmm_links
    from pgbench_harness.spec import Pmm
    spec = SimpleNamespace(pmm=Pmm(server_host="pmm.example.com",
                                   service_name="cluster1-instance1"))
    links = build_pmm_links(spec, "2026-07-17T10:00:00Z", "2026-07-17T11:00:00Z")
    frm = int(datetime(2026, 7, 17, 10, tzinfo=timezone.utc).timestamp() * 1000)
    to = frm + 3_600_000
    assert links["from_ms"] == frm and links["to_ms"] == to
    assert links["instances"] == ("https://pmm.example.com/graph/d/"
                                  "postgresql-instance-overview/"
                                  f"postgresql-instances-overview?from={frm}&to={to}")
    assert "pmm-qan" in links["qan"] and f"from={frm}&to={to}" in links["qan"]
    assert "var-service_name=cluster1-instance1" in links["qan"]
    assert build_pmm_links(SimpleNamespace(pmm=None), "x", "y") is None


def test_merge_spl_preserves_and_dedupes():
    from pgbench_harness.ops.pmm import _merge_spl
    assert _merge_spl("pgaudit,pgvector", "pg_stat_monitor") == \
        "pgaudit,pgvector,pg_stat_monitor"
    assert _merge_spl(" pgaudit , pg_stat_monitor ", "pg_stat_monitor") == \
        "pgaudit,pg_stat_monitor"                     # already there: no dupe
    assert _merge_spl("", "pg_stat_monitor") == "pgaudit,pg_stat_monitor"
    assert _merge_spl("pgvector,pgaudit,pgvector", "pg_stat_statements") == \
        "pgvector,pgaudit,pg_stat_statements"         # order kept, dupes dropped


def test_pmm_enable_preserves_existing_preload_libraries(pmmops):
    """The cluster already loads custom libraries (pgvector, pg_cron): the PMM
    patch must append the extension, never clobber the existing list."""
    import subprocess as sp

    from pgbench_harness.ops.pmm import run_pmm_enable
    sp.run([str(FAKEBIN / "kubectl"), "patch", "perconapgcluster", "cluster1",
            "--type", "merge", "-p", json.dumps({"spec": {"patroni": {
                "dynamicConfiguration": {"postgresql": {"parameters": {
                    "shared_preload_libraries": "pgaudit,pgvector,pg_cron"}}}}}})],
           env=dict(os.environ), capture_output=True, check=True)
    rc = run_pmm_enable(_pmm_ops_spec("pmm-enable"), pmmops)
    assert rc == 0
    st = _fake_state()
    spl = (st["cr"]["spec"]["patroni"]["dynamicConfiguration"]["postgresql"]
           ["parameters"]["shared_preload_libraries"])
    assert spl == "pgaudit,pgvector,pg_cron,pg_stat_monitor"
    run_dir = _only_pmm_run_dir(pmmops, "pmm-enable")
    events = (run_dir / "events.jsonl").read_text()
    assert "pgaudit,pgvector,pg_cron,pg_stat_monitor" in events
    assert "preserved" in events
    # validation verified every preserved library, not just the extension
    val = json.loads((run_dir / "validation.json").read_text())
    assert val["libs"] == {"pgaudit": True, "pgvector": True, "pg_cron": True,
                           "pg_stat_monitor": True}


# ── PMM via the console API (web routes) ──

def test_pmm_web_enable_status_disable_flow(opsweb, monkeypatch):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    monkeypatch.setenv("PGB_PMM_TOKEN", PMM_TOKEN)
    monkeypatch.setenv("FAKE_KUBE_RESTART_S", "0")
    monkeypatch.setenv("FAKE_KUBE_RECREATE_S", "0")
    monkeypatch.setenv("FAKE_KUBE_ROLL_S", "0.2")
    params = {"server_host": "http://127.0.0.1:9", "poll_s": 0.2,
              "rollout_timeout_s": 30, "discover_timeout_s": 10,
              "qan_timeout_s": 10}
    # enable: admin + typed confirmation
    r = client.post(f"/api/kube-targets/{tid}/pmm/enable",
                    json={"confirm": "cluster1", "params": params},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "pmm-enable")
    assert run["status"] == "complete", run
    assert run["headline"]["healthy"] is True
    enable_id = run["op_run_id"]
    # status: operator role, no confirmation, zero mutations
    r = client.post(f"/api/kube-targets/{tid}/pmm/status",
                    json={"params": {"server_host": "http://127.0.0.1:9"}},
                    auth=("op", "oppw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "pmm-status")
    assert run["status"] == "complete", run
    # disable: rollback_of auto-resolves to the newest enable run's backup
    r = client.post(f"/api/kube-targets/{tid}/pmm/disable",
                    json={"confirm": "cluster1", "params": {}},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text
    _drain_queue(cfg)
    run = _last_ops_run(client, "pmm-disable")
    assert run["status"] == "complete", run
    assert run["headline"]["restored_from"] == enable_id
    state = json.loads((cfg.data_dir.parent / "fakekube" / "state.json").read_text())
    assert "pmm" not in state["cr"]["spec"]
    # the token sentinel never landed anywhere the webapp writes
    for path in cfg.data_dir.rglob("*"):
        if path.is_file() and "fakekube" not in str(path) \
                and path.name != "secrets.enc":
            assert PMM_TOKEN not in path.read_text(encoding="utf-8",
                                                   errors="replace"), path


def test_pmm_web_rbac_confirm_and_validation(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    host = {"server_host": "pmm.example.com"}
    # viewer: nothing; operator: status only, not enable/disable
    r = client.post(f"/api/kube-targets/{tid}/pmm/status",
                    json={"params": host}, auth=("viewer", "vpw"))
    assert r.status_code == 403
    r = client.post(f"/api/kube-targets/{tid}/pmm/enable",
                    json={"confirm": "cluster1", "params": host},
                    auth=("op", "oppw"))
    assert r.status_code == 403
    # admin without typed confirmation -> 400 (unless dry-run)
    r = client.post(f"/api/kube-targets/{tid}/pmm/enable",
                    json={"params": host}, auth=("admin", "apw"))
    assert r.status_code == 400 and "confirmation" in r.text
    r = client.post(f"/api/kube-targets/{tid}/pmm/enable",
                    json={"params": {**host, "dry_run": True}},
                    auth=("admin", "apw"))
    assert r.status_code == 200, r.text          # dry-run: no confirm needed
    # missing server_host -> clean 400, nothing enqueued
    r = client.post(f"/api/kube-targets/{tid}/pmm/enable",
                    json={"confirm": "cluster1", "params": {}},
                    auth=("admin", "apw"))
    assert r.status_code == 400 and "server_host" in r.text
    # disable with no enable run to roll back to -> clean 400
    r = client.post(f"/api/kube-targets/{tid}/pmm/disable",
                    json={"confirm": "cluster1", "params": {}},
                    auth=("admin", "apw"))
    assert r.status_code == 400 and "nothing to restore" in r.text


# ── bug-bash round 3 regressions (PMM surface) ──

def test_pmm_enable_twice_is_idempotent(pmmops):
    """Re-enabling an already-monitored cluster must converge, not break:
    no duplicate libs, secret refreshed, run completes healthy."""
    from pgbench_harness.ops.pmm import run_pmm_enable
    assert run_pmm_enable(_pmm_ops_spec("pmm-enable"), pmmops) == 0
    assert run_pmm_enable(_pmm_ops_spec("pmm-enable"), pmmops) == 0
    st = _fake_state()
    spl = (st["cr"]["spec"]["patroni"]["dynamicConfiguration"]["postgresql"]
           ["parameters"]["shared_preload_libraries"])
    assert spl == "pgaudit,pg_stat_monitor"          # merged, not doubled
    assert st["pmm_secret"] == "cluster1-pmm-secret"


def test_pmm_disable_rejects_traversal_rollback_id(pmmops):
    from pgbench_harness.ops.pmm import run_pmm_disable
    rc = run_pmm_disable(
        _pmm_ops_spec("pmm-disable", rollback_of="../../../../etc"), pmmops)
    assert rc == 3
    meta = json.loads((_only_pmm_run_dir(pmmops, "pmm-disable") / "meta.json")
                      .read_text())
    assert meta["status"] == "aborted"
    assert meta["headline"]["reason"] == "bad-rollback-id"


def test_pmm_disable_strips_server_owned_fields_on_restore(pmmops):
    """The backed-up CR dump carries resourceVersion/uid/creationTimestamp and
    status; re-applying those can be rejected as a conflict. The restore must
    strip them (and report the reconcile actually completed)."""
    from pgbench_harness.ops.pmm import run_pmm_disable, run_pmm_enable
    assert run_pmm_enable(_pmm_ops_spec("pmm-enable"), pmmops) == 0
    enable_id = _only_pmm_run_dir(pmmops, "pmm-enable").name
    # the backup itself DOES contain the server-owned fields (raw truth)
    backup = (_only_pmm_run_dir(pmmops, "pmm-enable") / "backup"
              / "cr-cluster1.yaml").read_text()
    assert "resourceVersion" in backup and '"status"' in backup
    rc = run_pmm_disable(_pmm_ops_spec("pmm-disable", rollback_of=enable_id),
                         pmmops)
    assert rc == 0
    st = _fake_state()
    assert "resourceVersion" not in st["cr"]["metadata"]
    assert "status" not in st["cr"]
    assert "pmm" not in st["cr"]["spec"]
    meta = json.loads((_only_pmm_run_dir(pmmops, "pmm-disable") / "meta.json")
                      .read_text())
    assert meta["headline"]["reconciled"] is True


def test_pmm_token_whitespace_is_stripped(pmmops, monkeypatch):
    """A pasted token with a trailing newline must not corrupt the Bearer
    header/secret or trigger a bogus 'not glsa_' warning."""
    from pgbench_harness.ops.pmm import run_pmm_enable
    monkeypatch.setenv("PGB_PMM_TOKEN", "  glsa_padded_token_SENTINEL_42\n")
    rc = run_pmm_enable(_pmm_ops_spec("pmm-enable", dry_run=True), pmmops)
    assert rc == 0
    events = (_only_pmm_run_dir(pmmops, "pmm-enable") / "events.jsonl").read_text()
    assert "does not start with 'glsa_'" not in events
    assert "glsa_padded_token_SENTINEL_42" not in events
    # fingerprint of the STRIPPED token, for shell comparison — never the token
    import hashlib
    fp = hashlib.sha256(b"glsa_padded_token_SENTINEL_42").hexdigest()[:12]
    assert f"sha256 {fp}" in events and "29 chars" in events


def test_pmm_web_enable_mutex_blocks_second_destructive(opsweb, monkeypatch):
    """Two enables can't be queued at once — the shared one-destructive-op
    mutex rejects the second with a 409 while the first is still queued."""
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    monkeypatch.setenv("PGB_PMM_TOKEN", PMM_TOKEN)
    body = {"confirm": "cluster1",
            "params": {"server_host": "http://127.0.0.1:9", "poll_s": 0.2,
                       "rollout_timeout_s": 20, "discover_timeout_s": 10,
                       "qan_timeout_s": 5}}
    r1 = client.post(f"/api/kube-targets/{tid}/pmm/enable", json=body,
                     auth=("admin", "apw"))
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/api/kube-targets/{tid}/pmm/enable", json=body,
                     auth=("admin", "apw"))
    assert r2.status_code == 409                      # queued job holds the mutex
    # and a backup is blocked by the queued PMM enable too (shared tuple)
    r3 = client.post(f"/api/kube-targets/{tid}/backup",
                     json={"confirm": "cluster1", "params": {"type": "incr"}},
                     auth=("admin", "apw"))
    assert r3.status_code == 409
    monkeypatch.setenv("FAKE_KUBE_RESTART_S", "0")
    monkeypatch.setenv("FAKE_KUBE_RECREATE_S", "0")
    _drain_queue(cfg)                                 # first enable completes
    run = _last_ops_run(client, "pmm-enable")
    assert run["status"] in ("complete", "warning")


def test_pmm_web_disable_rejects_traversal_rollback_id(opsweb):
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = client.post(f"/api/kube-targets/{tid}/pmm/disable",
                    json={"confirm": "cluster1",
                          "params": {"rollback_of": "../../secrets"}},
                    auth=("admin", "apw"))
    assert r.status_code == 400 and "invalid id" in r.text


# ── IOPS framework through the web tier (cluster-aware runs from the UI) ──

def _iops_suite_yaml():
    import yaml as _yaml
    from conftest import make_spec_doc
    doc = make_spec_doc()
    del doc["sweep"]
    doc["suite"] = {"duration_s": 3, "threads": [1, 2], "warmup_s": 0,
                    "cooldown_s": 0, "workloads": ["oltp_read_write"],
                    "pgbench": False}
    doc["cluster"] = {"cr_name": "cluster1", "namespace": "percona"}
    return _yaml.safe_dump(doc)


def test_web_suite_run_with_attached_cluster_yields_verdict(opsweb, monkeypatch):
    """New Run -> attach cluster -> suite: the worker injects the target's
    kubeconfig, the run captures storage identity + device series, and the
    verdict is served to the run page via /api/runs/{id}/evidence."""
    from conftest import TEST_PASSWORD
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    monkeypatch.setenv("FAKE_PSQL_STATE",
                       str(cfg.data_dir.parent / "fake_psql"))
    (cfg.data_dir.parent / "fake_psql").mkdir(exist_ok=True)
    monkeypatch.setenv("PGB_PROBE_GRACE_S", "0.4")
    monkeypatch.setenv("FAKE_KUBE_DEV_IOPS", "9900")
    monkeypatch.setenv("FAKE_KUBE_DEV_UTIL", "99")
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")   # real seconds -> device samples
    r = client.post("/api/runs",
                    json={"spec_yaml": _iops_suite_yaml(),
                          "password": TEST_PASSWORD, "kube_target_id": tid},
                    auth=("op", "oppw"))
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "suite"
    _drain_queue(cfg)
    runs = client.get("/api/runs", auth=("viewer", "vpw")).json()
    assert runs and runs[0]["status"] == "complete", runs[:1]
    run_id = runs[0]["run_id"]
    ev = client.get(f"/api/runs/{run_id}/evidence", auth=("viewer", "vpw"))
    assert ev.status_code == 200, ev.text
    doc = ev.json()
    assert doc["verdict"]["finding"] == "capped"
    assert doc["storage_identity"]["pvc"]["storage_class"] == "do-block-storage"
    # the bundle download carries it all
    art = client.get(f"/runs/{run_id}/artifact", auth=("viewer", "vpw"))
    assert art.status_code == 200
    import gzip, io, tarfile
    names = tarfile.open(fileobj=io.BytesIO(art.content), mode="r:gz").getnames()
    assert any(n.endswith("evidence.json") for n in names)
    assert any(n.endswith("parsed/device_io.csv") for n in names)
    # bad kube_target_id is a clean 400
    r = client.post("/api/runs",
                    json={"spec_yaml": _iops_suite_yaml(),
                          "password": TEST_PASSWORD, "kube_target_id": 9999},
                    auth=("op", "oppw"))
    assert r.status_code == 400


def test_web_device_probe_gating(opsweb):
    import yaml as _yaml
    from conftest import make_spec_doc
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    doc = make_spec_doc()
    del doc["sweep"]                     # probe-only spec (no SQL load)
    doc["cluster"] = {"cr_name": "cluster1"}
    doc["device_probe"] = {"allow_device_probe": True, "duration_s": 2,
                           "file_total_size_gb": 1, "file_num": 8}
    probe_yaml = _yaml.safe_dump(doc)
    # operators cannot launch a probe
    r = client.post("/api/runs", json={"spec_yaml": probe_yaml,
                                       "kube_target_id": tid},
                    auth=("op", "oppw"))
    assert r.status_code == 403
    # admin without a cluster attachment: clean 400 (probe needs kubeconfig)
    r = client.post("/api/runs", json={"spec_yaml": probe_yaml},
                    auth=("admin", "apw"))
    assert r.status_code == 400 and "kube_target_id" in r.text
    # armed + attached: enqueued as a device_probe job and completes
    r = client.post("/api/runs", json={"spec_yaml": probe_yaml,
                                       "kube_target_id": tid},
                    auth=("admin", "apw"))
    assert r.status_code == 200 and r.json()["kind"] == "device_probe"
    _drain_queue(cfg)
    runs = client.get("/api/runs", auth=("viewer", "vpw")).json()
    assert runs and runs[0]["status"] == "complete", runs[:1]
    ev = client.get(f"/api/runs/{runs[0]['run_id']}/evidence",
                    auth=("viewer", "vpw")).json()
    assert ev["verdict"]["finding"] in ("capped", "exceeds", "inconclusive")
    assert ev["fileio"]["iops"] > 0


def test_pmm_inventory_401_reports_token_rejection_not_unreachable(pmmops):
    """Field bug: an HTTP 401 from the PMM API was reported as 'server
    unreachable'. A status code IS an answer — say the token was rejected,
    keep it a warning, and don't fail the run."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from pgbench_harness.ops.pmm import run_pmm_enable

    class Deny(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"message":"Unauthorized"}')

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Deny)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc = run_pmm_enable(
            _pmm_ops_spec("pmm-enable",
                          server_host=f"http://127.0.0.1:{srv.server_port}"),
            pmmops)
    finally:
        srv.shutdown()
    assert rc == 0                                # enablement itself succeeded
    events = (_only_pmm_run_dir(pmmops, "pmm-enable") / "events.jsonl").read_text()
    assert "rejected the token (HTTP 401)" in events
    assert "Service accounts" in events           # actionable pointer
    assert "unreachable" not in events


def test_pmm_spl_check_tolerates_real_cluster_renderings():
    """Runtime SHOW shared_preload_libraries can come back quoted, spaced,
    or operator-doubled — none of those mean a library is missing."""
    from pgbench_harness.ops.pmm import _norm_lib
    for rt in ("pgaudit,pg_stat_monitor",
               "pgaudit, pg_stat_monitor",
               "'pgaudit, pg_stat_monitor'",
               "pgaudit,pgaudit, pg_stat_monitor"):
        tokens = {_norm_lib(x) for x in rt.split(",")}
        assert "pgaudit" in tokens and "pg_stat_monitor" in tokens, rt
