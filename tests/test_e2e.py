"""End-to-end tests through the CLI against fake sysbench/psql binaries.

This is the in-repo walkthrough of acceptance criterion 1 (preflight →
prepare → run → report.html) with the sysbench/psql boundary mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pgbench_harness.cli import main

from conftest import TEST_PASSWORD

REQUIRED_SECTIONS = [
    "Headline results",
    "Throughput",
    "Latency",
    "Stability (QPS over time)",
    "Errors &amp; limits",
    "Configuration appendix",
]


def run_cli(*argv: str) -> int:
    return main(list(argv))


def find_run_dir(results: Path) -> Path:
    dirs = [d for d in results.iterdir() if (d / "manifest.json").exists()]
    assert dirs, f"no run dir under {results}"
    return sorted(dirs)[-1]


@pytest.fixture()
def results_dir(tmp_path: Path) -> Path:
    return tmp_path / "results"


def test_sweep_live_samples_written_during_run(fake_env, spec_file, results_dir) -> None:
    """The cockpit's samples.csv is fed per-second DURING the sweep, not only at
    finalize: after a level runs, the live file already holds parsed interval rows
    (write_parsed later rebuilds the same file canonically)."""
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    run_dir = find_run_dir(results_dir)
    samples = (run_dir / "parsed" / "samples.csv").read_text().splitlines()
    assert samples[0].split(",")[:4] == ["run_id", "rep", "threads", "t_offset"]
    assert len(samples) > 1                       # real per-second rows, not just '--'


def test_run_streaming_on_line_fires_per_line(fake_env, spec_file) -> None:
    """run_streaming delivers each line to the on_line tap as the child runs (the
    hook that drives live per-second charts), before the process exits."""
    import logging

    from pgbench_harness import sysbench
    from pgbench_harness.parser import parse_interval_line
    from pgbench_harness.spec import load_spec

    spec = load_spec(spec_file)
    seen: list[str] = []
    cmd = sysbench.build_run_command(spec, 1)
    rc = sysbench.run_streaming(cmd, sysbench.child_env(spec, "pw"),
                                spec_file.parent / "raw.log", logging.getLogger("t"),
                                on_line=seen.append)
    assert rc == 0
    assert sum(1 for ln in seen if parse_interval_line(ln) is not None) >= 1


def test_preflight_ok(fake_env: Path, spec_file: Path, capsys) -> None:
    assert run_cli("preflight", "--spec", str(spec_file)) == 0


def test_preflight_missing_dataset(fake_env, spec_file, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_PSQL_TABLES", "0")
    assert run_cli("preflight", "--spec", str(spec_file)) == 1


def test_preflight_connection_ceiling(fake_env, spec_file, monkeypatch, capsys) -> None:
    """Acceptance criterion 4: ceiling failure surfaces count + verbatim error."""
    monkeypatch.setenv("FAKE_PSQL_MAX_CONN", "2")
    rc = run_cli("preflight", "--spec", str(spec_file))
    err = capsys.readouterr().err
    assert rc == 2
    assert "no more connections allowed (max_client_conn)" in err
    assert "2 of 4 simultaneous connections" in err
    assert "connection #3" in err


def test_prepare_already_present_errors_clearly(fake_env, spec_file, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    rc = run_cli("prepare", "--spec", str(spec_file))   # dataset already present
    assert rc != 0                                        # no longer a silent no-op
    assert "already present" in capsys.readouterr().err.lower()


def test_prepare_recreate_requires_matching_confirm(fake_env, spec_file, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert run_cli("prepare", "--spec", str(spec_file), "--recreate", "tables", "--confirm", "wrong") != 0
    assert "confirmation" in capsys.readouterr().err.lower()


def test_prepare_recreate_tables_reloads(fake_env, spec_file, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    # correct confirmation -> drops benchmark tables and reloads (sysbench prepare runs)
    assert run_cli("prepare", "--spec", str(spec_file), "--recreate", "tables", "--confirm", "sbtest") == 0


def test_prepare_create_db_unreachable_after_create_fails(fake_env, spec_file, results_dir, monkeypatch) -> None:
    """CREATE DATABASE succeeds but the new DB never accepts a connection: prepare
    must fail loudly (it used to discard wait_for_db and load against a dead cluster)."""
    from pgbench_harness import capture, runner
    from pgbench_harness.errors import RunError
    monkeypatch.setattr(capture, "maintenance_db", lambda spec, pw: "defaultdb")
    monkeypatch.setattr(capture, "database_exists", lambda spec, pw, maint: False)
    monkeypatch.setattr(capture, "create_database", lambda spec, pw, maint: (True, ""))
    monkeypatch.setattr(capture, "wait_for_db", lambda spec, pw, **k: False)
    with pytest.raises(RunError, match="not reachable"):
        runner.cmd_prepare(spec_file, results_dir, create_db=True)


def test_prepare_no_maintenance_db_and_target_unreachable_is_explicit(
        fake_env, spec_file, results_dir, monkeypatch) -> None:
    """No maintenance DB reachable AND the target itself doesn't answer -> an
    explicit error naming the cause, not a generic downstream connectivity failure."""
    from pgbench_harness import capture, runner
    from pgbench_harness.errors import RunError
    monkeypatch.setattr(capture, "maintenance_db", lambda spec, pw: None)
    monkeypatch.setattr(capture, "wait_for_db", lambda spec, pw, **k: False)
    with pytest.raises(RunError, match="maintenance database"):
        runner.cmd_prepare(spec_file, results_dir, create_db=True)


def test_dry_run_prints_commands_and_budget(fake_env, spec_file, capsys) -> None:
    assert run_cli("run", "--spec", str(spec_file), "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.count("sysbench oltp_read_write") == 4  # 2 reps x 2 levels
    assert "--threads=1" in out and "--threads=4" in out
    assert "--time=5" in out
    # budget: 4 levels x 5s + 3 cooldowns x 0s = 20s
    assert "planned wall-clock budget: 20s" in out
    assert TEST_PASSWORD not in out


def test_full_run_produces_report(fake_env, spec_file, results_dir) -> None:
    """Acceptance criteria 1, 2, 6, 7: full run, all sections, per-rep variance."""
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    run_dir = find_run_dir(results_dir)

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert len(manifest["levels"]) == 4
    assert all(l["status"] == "ok" for l in manifest["levels"])

    for rel in ["spec.yaml", "env/pg_settings.csv", "env/server_version.txt",
                "env/sysbench_version.txt", "env/tpcc_git_sha.txt",
                "env/harness_git_sha.txt", "env/host_info.txt", "env/spec.yaml",
                "raw/rep1_t001.log", "raw/rep2_t004.log",
                "parsed/samples.csv", "parsed/summary.json", "report.html"]:
        assert (run_dir / rel).exists(), f"missing {rel}"

    html = (run_dir / "report.html").read_text()
    for section in REQUIRED_SECTIONS:
        assert section in html, f"report missing section {section!r}"
    assert "data:image/png;base64," in html       # self-contained charts
    assert "http://" not in html and "https://" not in html  # no CDN/network refs
    assert "rep Δ QPS %" in html                   # variance column (2 reps)
    assert "shared_buffers" in html                # key settings table
    assert "overflow: hidden" not in html          # scroll-bug regression guard
    assert "table-wrap" in html                    # tables scroll horizontally
    assert "<tbody>" in html                        # proper zebra striping structure
    assert "Storage I/O (engine-side)" in html     # IOPS-proxy section
    assert "read ops/s" in html and "write ops/s" in html
    assert (run_dir / "raw" / "rep1_t001_iostats.json").exists()  # raw snapshots

    summary = json.loads((run_dir / "parsed" / "summary.json").read_text())
    assert {(l["rep"], l["threads"]) for l in summary["levels"]} == \
        {(1, 1), (1, 4), (2, 1), (2, 4)}
    assert all(l["qps_avg"] > 0 for l in summary["levels"])
    assert all(l["steady_state_window"] == [1, 5] for l in summary["levels"])
    # engine-side I/O deltas landed in the summary contract
    for lvl in summary["levels"]:
        assert "io" in lvl and lvl["io"]["read_ops_s"] is not None


def test_no_password_anywhere_in_results(fake_env, spec_file, results_dir) -> None:
    """The password-leak test: TEST_PASSWORD must not land in any results file."""
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    run_dir = find_run_dir(results_dir)
    leaks = []
    for path in run_dir.rglob("*"):
        if path.is_file() and TEST_PASSWORD.encode() in path.read_bytes():
            leaks.append(str(path))
    assert not leaks, f"password leaked into: {leaks}"


def test_failed_level_continues_and_marks_partial(
    fake_env, spec_file, results_dir, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_SYSBENCH_FAIL_THREADS", "4")
    rc = run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir))
    assert rc == 1  # partial
    run_dir = find_run_dir(results_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "partial"
    by_threads = {(l["rep"], l["threads"]): l for l in manifest["levels"]}
    assert by_threads[(1, 1)]["status"] == "ok"
    assert by_threads[(1, 4)]["status"] == "failed"
    assert by_threads[(2, 1)]["status"] == "ok"  # later levels still ran
    assert "max_client_conn" in by_threads[(1, 4)]["error_excerpt"]
    html = (run_dir / "report.html").read_text()
    assert "FAILED" in html
    assert "max_client_conn" in html


def test_resume_completes_only_remaining_levels(
    fake_env, spec_file, results_dir, monkeypatch, tmp_path
) -> None:
    """Acceptance criterion 3: kill mid-sweep, --resume finishes the rest."""
    counter = tmp_path / "invocations.txt"
    monkeypatch.setenv("FAKE_SYSBENCH_COUNT_FILE", str(counter))
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    run_dir = find_run_dir(results_dir)

    # Simulate a crash after level 2: mark the last two levels unfinished.
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["levels"][2]["status"] = "running"   # crashed mid-level
    manifest["levels"][3]["status"] = "pending"
    manifest["status"] = "running"
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    counter.write_text("")

    rc = run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir),
                 "--resume", "--run-dir", str(run_dir))
    assert rc == 0
    invocations = [l for l in counter.read_text().splitlines() if l]
    assert len(invocations) == 2  # only the two unfinished levels re-ran
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert (run_dir / "report.html").exists()


def test_report_regeneration(fake_env, spec_file, results_dir) -> None:
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    run_dir = find_run_dir(results_dir)
    (run_dir / "report.html").unlink()
    assert run_cli("report", "--run-dir", str(run_dir)) == 0
    assert "Headline results" in (run_dir / "report.html").read_text()


def test_compare_two_runs(fake_env, spec_file, results_dir, monkeypatch, tmp_path) -> None:
    """Acceptance criterion 5: overlaid charts + settings diff across run dirs."""
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    monkeypatch.setenv("FAKE_PSQL_WORK_MEM", "65536")  # second run differs
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    runs = sorted(d.name for d in results_dir.iterdir() if (d / "manifest.json").exists())
    assert len(runs) == 2
    out = tmp_path / "compare.html"
    rc = run_cli("compare", "--runs", *runs, "--results-dir", str(results_dir),
                 "--out", str(out))
    assert rc == 0
    html = out.read_text()
    assert "work_mem" in html            # settings diff caught the difference
    assert "65536" in html and "4096" in html
    # QPS, TPS, p99, efficiency, relative-to-baseline
    assert html.count("data:image/png;base64,") >= 4
    assert "Settings diff" in html
    assert "Per-run summary" in html         # new KPI band
    assert "Efficiency (latency vs throughput)" in html
    assert "Storage I/O (engine-side)" in html  # I/O overlay section
    assert "peak QPS" in html
    assert "highest peak throughput" in html  # winner callout
    assert "overflow: hidden" not in html     # scroll-bug regression guard
    assert "table-wrap" in html               # tables are horizontally scrollable


def test_list_runs(fake_env, spec_file, results_dir, capsys) -> None:
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    capsys.readouterr()
    assert run_cli("list", "--results-dir", str(results_dir)) == 0
    out = capsys.readouterr().out
    assert "test-tiny" in out
    assert "complete" in out
    assert "4/4" in out


def test_validate_ok(spec_file, capsys) -> None:
    assert run_cli("validate", "--spec", str(spec_file)) == 0
    out = capsys.readouterr().out
    assert "OK:" in out and "mode     : sweep" in out


def test_validate_bad_spec(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("run: {label: x}\n", encoding="utf-8")  # missing required sections
    assert run_cli("validate", "--spec", str(bad)) == 2


def test_doctor(capsys) -> None:
    assert run_cli("doctor") == 0
    assert "pgbench-harness" in capsys.readouterr().out


def test_run_prepare_chains(fake_env, spec_file, results_dir, monkeypatch) -> None:
    """`run --prepare` loads a missing dataset first, then runs to completion."""
    monkeypatch.setenv("FAKE_PSQL_TABLES", "0")  # dataset absent until prepared
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir),
                   "--prepare") == 0
    run_dir = find_run_dir(results_dir)
    assert json.loads((run_dir / "manifest.json").read_text())["status"] == "complete"


def test_run_refuses_missing_dataset(fake_env, spec_file, results_dir, monkeypatch, capsys) -> None:
    monkeypatch.setenv("FAKE_PSQL_TABLES", "0")
    rc = run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir))
    assert rc == 2
    err = capsys.readouterr().err
    assert "prepare" in err  # tells the user what to do next


def test_dataset_size_mismatch_aborts(fake_env, spec_file, results_dir, monkeypatch, capsys) -> None:
    """Spec says table_size 1000 but the cluster holds 2000 rows -> hard abort."""
    monkeypatch.setenv("FAKE_PSQL_MAX_ID", "2000")
    assert run_cli("preflight", "--spec", str(spec_file)) == 2
    err = capsys.readouterr().err
    assert "mismatch" in err
    assert "2000" in err and "1000" in err
    assert "re-prepare" in err or "prepare" in err
    # run and prepare must refuse too (prepare never loads on top)
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 2
    assert run_cli("prepare", "--spec", str(spec_file),
                   "--results-dir", str(results_dir)) == 2


def test_wrong_schema_detected(fake_env, spec_file, monkeypatch, capsys) -> None:
    """Tables exist but off the search_path -> diagnosed as wrong_schema, not missing."""
    monkeypatch.setenv("FAKE_PSQL_WRONG_SCHEMA", "benchmark")
    assert run_cli("preflight", "--spec", str(spec_file)) == 2
    err = capsys.readouterr().err
    assert "wrong_schema" in err
    assert "search_path" in err
    assert "benchmark.sbtest1" in err  # tells the user exactly where they are


def test_prepare_succeeds_but_creates_nothing(
    fake_env, spec_file, results_dir, monkeypatch, capsys
) -> None:
    """sysbench prepare exits 0 but no tables appear -> error shows the log tail."""
    monkeypatch.setenv("FAKE_PSQL_TABLES", "0")
    monkeypatch.setenv("FAKE_SYSBENCH_NO_MARKER", "1")
    rc = run_cli("prepare", "--spec", str(spec_file), "--results-dir", str(results_dir))
    assert rc == 2
    err = capsys.readouterr().err
    assert "reported success but no benchmark tables exist" in err
    assert "Creating tables and loading data" in err  # prepare-log tail included


def test_incomplete_dataset_aborts(fake_env, spec_file, monkeypatch, capsys) -> None:
    """Some expected tables exist but not all -> abort with cleanup hint."""
    monkeypatch.setenv("FAKE_PSQL_TABLES", "3")  # spec expects 9 sbtest tables
    assert run_cli("preflight", "--spec", str(spec_file)) == 2
    err = capsys.readouterr().err
    assert "3 of 9" in err


def test_unrecognized_canary_schema_aborts(fake_env, spec_file, monkeypatch, capsys) -> None:
    """sbtest1 exists but with someone else's columns -> abort, never overwrite."""
    monkeypatch.setenv("FAKE_PSQL_CANARY_COLS", "1")
    assert run_cli("preflight", "--spec", str(spec_file)) == 2
    err = capsys.readouterr().err
    assert "not created by this tool" in err or "unrecognized schema" in err


def test_foreign_tables_warning(fake_env, spec_file, capsys, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_PSQL_FOREIGN", "5")
    assert run_cli("preflight", "--spec", str(spec_file)) == 0  # warns, not fatal
    out = capsys.readouterr().out
    assert "non-benchmark table(s)" in out
    assert "dedicated database" in out


def test_prepare_records_load_metrics(
    fake_env: Path, spec_file, results_dir, monkeypatch
) -> None:
    """prepare loads when missing, records wall time / DB size / throughput."""
    monkeypatch.setenv("FAKE_PSQL_TABLES", "0")  # nothing loaded yet
    assert run_cli("prepare", "--spec", str(spec_file),
                   "--results-dir", str(results_dir)) == 0
    stats_files = list(results_dir.glob("prepare_*.json"))
    assert len(stats_files) == 1
    stats = json.loads(stats_files[0].read_text())
    assert stats["db_size_bytes"] == 1073741824
    assert stats["db_size_pretty"] == "1.00 GiB"
    assert stats["wall_s"] >= 0
    assert stats["loaded_units"] == "9,000 rows"  # 9 tables x 1000
    assert stats["workload"]["type"] == "oltp_read_write"
    # marker written by fake sysbench: dataset now reads as loaded
    assert run_cli("preflight", "--spec", str(spec_file)) == 0


def test_report_includes_data_load_section(
    fake_env: Path, spec_file, results_dir, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_PSQL_TABLES", "0")
    assert run_cli("prepare", "--spec", str(spec_file),
                   "--results-dir", str(results_dir)) == 0
    monkeypatch.delenv("FAKE_PSQL_TABLES")
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    run_dir = find_run_dir(results_dir)
    assert (run_dir / "env" / "prepare_stats.json").exists()
    html = (run_dir / "report.html").read_text()
    assert "Data load" in html
    assert "1.00 GiB" in html


def test_report_kpi_cards(fake_env, spec_file, results_dir) -> None:
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    html = (find_run_dir(results_dir) / "report.html").read_text()
    assert "Peak QPS" in html
    assert "p99 latency at peak" in html
    assert "Failed levels / SQL errors" in html


# ── live PostgreSQL metrics sampler (Phase 5) ───────────────────────────

def test_pg_delta_row_rates() -> None:
    from pgbench_harness.capture import pg_delta_row
    prev = {"_mono": 100.0, "blks_hit": 1000, "blks_read": 100, "xacts": 500,
            "wal_bytes": 1_000_000, "active": 2, "total_conn": 5,
            "xact_commit": 480, "xact_rollback": 20, "tup_inserted": 100}
    cur = {"_mono": 102.0, "blks_hit": 1900, "blks_read": 110, "xacts": 700,
           "wal_bytes": 3_000_000, "active": 8, "total_conn": 12,
           "xact_commit": 660, "xact_rollback": 40, "tup_inserted": 400}
    row = pg_delta_row(prev, cur, 2.0)              # dt = 2s
    assert row["t"] == 2
    assert row["active"] == 8 and row["total_conn"] == 12
    assert row["xacts_s"] == 100.0                  # 200 xacts / 2s
    assert row["commits_s"] == 90.0 and row["rollbacks_s"] == 10.0
    assert row["tup_inserted_s"] == 150.0          # 300 / 2s
    assert abs(float(row["cache_hit_pct"]) - 98.9) < 0.2   # 900 / 910
    assert abs(row["wal_mb_s"] - (2_000_000 / 1024 / 1024 / 2)) < 0.01  # MiB/s


def test_pg_delta_row_uses_server_clock_and_lock_metrics() -> None:
    """Rates use the server clock delta (db_epoch), not the harness round-trip
    timing, and lock-contention gauges (blocked queries / max wait) are recorded."""
    from pgbench_harness.capture import _sample_dt, pg_delta_row
    base = {"blks_hit": 0, "blks_read": 0, "xacts": 0, "xact_commit": 0,
            "xact_rollback": 0, "wal_bytes": 0, "tup_returned": 0, "tup_fetched": 0,
            "tup_inserted": 0, "tup_updated": 0, "tup_deleted": 0, "deadlocks": 0,
            "conflicts": 0, "temp_bytes": 0, "temp_files": 0, "ckpt_timed": 0,
            "ckpt_req": 0, "ckpt_write_ms": 0, "ckpt_sync_ms": 0, "bgw_clean": 0,
            "bgw_alloc": 0, "active": 4, "total_conn": 9}
    prev = {**base, "db_epoch": 2000.0, "_mono": 50.0, "tup_inserted": 100}
    # the sample's psql round-trip was slow (mono advanced 9s) but the server clock
    # only advanced 3s between the two counter reads — the rate must use 3s.
    cur = {**base, "db_epoch": 2003.0, "_mono": 59.0, "tup_inserted": 400,
           "blocked": 5, "lock_wait_max_s": 2.5}
    assert _sample_dt(prev, cur) == 3.0
    row = pg_delta_row(prev, cur, 3)
    assert row["tup_inserted_s"] == 100.0          # 300 / 3s (server clock), not /9
    assert row["blocked_queries"] == 5 and row["lock_wait_max_s"] == 2.5


def test_pg_delta_row_reset_is_a_gap_not_a_spike() -> None:
    """A counter that went backwards (server restart/failover/reset) yields a
    blank gap — never a misleading 0 or a huge first-delta spike (the WAL
    15,000 MB/s artifact)."""
    from pgbench_harness.capture import pg_delta_row
    prev = {"_mono": 100.0, "wal_bytes": 9_000_000_000, "xacts": 1_000_000,
            "blks_hit": 500, "blks_read": 50}
    cur = {"_mono": 102.0, "wal_bytes": 1_000, "xacts": 10,     # reset to tiny values
           "blks_hit": 5, "blks_read": 1}
    row = pg_delta_row(prev, cur, 2.0)
    assert row["wal_mb_s"] == "" and row["xacts_s"] == ""       # gap, not spike/0
    assert row["cache_hit_pct"] == ""                            # negative access delta


def test_live_pg_sampler_writes_timeseries(fake_env, spec_file, tmp_path) -> None:
    import time
    from pgbench_harness import capture
    from pgbench_harness.spec import load_spec
    spec = load_spec(spec_file)
    run_dir = tmp_path / "run"
    (run_dir / "parsed").mkdir(parents=True)
    sampler = capture.LivePgSampler(spec, "any-nonempty-pw", run_dir, interval_s=1)
    sampler.start()
    time.sleep(2.4)
    sampler.stop()
    lines = (run_dir / "parsed" / "pg_timeseries.csv").read_text().splitlines()
    assert lines[0] == ",".join(capture.LIVE_PG_COLUMNS)
    assert len(lines) >= 2                          # at least one delta row
    cols = dict(zip(capture.LIVE_PG_COLUMNS, lines[1].split(",")))
    assert 0 <= float(cols["cache_hit_pct"]) <= 100
    assert float(cols["wal_mb_s"]) >= 0
    # B4: the richer engine-side set reaches the CSV
    assert float(cols["commits_s"]) > 0 and float(cols["tup_inserted_s"]) > 0
    assert float(cols["blks_read_s"]) >= 0 and float(cols["ckpt_write_ms_s"]) >= 0
    assert cols["repl_replay_lag_s"] == ""           # no replica -> blank, not 0
