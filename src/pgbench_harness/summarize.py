"""Turn raw sysbench logs into the tidy artifacts in parsed/.

* ``parsed/samples.csv`` — per-second samples (full, including warm-up).
* ``parsed/summary.json`` — per (rep, threads) steady-state aggregates; this
  file is the contract that ``compare`` consumes.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.manifest import STATUS_OK, Manifest
from pgbench_harness.parser import ParsedLog, parse_log_file, percentile_from_histogram, trim_warmup
from pgbench_harness.spec import Spec
from pgbench_harness.util import atomic_write_json, atomic_write_text

SAMPLE_COLUMNS = [
    "run_id", "rep", "threads", "t_offset", "tps", "qps", "r", "w", "o",
    "lat_p99", "err_s", "reconn_s",
]


def summarize_level(
    parsed: ParsedLog, spec: Spec, percentiles: tuple[int, ...]
) -> dict[str, Any]:
    """Compute steady-state aggregates for one level.

    Aggregates use only samples after ``sweep.warmup_s``. Percentiles come
    from the histogram (interpolated) when available; otherwise only the
    declared 99th percentile from the summary block is reported.
    """
    assert spec.sweep is not None
    steady = trim_warmup(parsed.samples, spec.sweep.warmup_s)
    out: dict[str, Any] = {
        "qps_avg": round(statistics.fmean(s.qps for s in steady), 2) if steady else None,
        "tps_avg": round(statistics.fmean(s.tps for s in steady), 2) if steady else None,
        "errors": round(sum(s.err_s for s in steady), 2) if steady else None,
        "reconnects": round(sum(s.reconn_s for s in steady), 2) if steady else None,
        "duration_s": spec.sweep.duration_s,
        "steady_state_window": [spec.sweep.warmup_s, spec.sweep.duration_s],
        "samples_total": len(parsed.samples),
        "samples_steady": len(steady),
    }
    for p in percentiles:
        val: Optional[float] = percentile_from_histogram(parsed.histogram, p)
        if val is None and parsed.summary and parsed.summary.lat_declared_pct == p:
            val = parsed.summary.lat_declared_value
        out[f"lat_p{p}"] = round(val, 2) if val is not None else None
    if parsed.summary:
        out["lat_min"] = parsed.summary.lat_min
        out["lat_avg"] = parsed.summary.lat_avg
        out["lat_max"] = parsed.summary.lat_max
        out["transactions"] = parsed.summary.transactions
        out["queries"] = parsed.summary.queries
    else:
        out.update({"lat_min": None, "lat_avg": None, "lat_max": None,
                    "transactions": None, "queries": None})
    return out


def _samples_rows(run_id: str, rep: int, threads: int, parsed: ParsedLog) -> list[list[Any]]:
    return [
        [run_id, rep, threads, s.t_offset, s.tps, s.qps, s.r, s.w, s.o,
         s.lat_ms, s.err_s, s.reconn_s]
        for s in parsed.samples
    ]


BLOCK_BYTES = 8192      # PostgreSQL default block size; pg_stat_io counts 8 KB ops
MB = 1024 * 1024


def io_delta(path: Path, duration_s: int) -> Optional[dict[str, Any]]:
    """Derive engine-side I/O rates from a level's pre/post I/O snapshots.

    Reads ``raw/<level>_iostats.json`` ({pre, post} of pg_stat_io /
    pg_stat_database / pg_stat_wal), subtracts, and converts to per-second
    rates over the **whole level** (the snapshots bracket the entire run,
    including warm-up). 8 KB blocks are assumed for byte conversions. Returns
    ``None`` when nothing usable was captured.
    """
    if duration_s <= 0 or not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pre, post = doc.get("pre") or {}, doc.get("post") or {}

    def delta(section: str, key: str) -> Optional[float]:
        a, b = pre.get(section) or {}, post.get(section) or {}
        if a.get(key) is not None and b.get(key) is not None:
            return max(0.0, float(b[key]) - float(a[key]))
        return None

    out: dict[str, Any] = {}
    reads, writes = delta("io", "reads"), delta("io", "writes")
    if reads is not None:
        out["read_ops_s"] = round(reads / duration_s, 1)
        out["read_mb"] = round(reads * BLOCK_BYTES / MB, 1)
    if writes is not None:
        out["write_ops_s"] = round(writes / duration_s, 1)
        out["write_mb"] = round(writes * BLOCK_BYTES / MB, 1)
    for src, name in (("extends", "extend_ops_s"), ("fsyncs", "fsync_s")):
        v = delta("io", src)
        if v is not None:
            out[name] = round(v / duration_s, 1)
    blks_read, blks_hit = delta("db", "blks_read"), delta("db", "blks_hit")
    if blks_read is not None and blks_hit is not None and (blks_read + blks_hit) > 0:
        out["cache_hit_pct"] = round(blks_hit / (blks_hit + blks_read) * 100, 2)
    elif reads is not None:
        hits = delta("io", "hits")
        if hits is not None and (hits + reads) > 0:
            out["cache_hit_pct"] = round(hits / (hits + reads) * 100, 2)
    wal_bytes, wal_records = delta("wal", "wal_bytes"), delta("wal", "wal_records")
    if wal_bytes is not None:
        out["wal_mb"] = round(wal_bytes / MB, 1)
        out["wal_mb_s"] = round(wal_bytes / MB / duration_s, 2)
    if wal_records is not None:
        out["wal_records_s"] = round(wal_records / duration_s, 1)
    return out or None


def write_parsed(run_dir: Path, spec: Spec, manifest: Manifest) -> dict[str, Any]:
    """(Re)build parsed/samples.csv and parsed/summary.json from raw logs.

    Re-parsing from raw is the source of truth, so reports can be regenerated
    after parser improvements without re-running benchmarks.
    """
    assert spec.sweep is not None
    parsed_dir = run_dir / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    pcts = spec.report.percentiles
    rows: list[list[Any]] = []
    levels_out: list[dict[str, Any]] = []
    for lvl in manifest.levels:
        entry: dict[str, Any] = {
            "rep": lvl.rep, "threads": lvl.threads, "status": lvl.status,
            "error_excerpt": lvl.error_excerpt or None,
        }
        log_path = run_dir / lvl.raw_log if lvl.raw_log else None
        if log_path is not None and log_path.exists():
            parsed = parse_log_file(log_path)
            rows.extend(_samples_rows(manifest.run_id, lvl.rep, lvl.threads, parsed))
            if lvl.status == STATUS_OK:
                entry.update(summarize_level(parsed, spec, pcts))
                iostats = io_delta(
                    run_dir / "raw" / f"{lvl.key}_iostats.json", spec.sweep.duration_s)
                if iostats is not None:
                    entry["io"] = iostats
            elif parsed.error_lines and not entry["error_excerpt"]:
                entry["error_excerpt"] = "\n".join(parsed.error_lines[:3])
        levels_out.append(entry)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(SAMPLE_COLUMNS)
    writer.writerows(rows)
    atomic_write_text(parsed_dir / "samples.csv", buf.getvalue())
    summary = {
        "run_id": manifest.run_id,
        "label": manifest.label,
        "edition": manifest.edition,
        "tshirt_size": manifest.tshirt_size,
        "status": manifest.status,
        "workload": dict(spec.raw.get("workload", {})),
        "sweep": dict(spec.raw.get("sweep", {})),
        "percentiles": list(pcts),
        "levels": levels_out,
    }
    atomic_write_json(parsed_dir / "summary.json", summary)
    return summary
