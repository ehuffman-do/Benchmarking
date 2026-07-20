"""Evidence bundle assembly: evidence.json + per-workload SQL descriptions.

The bundle must be self-interpreting — uploadable to an independent reviewer
with zero additional context — so the workload descriptions are generated
from the workload definitions (what each transaction actually executes),
never hand-written per run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.spec import Spec
from pgbench_harness.util import atomic_write_json, read_json, utc_now_iso

# Exactly what one transaction executes, per stock workload. Sources: sysbench
# 1.0.20 oltp_common.lua defaults and pgbench's builtin scripts.
WORKLOAD_SQL = {
    "oltp_point_select": {
        "driver": "sysbench", "label": "Point selects (pure random PK reads)",
        "per_txn": ["10x SELECT c FROM sbtestN WHERE id = ? (random PK)"],
        "profile": "read-only; with a dataset far beyond RAM every miss is a "
                   "synchronous 8 KB random read from the volume",
    },
    "oltp_read_only": {
        "driver": "sysbench", "label": "Read-only mix (point + range reads)",
        "per_txn": [
            "10x SELECT c FROM sbtestN WHERE id = ?",
            "1x SELECT c FROM sbtestN WHERE id BETWEEN ? AND ?+99",
            "1x SELECT SUM(k) FROM sbtestN WHERE id BETWEEN ? AND ?+99",
            "1x SELECT c FROM sbtestN WHERE id BETWEEN ? AND ?+99 ORDER BY c",
            "1x SELECT DISTINCT c FROM sbtestN WHERE id BETWEEN ? AND ?+99 ORDER BY c",
        ],
        "profile": "read-only; range scans add sequentialish reads on top of "
                   "random point reads",
    },
    "oltp_read_write": {
        "driver": "sysbench", "label": "Classic OLTP read/write mix",
        "per_txn": [
            "the full oltp_read_only read set (14 queries), plus:",
            "1x UPDATE sbtestN SET k=k+1 WHERE id = ? (index column)",
            "1x UPDATE sbtestN SET c=? WHERE id = ? (non-index column)",
            "1x DELETE FROM sbtestN WHERE id = ?",
            "1x INSERT INTO sbtestN (id,k,c,pad) VALUES (...) (same id)",
        ],
        "profile": "mixed: random reads + heap/index writes + WAL flushes at "
                   "commit",
    },
    "oltp_write_only": {
        "driver": "sysbench", "label": "Write-only (WAL/fsync-bound)",
        "per_txn": [
            "1x UPDATE sbtestN SET k=k+1 WHERE id = ?",
            "1x UPDATE sbtestN SET c=? WHERE id = ?",
            "1x DELETE FROM sbtestN WHERE id = ?",
            "1x INSERT INTO sbtestN (id,k,c,pad) VALUES (...)",
        ],
        "profile": "write-only; throughput is bounded by WAL fsync latency and "
                   "checkpoint write pressure on the volume",
    },
    "pgbench_tpcb": {
        "driver": "pgbench", "label": "pgbench TPC-B (sort of)",
        "per_txn": [
            "UPDATE pgbench_accounts SET abalance = abalance + ? WHERE aid = ?",
            "SELECT abalance FROM pgbench_accounts WHERE aid = ?",
            "UPDATE pgbench_tellers SET tbalance = tbalance + ? WHERE tid = ?",
            "UPDATE pgbench_branches SET bbalance = bbalance + ? WHERE bid = ?",
            "INSERT INTO pgbench_history (tid,bid,aid,delta,mtime) VALUES (...)",
        ],
        "profile": "independent second driver; write-heavy with hot-row "
                   "contention on branches/tellers",
    },
    "pgbench_select": {
        "driver": "pgbench", "label": "pgbench SELECT-only",
        "per_txn": ["SELECT abalance FROM pgbench_accounts WHERE aid = ? "
                    "(random PK)"],
        "profile": "independent second driver; pure random PK reads",
    },
    "fileio": {
        "driver": "sysbench fileio", "label": "Direct device probe (no SQL)",
        "per_txn": ["random 16 KB read/write IO against test files on the "
                    "pgdata volume, async queue — bypasses Postgres entirely"],
        "profile": "device ground truth: deep async queues expose the hard "
                   "throttle (and any burst window) that Postgres's "
                   "synchronous read path cannot",
    },
    "tpcc": {
        "driver": "sysbench-tpcc", "label": "TPCC (Percona-Lab lua)",
        "per_txn": ["full TPC-C transaction mix (new-order, payment, "
                    "order-status, delivery, stock-level)"],
        "profile": "mixed OLTP with large working set",
    },
}


def workload_descriptions(spec: Spec) -> list[dict[str, Any]]:
    """The plain-language what-runs list for THIS run, from its definitions."""
    segs: list[str] = []
    if spec.suite is not None:
        segs = list(spec.suite.workloads)
        if spec.suite.pgbench:
            segs += ["pgbench_tpcb", "pgbench_select"]
    elif spec.device_probe is not None and spec.sweep is None and spec.soak is None:
        segs = ["fileio"]
    else:
        w = spec.workload
        from pgbench_harness.spec import IO_STRESS_MIXES
        segs = [IO_STRESS_MIXES[w.mix] if w.type == "io_stress" else w.type]
    out = []
    for seg in segs:
        d = dict(WORKLOAD_SQL.get(seg, {"driver": "?", "label": seg,
                                        "per_txn": [], "profile": ""}))
        d["workload"] = seg
        if spec.workload.rand_type and d.get("driver") == "sysbench":
            d["profile"] += f"; key distribution: --rand-type={spec.workload.rand_type}"
        out.append(d)
    return out


def _load_json(path: Path) -> Optional[dict]:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def build_caveats(spec: Spec, verdict: Optional[dict],
                  storage: Optional[dict]) -> list[str]:
    """Honest-limitations section, auto-populated from what the run really was."""
    caveats = []
    w = spec.workload
    if w.type == "io_stress" and w.dataset_gb:
        caveats.append(f"Dataset sized at {w.dataset_gb:g} GiB via dataset_gb; "
                       "verify env/prepare_stats.json for the actual on-disk "
                       "size — reads only hit the device once the working set "
                       "defeats shared_buffers + page cache.")
    else:
        caveats.append("Dataset size was not driven by dataset_gb; if it fits "
                       "in cache, read results measure memory, not the volume "
                       "(the storage team's own report had this weakness).")
    if spec.cluster is None:
        caveats.append("Run was not cluster-aware (no cluster: section): no "
                       "storage identity or device-level IOPS were captured — "
                       "TPS alone cannot establish the IOPS ceiling.")
    if verdict and verdict.get("finding") == "inconclusive":
        caveats.append("The verdict is INCONCLUSIVE — see the verdict detail "
                       "for what stopped scaling before redesigning the run.")
    if storage and storage.get("warnings"):
        caveats.append("Storage identity capture degraded: "
                       + "; ".join(storage["warnings"]))
    caveats.append("Device counters come from /proc/diskstats on the node "
                   "(1s cadence): they include ALL IO to the pgdata volume — "
                   "WAL, checkpoints, autovacuum, backups — which is exactly "
                   "why they can diverge from pg_stat_io's logical view.")
    caveats.append("pgbench latency figures are transaction averages "
                   "(pgbench --progress reports no percentiles); sysbench "
                   "p95/p99 come from its histogram.")
    return caveats


def build_evidence(run_dir: Path, spec: Spec,
                   verdict: Optional[dict]) -> dict[str, Any]:
    """One machine-readable document with everything a reviewer needs."""
    doc: dict[str, Any] = {
        "generated_utc": utc_now_iso(),
        "run_id": run_dir.name,
        "purpose": "IOPS ceiling verification: can this cluster's pgdata "
                   "volume exceed the standard block-storage limit?",
        "limits": {
            "standard_iops": spec.limits.standard_iops,
            "burst_iops": spec.limits.burst_iops,
            "target_iops": spec.limits.target_iops,
            "tolerance_pct": spec.limits.tolerance_pct,
        },
        "verdict": verdict,
        "workloads": workload_descriptions(spec),
        "storage_identity": _load_json(run_dir / "env" / "storage_identity.json"),
        "manifest": _load_json(run_dir / "manifest.json"),
        "summary": _load_json(run_dir / "parsed" / "summary.json"),
        "soak_summary": _load_json(run_dir / "parsed" / "soak_summary.json"),
        "fileio": _load_json(run_dir / "parsed" / "fileio_summary.json"),
        "files": {
            "device_series_csv": "parsed/device_io.csv",
            "sql_samples_csv": "parsed/samples.csv",
            "engine_series_csv": "parsed/pg_timeseries.csv",
            "raw_diskstats": "raw/diskstats.log",
            "events": "events.jsonl",
        },
    }
    doc["caveats"] = build_caveats(spec, verdict, doc["storage_identity"])
    atomic_write_json(run_dir / "evidence.json", doc)
    return doc
