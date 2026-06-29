"""Steady-state summarization tests (warm-up trimming, summary.json contract)."""

from __future__ import annotations

import json
import shutil
import statistics
from pathlib import Path

from pgbench_harness.manifest import STATUS_FAILED, STATUS_OK, Manifest, plan_levels
from pgbench_harness.parser import parse_log_file
from pgbench_harness.spec import parse_spec
from pgbench_harness.summarize import IncrementalCsvWriter, summarize_level, write_parsed

from conftest import make_spec_doc

FIXTURES = Path(__file__).parent / "fixtures"


def test_incremental_csv_writer_appends_flushes_and_is_readable_mid_stream(tmp_path):
    """The live cockpit writer must flush each row (readable mid-run) and write
    the header exactly once across re-opens (so --resume keeps prior rows)."""
    p = tmp_path / "live.csv"
    w = IncrementalCsvWriter(p, ["a", "b"])
    w.append([1, 2])
    assert p.read_text().splitlines() == ["a,b", "1,2"]   # flushed before close
    w.append([3, 4])
    assert len(p.read_text().splitlines()) == 3
    w.close()
    w2 = IncrementalCsvWriter(p, ["a", "b"])              # re-open == resume
    w2.append([5, 6])
    w2.close()
    lines = p.read_text().splitlines()
    assert lines.count("a,b") == 1 and lines[-1] == "5,6" and len(lines) == 4


def test_summarize_level_trims_warmup() -> None:
    doc = make_spec_doc(sweep={"threads": [64], "duration_s": 10, "warmup_s": 3,
                               "repetitions": 1, "cooldown_s": 0},
                        report={"percentiles": [50, 95, 99], "timeseries_levels": [],
                                "variance_warn_pct": 10})
    spec = parse_spec(doc)
    parsed = parse_log_file(FIXTURES / "success_with_histogram.log")
    out = summarize_level(parsed, spec, spec.report.percentiles)
    steady_qps = [s.qps for s in parsed.samples if s.t_offset > 3]
    assert out["samples_total"] == 10
    assert out["samples_steady"] == 7
    assert out["qps_avg"] == round(statistics.fmean(steady_qps), 2)
    # warm-up samples (lower qps ramp) must not drag the average down
    assert out["qps_avg"] > statistics.fmean(s.qps for s in parsed.samples)
    assert out["steady_state_window"] == [3, 10]
    assert out["errors"] == 2.0       # err/s spike at t=6 is inside the window
    assert out["reconnects"] == 1.0
    assert out["lat_p50"] < out["lat_p95"] < out["lat_p99"]
    assert out["lat_max"] == 225.65
    assert out["transactions"] == 12345


def test_write_parsed_builds_contract(tmp_path: Path) -> None:
    doc = make_spec_doc(sweep={"threads": [64], "duration_s": 10, "warmup_s": 3,
                               "repetitions": 2, "cooldown_s": 0},
                        report={"percentiles": [50, 95, 99], "timeseries_levels": [],
                                "variance_warn_pct": 10})
    spec = parse_spec(doc)
    run_dir = tmp_path / "run"
    (run_dir / "raw").mkdir(parents=True)
    manifest = Manifest(run_id="rid", label="l", edition="advanced", tshirt_size="8c32g",
                        levels=plan_levels((64,), 2))
    for lvl, fixture, status in [
        (manifest.levels[0], "success_with_histogram.log", STATUS_OK),
        (manifest.levels[1], "failure_pgbouncer.log", STATUS_FAILED),
    ]:
        lvl.raw_log = f"raw/{lvl.key}.log"
        lvl.status = status
        shutil.copy(FIXTURES / fixture, run_dir / lvl.raw_log)
    manifest.finalize_status()
    summary = write_parsed(run_dir, spec, manifest)

    on_disk = json.loads((run_dir / "parsed" / "summary.json").read_text())
    assert on_disk == summary
    assert summary["run_id"] == "rid"
    assert summary["status"] == "partial"
    ok = summary["levels"][0]
    assert ok["status"] == "ok" and ok["qps_avg"] is not None
    failed = summary["levels"][1]
    assert failed["status"] == "failed"
    assert "max_client_conn" in failed["error_excerpt"]

    csv_text = (run_dir / "parsed" / "samples.csv").read_text().splitlines()
    assert csv_text[0].startswith("run_id,rep,threads,t_offset,tps,qps")
    assert len(csv_text) == 1 + 10  # header + 10 samples from the ok level only
