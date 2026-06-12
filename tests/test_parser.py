"""Parser unit tests against real sysbench output formats."""

from __future__ import annotations

from pathlib import Path

import pytest

from pgbench_harness.parser import (
    parse_histogram,
    parse_interval_line,
    parse_log_file,
    parse_summary,
    percentile_from_histogram,
    trim_warmup,
)

FIXTURES = Path(__file__).parent / "fixtures"

SPEC_EXAMPLE_LINE = (
    "[ 10s ] thds: 64 tps: 1234.56 qps: 24691.20 "
    "(r/w/o: 12345.60/8230.40/4115.20) lat (ms,99%): 45.79 err/s: 0.00 reconn/s: 0.00"
)


def test_parse_interval_line_exact_fields() -> None:
    s = parse_interval_line(SPEC_EXAMPLE_LINE)
    assert s is not None
    assert s.t_offset == 10
    assert s.threads == 64
    assert s.tps == 1234.56
    assert s.qps == 24691.20
    assert (s.r, s.w, s.o) == (12345.60, 8230.40, 4115.20)
    assert s.lat_pct == 99
    assert s.lat_ms == 45.79
    assert s.err_s == 0.0
    assert s.reconn_s == 0.0


@pytest.mark.parametrize("line", [
    "Threads started!",
    "transactions:                        12345  (1234.50 per sec.)",
    "       4.910 |*                                        28",
    "",
])
def test_parse_interval_line_rejects_non_interval(line: str) -> None:
    assert parse_interval_line(line) is None


def test_parse_success_fixture() -> None:
    parsed = parse_log_file(FIXTURES / "success_with_histogram.log")
    assert len(parsed.samples) == 10
    assert parsed.samples[0].t_offset == 1
    assert parsed.samples[-1].qps == 25020.00
    assert parsed.samples[5].err_s == 2.0
    assert parsed.samples[6].reconn_s == 1.0
    assert parsed.summary is not None
    assert parsed.summary.transactions == 12345
    assert parsed.summary.transactions_per_s == 1234.50
    assert parsed.summary.queries == 246912
    assert parsed.summary.ignored_errors == 2
    assert parsed.summary.reconnects == 1
    assert parsed.summary.total_time_s == 10.0021
    assert parsed.summary.lat_min == 2.32
    assert parsed.summary.lat_avg == 18.12
    assert parsed.summary.lat_max == 225.65
    assert parsed.summary.lat_declared_pct == 99
    assert parsed.summary.lat_declared_value == 45.79
    assert len(parsed.histogram) == 26
    assert parsed.histogram[0] == (4.910, 28)
    assert parsed.histogram[-1] == (114.72, 2)


def test_histogram_only_after_header() -> None:
    text = "       4.910 |*  28\nLatency histogram (values are in milliseconds)\n" \
           "       value  ------------- distribution ------------- count\n" \
           "       1.500 |**                                       10\n"
    assert parse_histogram(text) == [(1.5, 10)]


def test_percentile_interpolation_simple() -> None:
    buckets = [(1.0, 50), (2.0, 50)]
    assert percentile_from_histogram(buckets, 50) == pytest.approx(1.0)
    assert percentile_from_histogram(buckets, 75) == pytest.approx(1.5)
    assert percentile_from_histogram(buckets, 100) == pytest.approx(2.0)
    assert percentile_from_histogram(buckets, 25) == pytest.approx(0.5)


def test_percentile_edge_cases() -> None:
    assert percentile_from_histogram([], 50) is None
    assert percentile_from_histogram([(5.0, 0)], 50) is None


def test_percentiles_monotonic_on_fixture() -> None:
    parsed = parse_log_file(FIXTURES / "success_with_histogram.log")
    p50 = percentile_from_histogram(parsed.histogram, 50)
    p95 = percentile_from_histogram(parsed.histogram, 95)
    p99 = percentile_from_histogram(parsed.histogram, 99)
    assert p50 is not None and p95 is not None and p99 is not None
    assert p50 < p95 < p99
    assert 10 < p50 < 25       # bulk of the fixture distribution
    assert p99 < 114.72        # never beyond the last bucket


def test_trim_warmup() -> None:
    parsed = parse_log_file(FIXTURES / "success_with_histogram.log")
    steady = trim_warmup(parsed.samples, 3)
    assert len(steady) == 7
    assert min(s.t_offset for s in steady) == 4
    assert trim_warmup(parsed.samples, 0) == parsed.samples


def test_failure_worker_threads_fixture() -> None:
    parsed = parse_log_file(FIXTURES / "failure_worker_threads.log")
    assert parsed.samples == []
    assert parsed.summary is None
    assert any("Worker threads failed to initialize" in l for l in parsed.error_lines)


def test_failure_pgbouncer_fixture() -> None:
    parsed = parse_log_file(FIXTURES / "failure_pgbouncer.log")
    assert any("no more connections allowed (max_client_conn)" in l
               for l in parsed.error_lines)
    assert any("Worker threads failed to initialize" in l for l in parsed.error_lines)


def test_parse_summary_absent() -> None:
    assert parse_summary("Threads started!\n[ 1s ] nothing useful\n") is None
