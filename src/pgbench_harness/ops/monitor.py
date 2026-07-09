"""Continuous cluster telemetry (pg_bench_monitor3 port) — lower priority.

A per-target sampler loop capturing to CSVs + a live status.json for the SSE
panel. Field lessons:
* the leader is RE-DETECTED every cycle, so the monitor survives failovers;
* queries are SPLIT — the old consolidated one-CTE health query returned
  empty on real clusters when a single column errored; here one failing
  collector leaves a blank cell, never a blank row.

The monitor runs until stopped (worker cancel = SIGTERM) or until
``max_duration_s`` elapses. It occupies the queue's monitor lane, not a
benchmark concurrency slot.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.ops import patroni
from pgbench_harness.ops.crconfig import resolve_leader
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.oprun import EXIT_FAILED, EXIT_OK, OpsRun
from pgbench_harness.ops.opspec import OpsSpec

WAL_SQL = "SELECT wal_bytes FROM pg_stat_wal"
CKPT_SQL_17 = ("SELECT num_timed, num_requested FROM pg_stat_checkpointer")
CKPT_SQL_OLD = ("SELECT checkpoints_timed, checkpoints_req FROM pg_stat_bgwriter")
ARCHIVER_SQL = "SELECT archived_count, failed_count FROM pg_stat_archiver"
REPL_SQL = ("SELECT application_name, state, "
            "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn), "
            "coalesce(extract(epoch from replay_lag), 0) "
            "FROM pg_stat_replication")
QUEUE_CMD = ["bash", "-c", "ls /pgdata/pg*/pg_wal/archive_status/ 2>/dev/null "
                           "| grep -c '\\.ready$' || true"]

MONITOR_HEADER = ("epoch_s,leader,timeline,wal_bytes,ckpt_timed,ckpt_req,"
                  "archived_count,archive_failed,archive_queue,ready,total")
REPL_HEADER = "epoch_s,replica,state,lag_bytes,lag_s"
DISK_HEADER = "epoch_s,pod,pgdata_used,pgdata_use_pct"


def _append(path: Path, header: str, row: str) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write(header + "\n")
        fh.write(row + "\n")


def _one(kube: Kube, pod: str, sql: str) -> list[str]:
    """One split query -> cells; [] on any failure (blank cell, not blank row)."""
    try:
        res = kube.psql(pod, sql, csv_sep=",")
        if res.ok and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].split(",")
    except KubeError:
        pass
    return []


def run_monitor(spec: OpsSpec, results_dir: Path) -> int:
    t = spec.target
    params = spec.params
    interval_s = float(params.get("interval_s", 60))
    max_duration_s = float(params.get("max_duration_s", 0))
    run = OpsRun(results_dir, "monitor", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=params)
    log = run.get_logger()
    kube = Kube(context=t.context, namespace=t.namespace)
    parsed = run.run_dir / "parsed"
    started = time.monotonic()
    cycles = 0
    ckpt_sql = CKPT_SQL_17
    run.event("monitor", "telemetry monitor started",
              f"interval {interval_s:.0f}s")
    try:
        while True:
            cycle_start = time.monotonic()
            ts = str(int(time.time()))
            # Leader re-detected EVERY cycle: the monitor survives failovers.
            leader, timeline, members, ready, total = "", None, [], 0, 0
            instances: list[str] = []
            try:
                instances, leader, view = resolve_leader(kube, t.cr_name)
                timeline = view.timeline
                members = view.to_dict()["members"]
                ready = sum(1 for m in view.members
                            if m.state.lower() in ("running", "streaming"))
                total = len(view.members)
            except KubeError as exc:
                log.warning("cycle %d: no leader view: %s", cycles, str(exc)[:200])

            row: dict[str, str] = {"wal": "", "ckpt_t": "", "ckpt_r": "",
                                   "arch": "", "arch_f": "", "queue": ""}
            if leader:
                cells = _one(kube, leader, WAL_SQL)
                row["wal"] = cells[0] if cells else ""
                cells = _one(kube, leader, ckpt_sql)
                if not cells and ckpt_sql == CKPT_SQL_17:
                    ckpt_sql = CKPT_SQL_OLD          # pre-17 fallback, once
                    cells = _one(kube, leader, ckpt_sql)
                if len(cells) >= 2:
                    row["ckpt_t"], row["ckpt_r"] = cells[0], cells[1]
                cells = _one(kube, leader, ARCHIVER_SQL)
                if len(cells) >= 2:
                    row["arch"], row["arch_f"] = cells[0], cells[1]
                try:
                    res = kube.exec(leader, "database", QUEUE_CMD, timeout_s=15)
                    val = res.stdout.strip().splitlines()[-1].strip() \
                        if res.stdout.strip() else ""
                    row["queue"] = val if val.isdigit() else ""
                except KubeError:
                    pass
                try:
                    res = kube.psql(leader, REPL_SQL, csv_sep=",")
                    repl_lines = res.stdout.strip().splitlines() if res.ok else []
                except KubeError:
                    repl_lines = []
                for ln in repl_lines:
                    cells = ln.split(",")
                    if len(cells) >= 4:
                        _append(parsed / "replication.csv", REPL_HEADER,
                                f"{ts},{cells[0]},{cells[1]},{cells[2]},{cells[3]}")
                for pod in instances:
                    try:
                        res = kube.exec(pod, "database",
                                        ["df", "-P", "/pgdata"], timeout_s=15)
                        # df -P guarantees one data line with fixed columns:
                        # Filesystem 1K-blocks Used Available Capacity Mounted.
                        # Record Used + Use% (cols 2 and 4) — NOT col 0, which is
                        # the device name (the header says pgdata_used).
                        used, pct = "", ""
                        if res.ok and res.stdout.strip():
                            cols = res.stdout.strip().splitlines()[-1].split()
                            if len(cols) >= 5:
                                used, pct = cols[2], cols[4]
                        if used:
                            _append(parsed / "disk.csv", DISK_HEADER,
                                    f"{ts},{pod},{used},{pct}")
                    except KubeError:
                        continue
            _append(parsed / "monitor.csv", MONITOR_HEADER,
                    f"{ts},{leader},{timeline if timeline is not None else ''},"
                    f"{row['wal']},{row['ckpt_t']},{row['ckpt_r']},"
                    f"{row['arch']},{row['arch_f']},{row['queue']},{ready},{total}")
            run.status_update(leader=leader, timeline=timeline,
                              ready=f"{ready}/{total}", members=members,
                              archive_queue=row["queue"], cycles=cycles + 1)
            cycles += 1
            if max_duration_s and time.monotonic() - started >= max_duration_s:
                break
            delay = interval_s - (time.monotonic() - cycle_start)
            if delay > 0:
                time.sleep(delay)
        run.event("monitor", "telemetry monitor finished",
                  f"{cycles} cycles")
        run.finalize("complete", headline={"cycles": cycles})
        return EXIT_OK
    except KeyboardInterrupt:
        run.finalize("canceled", headline={"cycles": cycles})
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        log.error("monitor failed: %s", exc)
        run.finalize("failed", error=str(exc)[:500])
        return EXIT_FAILED
