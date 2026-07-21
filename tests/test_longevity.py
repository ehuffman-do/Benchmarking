"""Longevity hardening: the failure modes that kill hours- or week-long runs.

Covered scenarios: a hung load generator mid-sweep (watchdog), sampler thread
death (pg sampler exception guard, device stream auto-respawn), worker
restarts mid-run (orphan re-attach + convergence), a filling results volume
(clean stop), and the SSE cockpit against huge series files (incremental
tail + capped backfill).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from conftest import TEST_PASSWORD, make_spec_doc

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"


def run_cli(*argv: str) -> int:
    from pgbench_harness.cli import main
    return main(list(argv))


def find_run_dir(results: Path) -> Path:
    dirs = [d for d in results.iterdir() if (d / "manifest.json").exists()]
    assert dirs, f"no run dir under {results}"
    return sorted(dirs)[-1]


# ── a hung load generator must never freeze a run forever ──

def test_sweep_hung_level_is_killed_and_run_continues(fake_env, tmp_path, monkeypatch):
    """The freeze bug class: sysbench connects then goes silent (network
    stall, pooler wedge). The soak path always had a watchdog; the sweep/suite
    path did not — a single hung cell froze the whole run. Now the watchdog
    kills it, the level is marked failed with a clear reason, and the sweep
    continues to the remaining levels."""
    monkeypatch.setenv("FAKE_SYSBENCH_HANG_THREADS", "4")
    monkeypatch.setenv("PGB_LEVEL_WATCHDOG_GRACE_S", "2")
    results = tmp_path / "results"
    doc = make_spec_doc()
    doc["sweep"] = {"threads": [4, 1], "duration_s": 2, "warmup_s": 1,
                    "cooldown_s": 0, "repetitions": 1}
    spec_path = tmp_path / "hang.yaml"
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    t0 = time.monotonic()
    rc = run_cli("run", "--spec", str(spec_path), "--results-dir", str(results))
    elapsed = time.monotonic() - t0
    assert rc == 1                                      # partial, not frozen
    assert elapsed < 60, f"watchdog did not fire (took {elapsed:.0f}s)"
    manifest = json.loads((find_run_dir(results) / "manifest.json").read_text())
    by_threads = {lvl["threads"]: lvl for lvl in manifest["levels"]}
    assert by_threads[4]["status"] == "failed"
    assert "watchdog" in (by_threads[4]["error_excerpt"] or "")
    assert by_threads[1]["status"] == "ok"              # the run went on
    assert manifest["status"] == "partial"


# ── sampler threads must survive anything ──

def test_pg_sampler_survives_repeated_exceptions(tmp_path, monkeypatch):
    """One transient error must not kill the engine-metrics thread for the
    rest of a week-long run (the old loop died on the first unexpected
    exception and the pg series silently stopped)."""
    from pgbench_harness import capture
    from pgbench_harness.spec import parse_spec
    spec = parse_spec(make_spec_doc())
    calls = {"n": 0}

    def explode(*a, **k):
        calls["n"] += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(capture, "live_pg_query", explode)
    sampler = capture.LivePgSampler(spec, "pw", tmp_path, interval_s=1)
    sampler.interval = 0.05
    sampler.start()
    time.sleep(0.4)
    try:
        assert sampler._thread is not None and sampler._thread.is_alive()
        assert calls["n"] >= 3                          # kept sampling through errors
    finally:
        sampler.stop()


def test_device_sampler_respawns_after_stream_death(tmp_path, monkeypatch):
    """kubectl exec streams DIE over long windows (token refresh, failover,
    network blips). The sampler must respawn — with the gap visible in the
    series, never a dead sampler for the rest of the run."""
    for exe in ("kubectl",):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    kstate = tmp_path / "fakekube"
    kstate.mkdir()
    monkeypatch.setenv("FAKE_KUBE_STATE", str(kstate))
    monkeypatch.setenv("FAKE_KUBE_STREAM_DIE_S", "1")   # stream dies every ~1s
    from pgbench_harness.deviceio import DeviceIoSampler, derive_device_series
    from pgbench_harness.spec import parse_spec
    doc = make_spec_doc()
    doc["cluster"] = {"cr_name": "cluster1", "namespace": "percona"}
    spec = parse_spec(doc)
    (tmp_path / "raw").mkdir()
    sampler = DeviceIoSampler(spec, tmp_path)
    sampler.RESPAWN_BACKOFF_S = 0.3
    assert sampler.start()
    time.sleep(4.0)
    sampler.stop()
    warn = (tmp_path / "env" / "device_io_warning.txt").read_text()
    assert "respawn #1" in warn                         # death was noticed
    assert "not interpolated" in warn
    rows = derive_device_series(tmp_path)
    assert rows, "no device rows across respawned streams"


# ── worker restart mid-run: the child survives and is re-adopted ──

def test_reattach_converges_job_after_child_exit(tmp_path, monkeypatch):
    """Deploy (worker restart) mid-benchmark: with KillMode=process the
    harness child keeps running; at startup the worker re-attaches by pid and
    converges the job + index when the child eventually finishes."""
    monkeypatch.setenv("PGBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PGBENCH_DB", str(tmp_path / "data" / "pgbench.db"))
    from pgbench_webapp import queries, worker
    from pgbench_webapp.config import load_config
    from pgbench_webapp.db import connect, ensure_dirs, migrate

    cfg = load_config()
    ensure_dirs(cfg)
    migrate(cfg.db_path)
    conn = connect(cfg.db_path)
    # a run dir whose manifest says the benchmark COMPLETED
    from pgbench_harness.manifest import Manifest
    run_id = "orphan-run-20260721T000000Z"
    run_dir = cfg.results_dir / run_id
    (run_dir / "raw").mkdir(parents=True)
    m = Manifest(run_id=run_id, label="orphan", edition="advanced",
                 tshirt_size="4c16g")
    m.status = "complete"
    m.save(run_dir)
    # a job that is 'running' and owned by a pid that dies shortly (the
    # adopted child from before the restart)
    child = subprocess.Popen(["sleep", "1"])
    job_id = queries.enqueue_job(conn, "run", "spec: {}", None, "tester")
    queries.update_job(conn, job_id, state="running", pid=child.pid,
                       run_id=run_id)
    # run the monitor in a thread and REAP the child: in production the
    # adopted child is reparented to init and reaped there; here it would
    # stay a zombie — still visible to kill(pid, 0) — unless we wait()
    import threading
    t = threading.Thread(target=worker._reattach_orphan,
                         args=(cfg, job_id, child.pid),
                         kwargs={"poll_s": 0.2})
    t.start()
    child.wait()
    t.join(timeout=30)
    assert not t.is_alive(), "re-attach monitor never converged"
    job = queries.get_job(conn, job_id)
    assert job["state"] == "done"
    assert job["pid"] is None
    assert "converged" in (job["error"] or "") or job["error"] == ""
    conn.close()


def test_reconcile_startup_leaves_alive_orphans_running(tmp_path, monkeypatch):
    """A pid that is STILL ALIVE at worker startup must not be marked failed
    (that was the pre-KillMode behavior for crashed workers) — it gets a
    re-attach monitor instead."""
    monkeypatch.setenv("PGBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PGBENCH_DB", str(tmp_path / "data" / "pgbench.db"))
    from pgbench_webapp import queries, worker
    from pgbench_webapp.config import load_config
    from pgbench_webapp.db import connect, ensure_dirs, migrate
    cfg = load_config()
    ensure_dirs(cfg)
    migrate(cfg.db_path)
    conn = connect(cfg.db_path)
    child = subprocess.Popen(["sleep", "5"])
    try:
        job_id = queries.enqueue_job(conn, "soak", "spec: {}", None, "tester")
        queries.update_job(conn, job_id, state="running", pid=child.pid)
        dead_id = queries.enqueue_job(conn, "run", "spec: {}", None, "tester")
        queries.update_job(conn, dead_id, state="running", pid=99999999)
        worker.reconcile_startup(cfg, conn)
        assert queries.get_job(conn, job_id)["state"] == "running"   # adopted
        assert queries.get_job(conn, dead_id)["state"] == "failed"   # crashed
    finally:
        child.terminate()
        conn.close()


# ── disk-space guard ──

def test_disk_guard_aborts_cleanly_when_volume_fills(tmp_path, monkeypatch):
    import logging
    import shutil as _shutil

    from pgbench_harness.errors import RunError
    from pgbench_harness.runner import DISK_ABORT_BYTES, _disk_guard
    logger = logging.getLogger("t")

    class Usage:
        def __init__(self, free):
            self.free = free

    monkeypatch.setattr(_shutil, "disk_usage", lambda p: Usage(DISK_ABORT_BYTES - 1))
    import pgbench_harness.runner as runner_mod
    monkeypatch.setattr(runner_mod.shutil, "disk_usage",
                        lambda p: Usage(DISK_ABORT_BYTES - 1))
    with pytest.raises(RunError, match="free"):
        _disk_guard(tmp_path, logger, {})
    # warn band: no raise, flag set once
    monkeypatch.setattr(runner_mod.shutil, "disk_usage",
                        lambda p: Usage(DISK_ABORT_BYTES * 3))
    warned: dict = {}
    _disk_guard(tmp_path, logger, warned)
    assert warned.get("disk") is True


# ── SSE cockpit: incremental tail + capped backfill for huge series ──

def test_sse_csv_tail_incremental_and_capped(tmp_path):
    from pgbench_webapp.app import _CsvTail
    p = tmp_path / "series.csv"
    with open(p, "w") as fh:
        fh.write("t,tps\n")
        for i in range(30):
            fh.write(f"{i},{100 + i}\n")
    tail = _CsvTail(cap=10)
    first = tail.read_new(p)
    assert len(first) == 10                       # capped backfill
    assert first[0] == "20,120"                   # ...of the most recent rows
    assert tail.header == "t,tps"
    assert tail.row_count == 20                   # rows skipped by the cap
    tail.row_count += len(first)
    with open(p, "a") as fh:                      # file grows: incremental
        fh.write("30,130\n31,13")                 # includes a partial line
    assert tail.read_new(p) == ["30,130"]
    with open(p, "a") as fh:
        fh.write("1\n")                           # partial completes
    assert tail.read_new(p) == ["31,131"]
    assert tail.read_new(p) == []                 # nothing new -> no work


def test_sse_stream_serves_running_run_incrementally(tmp_path):
    """End-to-end over the generator: a mid-flight run streams hello +
    capped samples with reset=True, then only new rows on later ticks."""
    from pgbench_webapp.app import SSE_BACKFILL_ROWS, _sse

    class Cfg:  # _sse only touches run_dir
        pass

    run_dir = tmp_path / "run1"
    (run_dir / "parsed").mkdir(parents=True)
    from pgbench_harness.manifest import Manifest
    m = Manifest(run_id="run1", label="x", edition="advanced", tshirt_size="s")
    m.status = "running"
    m.save(run_dir)
    with open(run_dir / "parsed" / "samples.csv", "w") as fh:
        fh.write("run_id,rep,threads,t_offset,tps,qps,r,w,o,lat_p99,err_s,reconn_s,seg\n")
        for i in range(SSE_BACKFILL_ROWS + 500):
            fh.write(f"run1,1,4,{i},100,2000,1,1,1,10,0,0,\n")
    gen = _sse(Cfg(), run_dir, max_ticks=1)
    events = []
    try:
        for ev in gen:
            events.append(ev)
    except Exception:
        pass
    blob = "".join(events)
    assert "event: hello" in blob
    sample_events = [e for e in events if e.startswith("event: samples")]
    assert sample_events, "no samples event emitted"
    payload = json.loads(sample_events[0].split("data: ", 1)[1].strip())
    assert payload["reset"] is True
    assert len(payload["rows"]) == SSE_BACKFILL_ROWS   # capped, not 22k+
    assert payload["offset"] == 500                    # where the window starts
