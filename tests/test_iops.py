"""IOPS ceiling verification framework: spec surface, pgbench driver, device
IOPS derivation + verdict, suite mode e2e, rate-stepped soak e2e, and the
guardrailed device probe — all against the fake sysbench/pgbench/psql/kubectl.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from conftest import TEST_PASSWORD, make_spec_doc

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"


@pytest.fixture()
def iops_env(tmp_path, monkeypatch):
    """fake sysbench/psql/pgbench/kubectl on PATH + a fake cluster."""
    for exe in ("sysbench", "psql", "pgbench", "kubectl"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGB_TARGET_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("PGB_PROBE_GRACE_S", "0.4")
    state = tmp_path / "fake_state"
    state.mkdir()
    monkeypatch.setenv("FAKE_PSQL_STATE", str(state))
    kstate = tmp_path / "fakekube"
    kstate.mkdir()
    monkeypatch.setenv("FAKE_KUBE_STATE", str(kstate))
    monkeypatch.setenv("FAKE_KUBE_RESTART_S", "0")
    monkeypatch.setenv("FAKE_KUBE_LOG_FOLLOW_S", "120")
    return tmp_path


def run_cli(*argv: str) -> int:
    from pgbench_harness.cli import main
    return main(list(argv))


def find_run_dir(results: Path) -> Path:
    dirs = [d for d in results.iterdir() if (d / "manifest.json").exists()]
    assert dirs, f"no run dir under {results}"
    return sorted(dirs)[-1]


# ── spec surface ──

def test_io_stress_spec_derives_table_size():
    from pgbench_harness.spec import parse_spec
    doc = make_spec_doc()
    doc["workload"] = {"type": "io_stress", "tables": 16, "dataset_gb": 64,
                      "mix": "mixed"}
    spec = parse_spec(doc)
    # 64 GiB / (16 tables * 250 B/row)
    assert spec.workload.table_size == int(64 * (1 << 30) / (16 * 250))
    assert spec.workload.rand_type == "uniform"           # io_stress default
    from pgbench_harness.sysbench import build_run_command
    argv = build_run_command(spec, 4).argv
    assert "oltp_read_write" in argv                      # mix: mixed
    assert "--rand-type=uniform" in argv


def test_io_stress_spec_validation():
    from pgbench_harness.errors import SpecError
    from pgbench_harness.spec import parse_spec
    doc = make_spec_doc()
    doc["workload"] = {"type": "io_stress", "tables": 16, "mix": "read"}
    with pytest.raises(SpecError, match="dataset_gb"):
        parse_spec(doc)
    doc["workload"] = {"type": "io_stress", "tables": 16, "dataset_gb": 1,
                      "mix": "nope"}
    with pytest.raises(SpecError, match="mix"):
        parse_spec(doc)
    doc = make_spec_doc()
    doc["workload"]["dataset_gb"] = 4                     # io_stress-only knob
    with pytest.raises(SpecError, match="io_stress-only"):
        parse_spec(doc)


def test_rate_steps_spec_validation():
    from pgbench_harness.errors import SpecError
    from pgbench_harness.spec import parse_spec
    doc = make_spec_doc()
    del doc["sweep"]
    doc["soak"] = {"threads": 8, "rate_steps": [100, 200], "step_duration_s": 60}
    spec = parse_spec(doc)
    assert spec.soak.duration_s == 120                    # derived
    doc["soak"] = {"threads": 8, "rate_steps": [100, 200]}
    with pytest.raises(SpecError, match="go together"):
        parse_spec(doc)
    doc["soak"] = {"threads": 8, "rate_steps": [100], "step_duration_s": 60,
                   "duration_s": 999}
    with pytest.raises(SpecError, match="conflicts"):
        parse_spec(doc)


def test_cluster_and_probe_spec_validation():
    from pgbench_harness.errors import SpecError
    from pgbench_harness.spec import parse_spec
    doc = make_spec_doc()
    doc["cluster"] = {"cr_name": "cluster1", "kubeconfig": "/root/kc"}
    with pytest.raises(SpecError, match="KUBECONFIG environment"):
        parse_spec(doc)
    doc = make_spec_doc()
    doc["device_probe"] = {"allow_device_probe": True}
    with pytest.raises(SpecError, match="cluster"):       # probe needs cluster
        parse_spec(doc)
    doc["cluster"] = {"cr_name": "cluster1"}
    spec = parse_spec(doc)
    assert spec.device_probe.file_num == 128              # defaults recorded
    assert spec.limits.standard_iops == 10000


def test_suite_spec_and_existing_specs_unchanged():
    from pgbench_harness.spec import parse_spec
    doc = make_spec_doc()
    del doc["sweep"]
    doc["suite"] = {"duration_s": 60}
    spec = parse_spec(doc)
    assert spec.suite.threads == (1, 2, 4, 8, 16, 32)
    assert spec.suite.workloads == ("oltp_point_select", "oltp_read_only",
                                    "oltp_read_write", "oltp_write_only")
    assert spec.suite.pgbench is True
    # a classic spec parses to exactly the old shape (no new behavior)
    old = parse_spec(make_spec_doc())
    assert old.cluster is None and old.suite is None and old.device_probe is None
    assert old.workload.rand_type == ""                   # no argv change


# ── pgbench driver ──

def test_pgbench_command_builders_and_parsers(iops_env):
    from pgbench_harness.pgbench_cmd import (build_pgbench_init,
                                             build_pgbench_run,
                                             parse_pgbench_progress,
                                             parse_pgbench_summary)
    from pgbench_harness.spec import parse_spec
    spec = parse_spec(make_spec_doc())
    init = build_pgbench_init(spec, 1000)
    assert init.argv[:4] == ("pgbench", "-i", "-s", "1000")
    run = build_pgbench_run(spec, 16, 60, select_only=True)
    assert "-S" in run.argv and "-c" in run.argv and "-P" in run.argv
    assert TEST_PASSWORD not in " ".join(run.argv)        # env-only password
    s = parse_pgbench_progress(
        "progress: 5.0 s, 8975.6 tps, lat 3.567 ms stddev 1.234, 0 failed")
    assert s is not None and s.tps == 8975.6 and s.lat_ms == 3.567
    import subprocess
    out = subprocess.run([str(FAKEBIN / "pgbench"), "-c", "4", "-T", "3",
                          "-P", "1", "db"], capture_output=True, text=True,
                         env=dict(os.environ)).stdout
    summary = parse_pgbench_summary(out)
    assert summary["tps"] and summary["transactions"] and summary["failed"] == 0


# ── device series derivation + verdict ──

def _dev_rows(iops: float, util: float, n: int = 30) -> list[dict]:
    return [{"t_epoch_ms": 1000 * i, "reads_s": iops * 0.6, "writes_s": iops * 0.4,
             "iops": iops, "read_mb_s": 90.0, "write_mb_s": 60.0,
             "await_ms": 2.0, "util_pct": util, "queue_depth": 4.0}
            for i in range(n)]


def test_verdict_three_ways():
    from pgbench_harness.deviceio import compute_verdict
    from pgbench_harness.spec import Limits
    lim = Limits()
    v = compute_verdict(_dev_rows(9900, 99), lim)
    assert v["finding"] == "capped" and "binding constraint" in v["detail"]
    v = compute_verdict(_dev_rows(22000, 99), lim)
    assert v["finding"] == "exceeds" and "22,000" in v["detail"]
    v = compute_verdict(_dev_rows(3000, 40), lim)
    assert v["finding"] == "inconclusive" and "bottleneck is" in v["detail"]
    v = compute_verdict([], lim)
    assert v["finding"] == "inconclusive" and "no device series" in v["detail"]


def test_device_series_derivation_from_diskstats_stream(tmp_path):
    from pgbench_harness.deviceio import derive_device_series
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    (raw / "diskstats_device.json").write_text(
        json.dumps({"majmin": "259:4", "device": "nvme1n1"}))
    lines = []
    for i in range(6):
        t_ms = 1_000_000 + i * 1000
        reads, writes = 6000 * i, 4000 * i
        lines.append(str(t_ms))
        lines.append(f" 259       4 nvme1n1 {reads} 0 {reads * 32} {reads // 3} "
                     f"{writes} 0 {writes * 32} {writes // 2} 2 {i * 980} "
                     f"{i * 3900} 0 0")
        lines.append("===")
    (raw / "diskstats.log").write_text("\n".join(lines))
    rows = derive_device_series(tmp_path)
    assert len(rows) == 5
    assert rows[0]["iops"] == 10000.0                     # 6000r + 4000w per 1s
    assert rows[0]["reads_s"] == 6000.0
    assert rows[0]["util_pct"] == 98.0
    assert abs(rows[0]["read_mb_s"] - 6000 * 32 * 512 / 1048576) < 0.1
    assert (tmp_path / "parsed" / "device_io.csv").exists()


# ── e2e: suite mode against an obviously-capped fake device ──

def test_suite_e2e_capped_verdict_and_evidence_bundle(iops_env, monkeypatch):
    monkeypatch.setenv("FAKE_KUBE_DEV_IOPS", "9900")      # ~ the 10K throttle
    monkeypatch.setenv("FAKE_KUBE_DEV_UTIL", "99")
    results = iops_env / "results"
    doc = make_spec_doc()
    del doc["sweep"]
    doc["suite"] = {"duration_s": 2, "threads": [1, 2], "warmup_s": 0,
                    "cooldown_s": 0,
                    "workloads": ["oltp_point_select", "oltp_write_only"],
                    "pgbench": True, "pgbench_scale": 1}
    doc["cluster"] = {"cr_name": "cluster1", "namespace": "percona"}
    doc["limits"] = {"standard_iops": 10000, "burst_iops": 15000,
                     "target_iops": 40000}
    spec_path = iops_env / "suite.yaml"
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    assert run_cli("suite", "--spec", str(spec_path), "--dry-run") == 0
    rc = run_cli("suite", "--spec", str(spec_path),
                 "--results-dir", str(results))
    assert rc == 0
    run_dir = find_run_dir(results)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["mode"] == "suite" and manifest["status"] == "complete"
    assert len(manifest["levels"]) == 8                   # (2 wl + 2 pgb) x 2
    assert all(lvl["status"] == "ok" for lvl in manifest["levels"])
    segs = {lvl["seg"] for lvl in manifest["levels"]}
    assert segs == {"oltp_point_select", "oltp_write_only",
                    "pgbench_tpcb", "pgbench_select"}
    # evidence bundle: verdict, storage identity, workloads-with-SQL, caveats
    ev = json.loads((run_dir / "evidence.json").read_text())
    assert ev["verdict"]["finding"] == "capped"
    assert ev["limits"]["standard_iops"] == 10000
    ident = ev["storage_identity"]
    assert ident["pvc"]["storage_class"] == "do-block-storage"
    assert ident["pv"]["provisioner"] == "dobs.csi.digitalocean.com"
    assert "no high-IOPS marker" in ident["finding"]
    assert any(w["workload"] == "pgbench_tpcb" for w in ev["workloads"])
    assert any("pgbench_history" in q for w in ev["workloads"]
               for q in w["per_txn"])
    assert ev["caveats"]
    # device series + report
    assert (run_dir / "parsed" / "device_io.csv").exists()
    html = (run_dir / "report.html").read_text()
    assert "CAPPED" in html and "binding constraint" in html
    assert "Storage identity" in html and "do-block-storage" in html
    assert "exactly what SQL" in html
    assert "data:image/png;base64," in html               # charts inline
    assert "Caveats" in html
    # full DB-settings capture renders in the evidence report (provider diffing)
    assert "Full pg_settings dump" in html and "shared_buffers" in html
    # per-seg rows in the summary + seg column in samples.csv
    summary = json.loads((run_dir / "parsed" / "summary.json").read_text())
    assert any(lv.get("driver") == "pgbench" for lv in summary["levels"])
    head = (run_dir / "parsed" / "samples.csv").read_text().splitlines()[0]
    assert head.endswith(",seg")
    # password + no-leak invariants hold for the new artifacts too
    for p in run_dir.rglob("*"):
        if p.is_file():
            assert TEST_PASSWORD not in p.read_text(errors="replace"), p


def test_suite_high_iops_marker_is_surfaced(iops_env, monkeypatch):
    from pgbench_harness.deviceio import capture_storage_identity
    from pgbench_harness.spec import parse_spec
    monkeypatch.setenv("FAKE_KUBE_SC_IOPS", "80")
    doc = make_spec_doc()
    doc["cluster"] = {"cr_name": "cluster1"}
    spec = parse_spec(doc)
    run_dir = iops_env / "ident"
    (run_dir / "env").mkdir(parents=True)
    ident = capture_storage_identity(spec, run_dir)
    assert any("iopsPerGB=80" in m for m in ident["high_iops_markers"])
    assert "high-IOPS/QoS markers present" in ident["finding"]


# ── e2e: rate-stepped soak against an obviously-not-pressured device ──

def test_rate_stepped_soak_inconclusive_verdict(iops_env, monkeypatch):
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")
    monkeypatch.setenv("FAKE_KUBE_DEV_IOPS", "3000")      # nowhere near 10K
    monkeypatch.setenv("FAKE_KUBE_DEV_UTIL", "40")
    results = iops_env / "results"
    doc = make_spec_doc()
    del doc["sweep"]
    doc["soak"] = {"threads": 4, "rate_steps": [50, 0], "step_duration_s": 3}
    doc["cluster"] = {"cr_name": "cluster1", "namespace": "percona"}
    spec_path = iops_env / "soak.yaml"
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert run_cli("soak", "--spec", str(spec_path), "--dry-run") == 0
    rc = run_cli("soak", "--spec", str(spec_path), "--results-dir", str(results))
    assert rc in (0, 1)
    run_dir = find_run_dir(results)
    events = (run_dir / "events.jsonl").read_text()
    assert "rate step 1/2: 50 tps offered" in events
    assert "rate step 2/2: unthrottled tps offered" in events
    ev = json.loads((run_dir / "evidence.json").read_text())
    assert ev["verdict"]["finding"] == "inconclusive"
    assert "pressure" in ev["verdict"]["detail"]
    # offered load honored: step-1 samples are throttled to ~50 tps
    soak_html = (run_dir / "soak_report.html").read_text()
    assert "Full pg_settings dump" in soak_html           # settings in soak too
    ts = (run_dir / "parsed" / "soak_timeseries.csv").read_text().splitlines()
    first_tps = [float(r.split(",")[2]) for r in ts[1:4] if r.split(",")[2]]
    assert first_tps and all(t <= 60 for t in first_tps)


# ── e2e: device probe (guardrails + verdict from the fileio window) ──

def test_device_probe_refuses_without_arming(iops_env):
    doc = make_spec_doc()
    doc["cluster"] = {"cr_name": "cluster1"}
    doc["device_probe"] = {"allow_device_probe": False, "duration_s": 2,
                           "file_total_size_gb": 1}
    spec_path = iops_env / "probe.yaml"
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    rc = run_cli("device-probe", "--spec", str(spec_path),
                 "--results-dir", str(iops_env / "results"))
    assert rc == 2                                        # HarnessError exit


def test_device_probe_free_space_guardrail(iops_env, monkeypatch):
    monkeypatch.setenv("FAKE_KUBE_DF_AVAIL_KB", str(10 * 1048576))  # 10 GiB
    doc = make_spec_doc()
    doc["cluster"] = {"cr_name": "cluster1"}
    doc["device_probe"] = {"allow_device_probe": True, "duration_s": 2,
                           "file_total_size_gb": 8}       # needs 16 GiB
    spec_path = iops_env / "probe.yaml"
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    rc = run_cli("device-probe", "--spec", str(spec_path),
                 "--results-dir", str(iops_env / "results"))
    assert rc == 2
    st = json.loads((Path(os.environ["FAKE_KUBE_STATE"]) / "state.json")
                    .read_text())
    assert not st.get("extra_pods")                       # pod cleaned up


def test_device_probe_e2e_capped_and_cleaned_up(iops_env, monkeypatch):
    monkeypatch.setenv("FAKE_KUBE_DEV_IOPS", "9950")
    monkeypatch.setenv("FAKE_KUBE_DEV_UTIL", "99")
    results = iops_env / "results"
    doc = make_spec_doc()
    doc["cluster"] = {"cr_name": "cluster1"}
    doc["device_probe"] = {"allow_device_probe": True, "duration_s": 3,
                           "file_total_size_gb": 1, "file_num": 8,
                           "threads": 4}
    spec_path = iops_env / "probe.yaml"
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert run_cli("device-probe", "--spec", str(spec_path), "--dry-run") == 0
    rc = run_cli("device-probe", "--spec", str(spec_path),
                 "--results-dir", str(results))
    assert rc == 0
    run_dir = find_run_dir(results)
    fio = json.loads((run_dir / "parsed" / "fileio_summary.json").read_text())
    assert fio["iops"] == pytest.approx(9950, rel=0.01)
    assert fio["started_utc"] and fio["finished_utc"]     # PMM window
    ev = json.loads((run_dir / "evidence.json").read_text())
    assert ev["verdict"]["finding"] == "capped"
    html = (run_dir / "report.html").read_text()
    assert "Direct device probe" in html and "CAPPED" in html
    # cleanup on the happy path: pod gone, files flag cleared
    st = json.loads((Path(os.environ["FAKE_KUBE_STATE"]) / "state.json")
                    .read_text())
    assert not st.get("extra_pods")
    assert st.get("probe_files") is False
