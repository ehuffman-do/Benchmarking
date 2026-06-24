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


def test_prepare_idempotent(fake_env, spec_file, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert run_cli("prepare", "--spec", str(spec_file)) == 0  # dataset already present


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

    summary = json.loads((run_dir / "parsed" / "summary.json").read_text())
    assert {(l["rep"], l["threads"]) for l in summary["levels"]} == \
        {(1, 1), (1, 4), (2, 1), (2, 4)}
    assert all(l["qps_avg"] > 0 for l in summary["levels"])
    assert all(l["steady_state_window"] == [1, 5] for l in summary["levels"])


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


def _write_spec(tmp_path: Path, **sweep_overrides: object) -> Path:
    """Write a spec with sweep overrides (e.g. retries) to disk; return its path."""
    import yaml

    from conftest import make_spec_doc
    doc = make_spec_doc(sweep={**make_spec_doc()["sweep"], **sweep_overrides})
    # Keep report.timeseries_levels consistent with any overridden thread ladder.
    threads = doc["sweep"]["threads"]
    doc["report"]["timeseries_levels"] = [t for t in doc["report"]["timeseries_levels"]
                                          if t in threads]
    path = tmp_path / "retry.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_transient_failure_auto_recovers_with_retries(
    fake_env, results_dir, monkeypatch, tmp_path
) -> None:
    """A level that fails once then succeeds is retried and ends `ok` (run stays
    complete), recording its attempt count — the failover-resilience path."""
    spec_file = _write_spec(tmp_path, retries=2, retry_wait_s=0)
    monkeypatch.setenv("FAKE_SYSBENCH_FAIL_FIRST_N", "1")  # only the first attempt fails
    rc = run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir))
    assert rc == 0  # complete, not partial — the retry rode out the blip
    run_dir = find_run_dir(results_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    by_key = {(l["rep"], l["threads"]): l for l in manifest["levels"]}
    first = by_key[(1, 1)]
    assert first["status"] == "ok"
    assert first["attempts"] == 2  # failed once, recovered on the retry
    assert all(l["attempts"] == 1 for k, l in by_key.items() if k != (1, 1))
    # The failed attempt's log is preserved for forensics.
    assert (run_dir / "raw" / "rep1_t001.attempt1.log").exists()
    html = (run_dir / "report.html").read_text()
    assert "Availability" in html
    assert "auto-recovered" in html.lower()


def test_exhausted_retries_records_incident(
    fake_env, results_dir, monkeypatch, tmp_path
) -> None:
    """When every attempt fails, the level is `failed` after 1+retries tries and
    the report shows it as an outage incident."""
    spec_file = _write_spec(tmp_path, threads=[1], repetitions=3, retries=1, retry_wait_s=0)
    monkeypatch.setenv("FAKE_SYSBENCH_FAIL_THREADS", "1")  # every attempt fails
    rc = run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir))
    assert rc == 1  # partial/failed
    run_dir = find_run_dir(results_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert all(l["status"] == "failed" and l["attempts"] == 2 for l in manifest["levels"])
    html = (run_dir / "report.html").read_text()
    assert "Availability &amp; incidents" in html
    assert "Outage incidents" in html


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
    assert html.count("data:image/png;base64,") >= 2  # overlaid QPS + p99 charts
    assert "Settings diff" in html


def test_list_runs(fake_env, spec_file, results_dir, capsys) -> None:
    assert run_cli("run", "--spec", str(spec_file), "--results-dir", str(results_dir)) == 0
    capsys.readouterr()
    assert run_cli("list", "--results-dir", str(results_dir)) == 0
    out = capsys.readouterr().out
    assert "test-tiny" in out
    assert "complete" in out
    assert "4/4" in out


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
