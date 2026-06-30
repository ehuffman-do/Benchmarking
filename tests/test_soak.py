"""Soak / resilience mode: metric correctness (synthetic timelines), spec
validation, and an end-to-end run + mark + report through the CLI."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from pgbench_harness.cli import main
from pgbench_harness.errors import SpecError
from pgbench_harness.soak import disruption_metrics, resolve_baseline_window
from pgbench_harness.spec import ReportCfg, parse_spec

from conftest import TEST_PASSWORD, make_spec_doc

CFG = ReportCfg(recovery_threshold_pct=95.0, full_recovery_pct=100.0,
                recovery_hold_s=10, latency_spike_mult=2.0)


def _row(tps, lat, err=0.0, reconn=0.0):
    return {"tps": tps, "qps": tps * 20, "lat_p99": lat, "err_s": err, "reconn_s": reconn}


def _scenario_timeline() -> dict[int, dict]:
    """Baseline 100 tps/10ms; event@60; 10s outage (5 zero-tps + 5 gap);
    re-warm tail at 96 tps then full 100; latency spikes during the window."""
    tl: dict[int, dict] = {}
    for t in range(0, 60):
        tl[t] = _row(100, 10)
    for t in range(60, 65):           # zero-tps with errors/reconnects
        tl[t] = _row(0.0, 200, err=10.0, reconn=2.0)
    # 65..69 missing entirely (hard gap)
    for t in range(70, 90):           # recovered to 95% but not full; latency elevated
        tl[t] = _row(96, 30)
    for t in range(90, 121):          # full re-warm to baseline
        tl[t] = _row(100, 10)
    return tl


def test_disruption_metrics_known_outage() -> None:
    tl = _scenario_timeline()
    m = disruption_metrics(tl, event_off=60, win_end=120,
                           baseline_tps=100.0, baseline_lat=10.0, cfg=CFG)
    assert m["hard_downtime_s"] == 10            # 60..69 (5 zero + 5 missing)
    assert m["downtime_start_offset"] == 0
    assert m["time_to_first_success_s"] == 10     # first tps>0 at t=70
    assert m["error_window_s"] == 5               # errors at 60..64
    assert m["error_seconds"] == 5
    assert m["reconnect_count"] == 10.0           # 2/s x 5s
    assert m["ttr_s"] == 10                        # >=95% sustained from t=70
    assert m["full_recovery_s"] == 30             # >=100% sustained from t=90
    assert m["full_recovery_s"] > m["ttr_s"]      # re-warm tail is distinct & longer
    assert m["peak_p99_ms"] == 200.0
    assert m["peak_p99_at_s"] == 0
    assert m["seconds_p99_above"] == 25           # 5 (downtime@200) + 20 (tail@30) > 20
    assert m["txns_failed"] == 50.0               # 10/s x 5s
    # missed vs baseline: 5x100 (zero) + 5x100 (gap) + 20x4 (96 vs 100) = 1080
    assert m["missed_vs_baseline"] == 1080.0


def test_disruption_no_recovery_within_window() -> None:
    tl = {t: _row(100, 10) for t in range(0, 60)}
    for t in range(60, 121):
        tl[t] = _row(0.0, 0.0, err=5.0)           # never comes back
    m = disruption_metrics(tl, 60, 120, 100.0, 10.0, CFG)
    assert m["hard_downtime_s"] == 61
    assert m["ttr_s"] is None
    assert m["full_recovery_s"] is None
    assert m["time_to_first_success_s"] is None


def test_disruption_graceful_dip_no_hard_downtime() -> None:
    tl = {t: _row(100, 10) for t in range(0, 60)}
    for t in range(60, 75):
        tl[t] = _row(80, 18)                       # dips but never zero
    for t in range(75, 121):
        tl[t] = _row(100, 10)
    m = disruption_metrics(tl, 60, 120, 100.0, 10.0, CFG)
    assert m["hard_downtime_s"] == 0
    assert m["ttr_s"] == 15                        # back to >=95% sustained at t=75
    assert m["seconds_p99_above"] == 0            # 18 < 2x10


def test_recovery_near_window_end_not_false_negative() -> None:
    """A healthy tail shorter than recovery_hold_s must NOT read 'never recovered'."""
    tl = {t: _row(100, 10) for t in range(0, 65)}      # all healthy
    m = disruption_metrics(tl, event_off=60, win_end=64, baseline_tps=100.0,
                           baseline_lat=10.0, cfg=CFG)   # only 5s after event < hold(10)
    assert m["hard_downtime_s"] == 0
    assert m["ttr_s"] == 0                               # recovered immediately, not None
    assert m["full_recovery_s"] == 0


def test_recovery_delayed_by_gap_inside_hold() -> None:
    tl = {t: _row(100, 10) for t in range(0, 60)}
    for t in range(60, 65):
        tl[t] = _row(0.0, 0.0, err=5.0)                 # 5s outage
    for t in range(65, 121):
        tl[t] = _row(100, 10)
    del tl[70]                                           # a single missing sample mid-recovery
    m = disruption_metrics(tl, 60, 120, 100.0, 10.0, CFG)
    assert m["ttr_s"] == 11                              # recovery detected after the gap at 71


def test_no_baseline_makes_dependent_metrics_none() -> None:
    """baseline_tps==0 (degenerate) -> recovery/missed/spike metrics are None, not 0."""
    tl = {t: _row(0.0, 0.0) for t in range(60, 121)}
    m = disruption_metrics(tl, 60, 120, baseline_tps=0.0, baseline_lat=0.0, cfg=CFG)
    assert m["ttr_s"] is None
    assert m["full_recovery_s"] is None
    assert m["missed_vs_baseline"] is None
    assert m["seconds_p99_above"] is None


def test_resolve_baseline_window_early_event_degenerate() -> None:
    assert resolve_baseline_window({}, 1000, [{"_offset": 3}], None) == (0, 0)  # too early
    a, b = resolve_baseline_window({}, 1000, [{"_offset": 20}], None)
    assert (a, b) == (0, 15)                              # strictly before the event


def test_resolve_baseline_window_defaults() -> None:
    assert resolve_baseline_window({}, 1000, [], None) == (200, 800)   # middle 60%
    ev = [{"_offset": 600}]
    a, b = resolve_baseline_window({}, 1000, ev, None)
    assert a < b <= 600                            # clean span before the event
    assert resolve_baseline_window({}, 1000, ev, (10, 50)) == (10, 50)  # explicit wins


# ── automatic detection + run profile (B1/B2) ──────────────────────

def _flat(tps, lat=10.0, err=0.0, reconn=0.0):
    return {"tps": tps, "qps": tps * 20, "lat_p99": lat, "err_s": err, "reconn_s": reconn,
            "qps_r": tps * 10, "qps_w": tps * 7, "qps_o": tps * 3}


def test_detect_downtime_failover_and_error_burst() -> None:
    from pgbench_harness import detect
    tl = {t: _flat(100) for t in range(0, 60)}
    for t in range(60, 66):                       # 6s hard outage with errors
        tl[t] = _flat(0.0, lat=0.0, err=5.0, reconn=1.0)
    for t in range(66, 120):
        tl[t] = _flat(100)
    cands = detect.detect_anomalies(tl, 100.0, 10.0, 119, CFG)
    dt = next(c for c in cands if c["type"] == "downtime")
    assert dt["at_s"] == 60 and dt["evidence"]["duration_s"] == 6
    assert dt["status"] == "detected_unconfirmed" and 0 < dt["confidence"] <= 1
    assert any(c["type"] == "error_burst" for c in cands)


def test_detect_latency_spike_and_no_false_positive_on_steady() -> None:
    from pgbench_harness import detect
    tl = {t: _flat(100) for t in range(0, 120)}
    assert detect.detect_anomalies(tl, 100.0, 10.0, 119, CFG) == []   # steady -> nothing
    for t in range(40, 45):
        tl[t] = _flat(100, lat=50.0)              # 5s p99 spike > 2x baseline
    cands = detect.detect_anomalies(tl, 100.0, 10.0, 119, CFG)
    assert any(c["type"] == "latency_spike" and c["at_s"] == 40 for c in cands)


def test_build_run_profile_aggregates(tmp_path) -> None:
    from pgbench_harness.soak import build_run_profile
    tl = {t: _flat(100) for t in range(0, 120)}
    rp = build_run_profile(tl, 119, (50, 95, 99), tmp_path)  # no raw logs -> latency empty
    assert rp["tps"]["median"] == 100.0 and rp["tps"]["cov_pct"] == 0.0
    assert rp["qps_read_mean"] == 1000.0
    assert rp["zero_or_gap_seconds"] == 0 and rp["longest_outage_s"] == 0
    assert rp["latency_ms"] == {}


# ── spec validation ────────────────────────────────────────────────

def _soak_doc(**over):
    doc = make_spec_doc()
    doc.pop("sweep")
    doc.pop("report", None)
    doc["soak"] = {"threads": 32, "duration_s": 120, "tolerate_errors": True}
    doc["report"] = {"baseline_window_s": [10, 50], "recovery_threshold_pct": 95}
    for k, v in over.items():
        doc[k] = v
    return doc


def test_valid_soak_spec() -> None:
    spec = parse_spec(_soak_doc())
    assert spec.is_soak
    assert spec.sweep is None
    assert spec.soak.threads == 32
    assert spec.report.baseline_window_s == (10, 50)


def test_soak_and_sweep_mutually_exclusive() -> None:
    doc = _soak_doc()
    doc["sweep"] = {"threads": [1], "duration_s": 10}
    with pytest.raises(SpecError, match="mutually exclusive"):
        parse_spec(doc)


def test_spec_events_section_rejected() -> None:
    """Events are no longer pre-declared in the spec — an `events:` section is now
    an unknown top-level key (events come from auto-detection or operator marks)."""
    doc = _soak_doc()
    doc["events"] = [{"at_s": 60, "type": "failover"}]
    with pytest.raises(SpecError, match="unknown top-level section.*events"):
        parse_spec(doc)


# ── end-to-end (fake sysbench/psql) ────────────────────────────────

def test_soak_end_to_end(fake_env, tmp_path, monkeypatch) -> None:
    results = tmp_path / "results"
    spec_path = tmp_path / "soak.yaml"
    doc = _soak_doc(soak={"threads": 4, "duration_s": 2, "tolerate_errors": True})
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert main(["soak", "--spec", str(spec_path), "--results-dir", str(results)]) in (0, 1)
    run_dir = sorted(d for d in results.iterdir() if (d / "manifest.json").exists())[-1]

    # artifacts
    assert (run_dir / "parsed" / "soak_summary.json").exists()
    assert (run_dir / "parsed" / "soak_timeseries.csv").exists()
    ts_lines = (run_dir / "parsed" / "soak_timeseries.csv").read_text().splitlines()
    # B3: the read/write/other QPS split + per-interval percentile reach the series
    assert ts_lines[0].split(",")[-4:] == ["qps_r", "qps_w", "qps_o", "lat_p99_pct"]
    assert float(ts_lines[1].split(",")[9]) > 0          # qps_r populated, not blank
    assert list(run_dir.glob("raw/soak_seg*.log"))
    summary = json.loads((run_dir / "parsed" / "soak_summary.json").read_text())
    assert summary["mode"] == "soak"
    # Events are no longer spec-seeded — the run starts with none confirmed; they
    # arrive via auto-detection or operator marks (asserted after the mark below).
    assert isinstance(summary["events"], list)
    # B1: the always-on run profile is present and populated even on a tiny run
    assert summary["run_profile"]["tps"] and "detected" in summary
    # B7: auto-generated narrative verdict leads the report
    assert summary["tldr"].startswith("Soak:") and "status:" in summary["tldr"]

    # mark adds a live event, report regenerates and is mode-aware
    assert main(["mark", "--run-dir", str(run_dir), "--type", "scale_up",
                 "--label", "resize"]) == 0
    assert main(["report", "--run-dir", str(run_dir)]) == 0
    # the operator mark is the source of the confirmed event now (and creates
    # events.jsonl, which a clean no-event soak never writes)
    assert (run_dir / "events.jsonl").exists()
    summary2 = json.loads((run_dir / "parsed" / "soak_summary.json").read_text())
    assert any(e["type"] == "scale_up" for e in summary2["events"])
    html = (run_dir / "soak_report.html").read_text()
    assert "Resilience" in html
    assert "Methodology" in html
    assert "hard downtime" in html
    assert "Steady-state" in html              # B1: always-on run profile section
    assert "data:image/png;base64," in html
    assert "overflow-y: auto" in html          # root scrolls (uPlot's scoped overflow:hidden is fine)
    assert "Interactive timeline" in html and "new uPlot(" in html   # B7: inline interactive charts
    assert 'src="http' not in html and 'href="http' not in html      # offline: no external loads

    # secret never reaches any soak artifact
    leaks = [str(p) for p in run_dir.rglob("*")
             if p.is_file() and TEST_PASSWORD.encode() in p.read_bytes()]
    assert not leaks


def test_interactive_payload_live_without_summary(tmp_path) -> None:
    """The in-app timeseries builder works MID-RUN: series come from a (partially
    written) soak_timeseries.csv, and markers are recomputed live from events.jsonl
    against the manifest soak start — with NO soak_summary.json present yet."""
    from pgbench_harness import report_soak, runner
    run_dir = tmp_path / "run-live"
    (run_dir / "parsed").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps(
        {"run_id": "run-live", "mode": "soak",
         "soak": {"start_utc": "2026-06-30T00:00:00.000000Z", "target_duration_s": 600}}),
        encoding="utf-8")
    cols = "t,ts_utc,tps,qps,lat_p99,err_s,reconn_s,threads,seg,qps_r,qps_w,qps_o,lat_p99_pct"
    rows = [cols,
            "0,2026-06-30T00:00:00.000000Z,100,2000,40,0,0,4,soak_seg01,1000,700,300,99",
            "1,2026-06-30T00:00:01.000000Z,110,2200,41,0,0,4,soak_seg01,1100,770,330,99",
            "2,2026-06-30T00:00:02.000000Z,120,2400,42,0,0,4,soak_seg01,1200,840,360,99",
            "x-partial-trailing-line"]   # truncated live append -> must be skipped, not crash
    (run_dir / "parsed" / "soak_timeseries.csv").write_text("\n".join(rows), encoding="utf-8")
    # operator stamps a failover at t=2 while the run is still in flight
    assert runner.cmd_mark(run_dir, "failover", "primary", "", at_s=2) == 0
    payload = report_soak.interactive_payload(run_dir)
    assert payload is not None
    assert payload["t"] == [0, 1, 2] and len(payload["tps"]) == 3
    assert any(mk["t"] == 2 and mk["kind"] == "event" for mk in payload["markers"])
    assert not (run_dir / "parsed" / "soak_summary.json").exists()


def test_soak_surfaces_failure_and_finalizes(fake_env, tmp_path, monkeypatch) -> None:
    """A soak whose load generator can't run must FAIL FAST, surface sysbench's
    own error, and leave a TERMINAL manifest — never an indistinguishable blank,
    never stuck 'running' (the field-incident regression)."""
    monkeypatch.setenv("FAKE_SYSBENCH_RUN_FAIL_THREADS", "64")
    results = tmp_path / "results"
    spec_path = tmp_path / "soak.yaml"
    doc = _soak_doc(soak={"threads": 64, "duration_s": 300, "tolerate_errors": True,
                          "fast_fail_segments": 2})
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    t0 = time.monotonic()
    rc = main(["soak", "--spec", str(spec_path), "--results-dir", str(results)])
    elapsed = time.monotonic() - t0
    assert rc == 2                       # RunError -> CLI exit 2 -> worker maps to 'failed'
    assert elapsed < 30                  # fast-fail in seconds, not the 300s window

    run_dir = sorted(d for d in results.iterdir() if (d / "manifest.json").exists())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "failed"          # terminal, NOT 'running'
    assert manifest.get("finished_utc")

    summary = json.loads((run_dir / "parsed" / "soak_summary.json").read_text())
    assert summary["status"] == "failed"
    assert "deadlock" in summary["failure_reason"] or "thread_init" in summary["failure_reason"]
    assert summary["failed_segments"] >= 1
    assert len(summary["segments"]) == 2           # aborted at the cutoff, did not churn
    assert all(s["error_excerpt"] for s in summary["segments"])

    # the failure reason is rendered in the report (not a near-blank deliverable)
    assert main(["report", "--run-dir", str(run_dir)]) == 0
    html = (run_dir / "soak_report.html").read_text()
    assert "Run failed" in html and ("deadlock" in html or "thread_init" in html)

    # secret never leaks even on the failure path
    assert not [p for p in run_dir.rglob("*")
                if p.is_file() and TEST_PASSWORD.encode() in p.read_bytes()]


def test_soak_rides_through_outage_after_producing_samples(fake_env, tmp_path, monkeypatch) -> None:
    """Once a soak has produced samples, an outage (0-sample segments) must NOT
    trip the fast-fail — the whole point of a failover soak is to survive it."""
    monkeypatch.setenv("FAKE_SYSBENCH_COUNT_FILE", str(tmp_path / "cnt"))
    monkeypatch.setenv("FAKE_SYSBENCH_FAIL_AFTER", "1")   # seg 1 OK, seg 2+ = outage
    results = tmp_path / "results"
    spec_path = tmp_path / "soak.yaml"
    doc = _soak_doc(soak={"threads": 4, "duration_s": 6, "tolerate_errors": True,
                          "fast_fail_segments": 2, "segment_kill_grace_s": 1,
                          "hard_ceiling_grace_s": 2})
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    rc = main(["soak", "--spec", str(spec_path), "--results-dir", str(results)])
    assert rc in (0, 1)                       # rode through -> finalized, NOT fast-failed (rc 2)
    run_dir = sorted(d for d in results.iterdir() if (d / "manifest.json").exists())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] in ("partial", "complete")          # terminal, not failed-fast
    segs = manifest["soak"]["segments"]
    assert any(s["intervals"] > 0 for s in segs)                  # produced samples first
    assert len(segs) >= 3                                         # then relaunched through the outage


def test_soak_segment_watchdog_kills_hung_child(fake_env, tmp_path, monkeypatch) -> None:
    """A segment that connects then hangs (no output, never exits) must be killed
    by the per-segment watchdog so the supervisor stays bounded and finalizes."""
    monkeypatch.setenv("FAKE_SYSBENCH_HANG_THREADS", "4")
    results = tmp_path / "results"
    spec_path = tmp_path / "soak.yaml"
    doc = _soak_doc(soak={"threads": 4, "duration_s": 2, "tolerate_errors": True,
                          "segment_kill_grace_s": 1, "hard_ceiling_grace_s": 3,
                          "fast_fail_segments": 5})
    spec_path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    t0 = time.monotonic()
    main(["soak", "--spec", str(spec_path), "--results-dir", str(results)])
    elapsed = time.monotonic() - t0
    assert elapsed < 30                  # the hang did not block the supervisor forever

    run_dir = sorted(d for d in results.iterdir() if (d / "manifest.json").exists())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] in ("failed", "partial")   # terminal
    segs = manifest["soak"]["segments"]
    assert segs and any(s.get("timed_out") for s in segs)


def test_live_soak_callback_dedups_offsets(tmp_path) -> None:
    """The live soak tap appends per-second rows keyed on read-time offset, with
    build_timeline's first-seen-wins / non-negative dedup, so the live file
    matches the canonical one at the finalize swap."""
    from datetime import datetime, timezone

    from pgbench_harness.runner import _live_soak_callback
    from pgbench_harness.soak import TIMESERIES_COLUMNS
    from pgbench_harness.summarize import IncrementalCsvWriter

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    live = IncrementalCsvWriter(tmp_path / "ts.csv", TIMESERIES_COLUMNS)
    cb = _live_soak_callback(live, base, set(), "soak_seg01")
    line = ("[ 1s ] thds: 4 tps: 10.00 qps: 200.00 (r/w/o: 100.00/66.00/34.00) "
            "lat (ms,99%): 5.00 err/s 0.00 reconn/s: 0.00")
    cb("2026-01-01T00:00:01.000000Z", line)   # offset 1
    cb("2026-01-01T00:00:01.400000Z", line)   # rounds to offset 1 -> deduped
    cb("2026-01-01T00:00:02.000000Z", line)   # offset 2
    cb("non-iso", line)                         # unparseable ts -> ignored
    live.close()
    rows = (tmp_path / "ts.csv").read_text().splitlines()
    assert rows[0].startswith("t,ts_utc")
    assert len(rows) == 3                        # header + 2 unique offsets
    assert rows[1].startswith("1,") and rows[2].startswith("2,")


def test_soak_dry_run(fake_env, tmp_path, capsys) -> None:
    spec_path = tmp_path / "soak.yaml"
    spec_path.write_text(yaml.safe_dump(_soak_doc()), encoding="utf-8")
    assert main(["soak", "--spec", str(spec_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "sysbench" in out and "--threads=32" in out
    # events are auto-detected or operator-marked, never pre-declared in the spec
    assert "auto-detected" in out and "mark" in out
    assert TEST_PASSWORD not in out
