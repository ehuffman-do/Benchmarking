"""Parsing of sysbench output: per-second interval lines, the final summary
block, and the optional latency histogram.

Notes on latency semantics (also stated in the report):

* The latency value on each per-second interval line reflects **only** the
  percentile passed via ``--percentile`` (the harness always passes 99).
* All other percentiles (``report.percentiles``) are derived from the
  ``--histogram`` table by linear interpolation over cumulative bucket counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Colons after each label are optional: some sysbench builds (notably
# sysbench-tpcc) print "err/s 0.00" with NO colon while every other field keeps
# one. Tolerating ":?" everywhere makes the parser robust across builds.
INTERVAL_RE = re.compile(
    r"\[\s*(?P<t>\d+)s\s*\]\s+thds:?\s*(?P<thds>\d+)"
    r"\s+tps:?\s*(?P<tps>[\d.]+)"
    r"\s+qps:?\s*(?P<qps>[\d.]+)"
    r"\s+\(r/w/o:?\s*(?P<r>[\d.]+)/(?P<w>[\d.]+)/(?P<o>[\d.]+)\)"
    r"\s+lat\s+\(ms,(?P<pct>\d+)%\):?\s*(?P<lat>[\d.]+)"
    r"\s+err/s:?\s*(?P<err>[\d.]+)"
    r"\s+reconn/s:?\s*(?P<reconn>[\d.]+)"
)

_SUMMARY_NUM = r"([\d.]+)"
TRANSACTIONS_RE = re.compile(rf"transactions:\s+(\d+)\s+\({_SUMMARY_NUM} per sec\.\)")
QUERIES_RE = re.compile(rf"queries:\s+(\d+)\s+\({_SUMMARY_NUM} per sec\.\)")
IGNORED_ERRORS_RE = re.compile(rf"ignored errors:\s+(\d+)\s+\({_SUMMARY_NUM} per sec\.\)")
RECONNECTS_RE = re.compile(rf"reconnects:\s+(\d+)\s+\({_SUMMARY_NUM} per sec\.\)")
TOTAL_TIME_RE = re.compile(r"total time:\s+([\d.]+)s")
LAT_MIN_RE = re.compile(rf"min:\s+{_SUMMARY_NUM}")
LAT_AVG_RE = re.compile(rf"avg:\s+{_SUMMARY_NUM}")
LAT_MAX_RE = re.compile(rf"max:\s+{_SUMMARY_NUM}")
LAT_PCT_RE = re.compile(rf"(\d+)th percentile:\s+{_SUMMARY_NUM}")
HISTOGRAM_HEADER_RE = re.compile(r"Latency histogram \(values are in milliseconds\)")
HISTOGRAM_BUCKET_RE = re.compile(r"^\s*([\d.]+)\s+\|[* ]*\s*(\d+)\s*$")
ERROR_LINE_RE = re.compile(r"FATAL|PANIC|^ERROR|\bError\b|failed", re.IGNORECASE)


@dataclass(frozen=True)
class IntervalSample:
    """One per-second sysbench report line."""

    t_offset: int
    threads: int
    tps: float
    qps: float
    r: float
    w: float
    o: float
    lat_pct: int
    lat_ms: float
    err_s: float
    reconn_s: float


@dataclass(frozen=True)
class Summary:
    """The final sysbench statistics block."""

    transactions: int
    transactions_per_s: float
    queries: int
    queries_per_s: float
    ignored_errors: int
    reconnects: int
    total_time_s: float
    lat_min: float
    lat_avg: float
    lat_max: float
    lat_declared_pct: Optional[int]
    lat_declared_value: Optional[float]


@dataclass
class ParsedLog:
    """Everything extracted from one raw sysbench log."""

    samples: list[IntervalSample] = field(default_factory=list)
    summary: Optional[Summary] = None
    histogram: list[tuple[float, int]] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)


def parse_interval_line(line: str) -> Optional[IntervalSample]:
    """Parse a single `[ Ns ] thds: ...` line, or return None if it isn't one."""
    m = INTERVAL_RE.search(line)
    if m is None:
        return None
    return IntervalSample(
        t_offset=int(m["t"]),
        threads=int(m["thds"]),
        tps=float(m["tps"]),
        qps=float(m["qps"]),
        r=float(m["r"]),
        w=float(m["w"]),
        o=float(m["o"]),
        lat_pct=int(m["pct"]),
        lat_ms=float(m["lat"]),
        err_s=float(m["err"]),
        reconn_s=float(m["reconn"]),
    )


def _search_float(pattern: re.Pattern[str], text: str) -> Optional[float]:
    m = pattern.search(text)
    return float(m.group(1)) if m else None


def parse_summary(text: str) -> Optional[Summary]:
    """Parse the final statistics block; returns None if no block is present."""
    tx = TRANSACTIONS_RE.search(text)
    if tx is None:
        return None
    q = QUERIES_RE.search(text)
    ign = IGNORED_ERRORS_RE.search(text)
    rec = RECONNECTS_RE.search(text)
    # The "Latency (ms):" block: min/avg/max appear after the histogram (if
    # any), so search the tail of the text starting at "Latency (ms):".
    lat_at = text.find("Latency (ms):")
    lat_text = text[lat_at:] if lat_at >= 0 else text
    pct = LAT_PCT_RE.search(lat_text)
    return Summary(
        transactions=int(tx.group(1)),
        transactions_per_s=float(tx.group(2)),
        queries=int(q.group(1)) if q else 0,
        queries_per_s=float(q.group(2)) if q else 0.0,
        ignored_errors=int(ign.group(1)) if ign else 0,
        reconnects=int(rec.group(1)) if rec else 0,
        total_time_s=_search_float(TOTAL_TIME_RE, text) or 0.0,
        lat_min=_search_float(LAT_MIN_RE, lat_text) or 0.0,
        lat_avg=_search_float(LAT_AVG_RE, lat_text) or 0.0,
        lat_max=_search_float(LAT_MAX_RE, lat_text) or 0.0,
        lat_declared_pct=int(pct.group(1)) if pct else None,
        lat_declared_value=float(pct.group(2)) if pct else None,
    )


def parse_histogram(text: str) -> list[tuple[float, int]]:
    """Parse the `--histogram` table into (bucket_upper_ms, count) pairs.

    Only lines after the histogram header are considered, so stray numbers
    elsewhere in the log can't be misread as buckets.
    """
    m = HISTOGRAM_HEADER_RE.search(text)
    if m is None:
        return []
    buckets: list[tuple[float, int]] = []
    for line in text[m.end():].splitlines():
        bm = HISTOGRAM_BUCKET_RE.match(line)
        if bm:
            buckets.append((float(bm.group(1)), int(bm.group(2))))
        elif buckets and line.strip() and "distribution" not in line:
            break  # histogram table ended
    return buckets


def percentile_from_histogram(buckets: list[tuple[float, int]], pct: float) -> Optional[float]:
    """Compute a latency percentile from histogram buckets via linear interpolation.

    sysbench buckets are log-spaced upper bounds; we interpolate linearly on
    the cumulative count between adjacent bucket values. Returns None when the
    histogram is empty.
    """
    if not buckets:
        return None
    total = sum(c for _, c in buckets)
    if total <= 0:
        return None
    target = pct / 100.0 * total
    cum = 0.0
    prev_value = 0.0
    for value, count in buckets:
        if cum + count >= target:
            if count == 0:
                return value
            frac = (target - cum) / count
            return prev_value + (value - prev_value) * frac
        cum += count
        prev_value = value
    return buckets[-1][0]


def parse_log_text(text: str) -> ParsedLog:
    """Parse a complete sysbench log (interval lines, summary, histogram, errors)."""
    parsed = ParsedLog()
    for line in text.splitlines():
        sample = parse_interval_line(line)
        if sample is not None:
            parsed.samples.append(sample)
        elif ERROR_LINE_RE.search(line) and line.strip():
            parsed.error_lines.append(line.strip())
    parsed.summary = parse_summary(text)
    parsed.histogram = parse_histogram(text)
    return parsed


def parse_log_file(path: Path) -> ParsedLog:
    """Parse a raw sysbench log file."""
    return parse_log_text(path.read_text(encoding="utf-8", errors="replace"))


def trim_warmup(samples: list[IntervalSample], warmup_s: int) -> list[IntervalSample]:
    """Drop samples inside the warm-up window (t_offset <= warmup_s).

    All aggregates are computed over the remaining steady-state window only.
    """
    return [s for s in samples if s.t_offset > warmup_s]
