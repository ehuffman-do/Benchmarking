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


def test_orphaned_prepare_converges_from_job_log(tmp_path, monkeypatch):
    """The reported bug: a multi-hour dataset PREPARE orphaned by a worker
    restart stayed 'running' forever (prepare was omitted from the re-attach
    kinds), so the UI never updated and the target could not be deleted. It
    must now converge — a successful load reads 'done' from its job log."""
    monkeypatch.setenv("PGBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PGBENCH_DB", str(tmp_path / "data" / "pgbench.db"))
    from pgbench_webapp import queries, worker
    from pgbench_webapp.config import load_config
    from pgbench_webapp.db import connect, ensure_dirs, migrate
    cfg = load_config()
    ensure_dirs(cfg)
    migrate(cfg.db_path)
    conn = connect(cfg.db_path)
    # a finished prepare's job log (no run dir) with the success marker
    (cfg.data_dir / "jobs").mkdir(parents=True, exist_ok=True)
    job_id = queries.enqueue_job(conn, "prepare", "spec: {}", None, "tester")
    (cfg.data_dir / "jobs" / f"job_{job_id}.out").write_text(
        "03:30:38 INFO database sbtest does not exist; creating it\n"
        "04:20:11 INFO dataset ready: 10 tables, scale 300\n"
        "04:20:11 INFO load metrics: 30,000,000 rows in 49m ...\n")
    queries.update_job(conn, job_id, state="running", pid=99999999)  # dead pid
    worker.reconcile_startup(cfg, conn)
    job = queries.get_job(conn, job_id)
    assert job["state"] == "done", job["error"]    # not stuck 'running'/'failed'
    # a prepare that ERRORED converges to failed with a clear note
    fail_id = queries.enqueue_job(conn, "prepare", "spec: {}", None, "tester")
    (cfg.data_dir / "jobs" / f"job_{fail_id}.out").write_text(
        "03:30:38 INFO preparing dataset\n"
        "FATAL: sysbench prepare exited with code 1\n")
    queries.update_job(conn, fail_id, state="running", pid=99999998)
    worker.reconcile_startup(cfg, conn)
    assert queries.get_job(conn, fail_id)["state"] == "failed"
    conn.close()


def test_streaming_heartbeat_skips_blank_lines(tmp_path):
    """The prepare log showed bare 'INFO' lines with no message: the heartbeat
    logged the raw current line, and sysbench prints blank separators. It must
    heartbeat the last NON-BLANK line with a progress count instead."""
    import logging
    from pgbench_harness import sysbench
    # a command that prints a real line then blanks, tripping the heartbeat on
    # a blank line (heartbeat_every=2 -> fires on the 2nd line, which is blank)
    cmd = sysbench.SysbenchCommand(
        argv=("bash", "-c", "printf 'Creating table sbtest1\\n\\n\\n\\n'"),
        cwd=str(tmp_path))
    records = []

    class Cap(logging.Handler):
        def emit(self, r): records.append(r.getMessage())

    logger = logging.getLogger("hb-test")
    logger.setLevel(logging.INFO)
    logger.addHandler(Cap())
    rc = sysbench.run_streaming(cmd, {}, tmp_path / "out.log", logger,
                                heartbeat_every=2, time_heartbeat_s=0)
    assert rc == 0
    hbs = [m for m in records if m.startswith("progress:")]
    assert hbs, "no progress heartbeat emitted"
    # every heartbeat carries the last real line, never a bare/empty message
    assert all("Creating table sbtest1" in m for m in hbs), hbs


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
    first, was_reset = tail.read_new(p)
    assert len(first) == 10                       # capped backfill
    assert not was_reset
    assert first[0] == "20,120"                   # ...of the most recent rows
    assert tail.header == "t,tps"
    assert tail.row_count == 20                   # rows skipped by the cap
    tail.row_count += len(first)
    with open(p, "a") as fh:                      # file grows: incremental
        fh.write("30,130\n31,13")                 # includes a partial line
    assert tail.read_new(p) == (["30,130"], False)
    with open(p, "a") as fh:
        fh.write("1\n")                           # partial completes
    assert tail.read_new(p) == (["31,131"], False)
    assert tail.read_new(p) == ([], False)        # nothing new -> no work
    # CRLF rows (csv.writer's default terminator) must not carry \r through
    with open(p, "a") as fh:
        fh.write("32,132\r\n")
    assert tail.read_new(p) == (["32,132"], False)


def test_pid_identity_prevents_recycled_pid_adoption():
    """os.kill(pid,0) alone mistakes a RECYCLED pid (reboot, pid_max wrap)
    for the surviving benchmark — the /proc start-time identity must
    disambiguate, or the phantom job starves the queue and Cancel signals
    a stranger's process group."""
    import subprocess as sp
    from pgbench_webapp.worker import _pid_is_our_child, _proc_start_ticks
    child = sp.Popen(["sleep", "30"])
    try:
        ticks = _proc_start_ticks(child.pid)
        assert ticks.isdigit()
        assert _pid_is_our_child(child.pid, ticks) is True
        assert _pid_is_our_child(child.pid, "1") is False    # recycled pid
        assert _pid_is_our_child(child.pid, "") is True      # legacy row
    finally:
        child.kill()
        child.wait()
    assert _pid_is_our_child(child.pid, ticks) is False      # gone


def test_sse_csv_tail_detects_atomic_rewrite(tmp_path):
    """The harness atomically REPLACES the live CSV at finalize/resume (new
    inode, possibly same-or-larger size). The tail must reset and SAY so, or
    the client double-appends thousands of rows / parses torn garbage."""
    from pgbench_webapp.app import _CsvTail
    import os as _os
    p = tmp_path / "series.csv"
    p.write_text("t,tps\n0,100\n1,101\n")
    tail = _CsvTail(cap=100)
    rows, was_reset = tail.read_new(p)
    assert rows == ["0,100", "1,101"] and not was_reset
    # atomic replace: bigger file, different content, new inode
    tmp = tmp_path / ".series.tmp"
    tmp.write_text("t,tps\n0,900\n1,901\n2,902\n3,903\n")
    _os.replace(tmp, p)
    rows, was_reset = tail.read_new(p)
    assert was_reset is True
    assert rows == ["0,900", "1,901", "2,902", "3,903"]   # full re-read


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


def test_cluster_target_mismatch_cross_check():
    """Attaching kube cluster A while SQL-targeting cluster B must be called
    out — the device series would measure an idle cluster."""
    from pgbench_harness.runner import cluster_target_mismatch
    from pgbench_harness.spec import parse_spec
    doc = make_spec_doc()
    doc["target"]["host"] = "adv-pg-bm-ehuff-1-bawya.db1.ondigitalocean.com"
    doc["cluster"] = {"cr_name": "adv-pg-bm-ehuff-1"}
    assert cluster_target_mismatch(parse_spec(doc)) == ""      # same cluster
    doc["cluster"] = {"cr_name": "adv-pgsql-30gsmal-nyc3-50415"}
    msg = cluster_target_mismatch(parse_spec(doc))
    assert "IDLE cluster" in msg and "SAME cluster" in msg     # cross-wired
    assert cluster_target_mismatch(parse_spec(make_spec_doc())) == ""


def test_rate_ladder_advances_past_unachievable_steps(fake_env, tmp_path, monkeypatch):
    """Field failure: --rate steps far above worker capacity made sysbench
    abort with 'event queue is full' on every relaunch, burning all 50
    relaunches inside one step. That answer is DATA (the knee is below the
    offered rate): the supervisor now records it and advances the ladder."""
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")
    monkeypatch.setenv("FAKE_SYSBENCH_QUEUE_FULL_RATE", "500")  # >=500 tps dies
    results = tmp_path / "results"
    doc = make_spec_doc()
    del doc["sweep"]
    doc["soak"] = {"threads": 4, "rate_steps": [1000, 2000, 100],
                   "step_duration_s": 4}
    spec_path = tmp_path / "ladder.yaml"
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    t0 = time.monotonic()
    rc = run_cli("soak", "--spec", str(spec_path), "--results-dir", str(results))
    elapsed = time.monotonic() - t0
    assert rc in (0, 1)
    assert elapsed < 60, "ladder did not advance past doomed steps"
    run_dir = find_run_dir(results)
    events = (run_dir / "events.jsonl").read_text()
    assert "rate step 1/3 unachievable: offered 1000 tps" in events
    assert "rate step 2/3 unachievable: offered 2000 tps" in events
    assert "rate step 3/3: 100 tps offered" in events     # achievable step ran
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["soak"]["relaunches"] == 0            # skips, not relaunches


def test_verdict_shallow_queue_full_util_names_the_real_suspect():
    """100% util with a shallow queue means time-busy, not saturation — the
    verdict must point at concurrency/the device-probe, not volume sizing."""
    from pgbench_harness.deviceio import compute_verdict
    from pgbench_harness.spec import Limits
    rows = [{"t_epoch_ms": 1000 * i, "reads_s": 3500.0, "writes_s": 2300.0,
             "iops": 5800.0, "read_mb_s": 50.0, "write_mb_s": 12.0,
             "await_ms": 6.6, "util_pct": 100.0, "queue_depth": 28.0}
            for i in range(30)]
    v = compute_verdict(rows, Limits())
    assert v["finding"] == "inconclusive"
    assert "NOT saturation" in v["detail"]
    assert "device-probe" in v["detail"]
    deep = [dict(r, queue_depth=200.0) for r in rows]
    v = compute_verdict(deep, Limits())
    assert "smaller-provisioned" in v["detail"]           # deep queue: sizing suspect
