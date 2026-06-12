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
