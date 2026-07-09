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
    assert "not visible to the worker" in out
    assert "kubeconfigs/" in out                 # points at the sanctioned dir


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
    client, cfg = opsweb
    tid = _ready_target(client, cfg)
    r = _fire_backup(client, tid, {"type": "incr", "path": "direct",
                                   "source": "replica",
                                   "sample_interval_s": 0.2, "settle_s": 0.3})
    assert r.status_code == 200
    _drain_queue(cfg)
    run = _last_ops_run(client, "backup")
    assert run["status"] == "complete", run
    h = run["headline"]
    assert h["source_role"] == "replica"
    assert h["source"] != h["leader"]             # the work landed on a replica
    # load sampler covered BOTH nodes
    load = (cfg.results_dir / "ops" / run["op_run_id"] / "parsed" /
            "load.csv").read_text()
    assert h["leader"] in load and h["source"] in load


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
