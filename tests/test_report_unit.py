"""Unit tests for report/compare internals and the audit-driven bug fixes."""

from __future__ import annotations

from pathlib import Path

from pgbench_harness import report
from pgbench_harness.manifest import STATUS_OK, Level, Manifest, plan_levels
from pgbench_harness.summarize import io_delta
from pgbench_harness.util import atomic_write_text, get_redactor
from pgbench_harness.runner import _wall_time_s


def _summary(levels: list[dict]) -> dict:
    return {"run_id": "r", "percentiles": [50, 95, 99], "levels": levels}


def test_chart_handles_zero_peak_without_crashing() -> None:
    """A level that ran but did zero work (qps_avg == 0) must not crash set_ylim."""
    summary = _summary([
        {"rep": 1, "threads": 1, "status": STATUS_OK, "qps_avg": 0.0, "tps_avg": 0.0,
         "lat_p50": None, "lat_p95": None, "lat_p99": None},
    ])
    img = report.chart_metric_vs_threads(summary, "qps_avg", "QPS", "QPS")
    assert img is not None and img.startswith("data:image/png;base64,")


def test_chart_none_when_no_ok_levels() -> None:
    summary = _summary([{"rep": 1, "threads": 1, "status": "failed"}])
    assert report.chart_metric_vs_threads(summary, "qps_avg", "QPS", "QPS") is None


def test_wall_time_sums_level_durations_not_elapsed() -> None:
    """Wall time = sum of per-level durations, robust to resume idle gaps."""
    m = Manifest(run_id="r", label="l", edition="standard", tshirt_size="x",
                 levels=plan_levels((1, 2), 1))
    m.created_utc = "2026-01-01T00:00:00Z"
    m.level(1, 1).started_utc = "2026-01-01T00:00:00Z"
    m.level(1, 1).finished_utc = "2026-01-01T00:00:30Z"   # 30s
    # Simulate a resume days later for the second level.
    m.level(1, 2).started_utc = "2026-01-03T10:00:00Z"
    m.level(1, 2).finished_utc = "2026-01-03T10:00:45Z"   # 45s
    assert _wall_time_s(m) == 75.0   # 30 + 45, NOT the multi-day elapsed span


def test_wall_time_ignores_unfinished_levels() -> None:
    m = Manifest(run_id="r", label="l", edition="standard", tshirt_size="x",
                 levels=plan_levels((1,), 1))
    assert _wall_time_s(m) == 0.0


def test_atomic_write_text_redacts_registered_secret(tmp_path: Path) -> None:
    """Any file written via atomic_write_text must not contain the secret."""
    secret = "p@ss-unit-test-DO-NOT-LEAK"
    get_redactor().register(secret)
    target = tmp_path / "leak.json"
    atomic_write_text(target, f'{{"error": "FATAL: password={secret} rejected"}}')
    body = target.read_text()
    assert secret not in body
    assert "***" in body


def test_atomic_write_text_can_opt_out_of_redaction(tmp_path: Path) -> None:
    secret = "another-unique-secret-xyz"
    get_redactor().register(secret)
    target = tmp_path / "raw.txt"
    atomic_write_text(target, secret, redact=False)
    assert target.read_text() == secret


def test_io_delta_computes_rates(tmp_path: Path) -> None:
    """pre/post pg_stat_io snapshots become per-second rates over the level."""
    import json

    snap = {
        "pre": {"io": {"reads": 0, "writes": 0, "extends": 0, "fsyncs": 0, "hits": 0},
                "db": {"blks_read": 0, "blks_hit": 0},
                "wal": {"wal_records": 0, "wal_bytes": 0, "wal_fpi": 0}},
        "post": {"io": {"reads": 2000, "writes": 1000, "extends": 100, "fsyncs": 50, "hits": 18000},
                 "db": {"blks_read": 1000, "blks_hit": 9000},
                 "wal": {"wal_records": 4000, "wal_bytes": 20 * 1024 * 1024, "wal_fpi": 100}},
    }
    p = tmp_path / "rep1_t004_iostats.json"
    p.write_text(json.dumps(snap), encoding="utf-8")
    d = io_delta(p, duration_s=10)
    assert d is not None
    assert d["read_ops_s"] == 200.0          # 2000 reads / 10s
    assert d["write_ops_s"] == 100.0         # 1000 writes / 10s
    assert d["fsync_s"] == 5.0
    assert d["read_mb"] == round(2000 * 8192 / 1024 / 1024, 1)
    assert d["cache_hit_pct"] == 90.0        # 9000 / (9000+1000) from pg_stat_database
    assert d["wal_mb"] == 20.0
    assert d["wal_mb_s"] == 2.0


def test_io_delta_absent_or_unusable() -> None:
    assert io_delta(Path("/nonexistent/iostats.json"), 10) is None


def test_io_delta_handles_missing_sources(tmp_path: Path) -> None:
    """Old server: only pg_stat_database present, pg_stat_io null."""
    import json

    snap = {"pre": {"io": None, "db": {"blks_read": 0, "blks_hit": 0}, "wal": None},
            "post": {"io": None, "db": {"blks_read": 100, "blks_hit": 900}, "wal": None}}
    p = tmp_path / "io.json"
    p.write_text(json.dumps(snap), encoding="utf-8")
    d = io_delta(p, duration_s=10)
    assert d is not None
    assert "read_ops_s" not in d              # no pg_stat_io
    assert d["cache_hit_pct"] == 90.0         # still derivable from blks
