"""Turn raw sysbench logs into the tidy artifacts in parsed/.

* ``parsed/samples.csv`` — per-second samples (full, including warm-up).
* ``parsed/summary.json`` — per (rep, threads) steady-state aggregates; this
  file is the contract that ``compare`` consumes.
"""

from __future__ import annotations

import csv
import io
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


def write_parsed(run_dir: Path, spec: Spec, manifest: Manifest) -> dict[str, Any]:
    """(Re)build parsed/samples.csv and parsed/summary.json from raw logs.

    Re-parsing from raw is the source of truth, so reports can be regenerated
    after parser improvements without re-running benchmarks.
    """
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
