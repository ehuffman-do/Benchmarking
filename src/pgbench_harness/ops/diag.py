"""Diagnostics workbench — curated, read-only checks with live results.

``ops diag`` runs a catalog of field-standard PostgreSQL / Patroni /
pgBackRest / Kubernetes diagnostics against the target cluster and streams
results as parsed CSVs into the op run dir, which the cockpit renders live
(tables + charts). Nothing here mutates the cluster: every check is a SELECT,
a ``patronictl``/``pgbackrest info`` read, or a ``kubectl get`` — so the route
is operator-level with no typed confirmation.

Checks marked ``watch`` are single-row samplers: with ``params.watch_s > 0``
they re-run every ``interval_s`` and append rows, turning the cockpit into a
live chart (connection saturation over time, replication lag over time, ...).

Each SQL carries a ``/*diag:<key>*/`` marker comment: harmless server-side,
and it lets the test shim answer deterministically without pattern-guessing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.ops import patroni
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.oprun import EXIT_FAILED, EXIT_OK, EXIT_WARNING, OpsRun
from pgbench_harness.ops.opspec import OpsSpec

# psql column separator for CSV checks. Comma would collide with query text
# and lock lists; the unit separator can't appear in identifiers or numbers.
SEP = "\x1f"


@dataclass(frozen=True)
class DiagCheck:
    key: str
    title: str
    description: str
    category: str                  # sessions | replication | maintenance | storage | backups | kubernetes
    kind: str                      # sql | patroni | pgbackrest | pods | events | disk
    columns: tuple[str, ...] = ()
    sql: str = ""
    watch: bool = False            # single-row sampler, safe to re-run on an interval
    chart: str = ""                # cockpit hint: column to chart in watch mode


DIAG_CATALOG: tuple[DiagCheck, ...] = (
    DiagCheck(
        "connections", "Connection saturation",
        "Backends by state vs max_connections — the first thing to look at when "
        "apps report 'too many clients' or stalls.",
        "sessions", "sql",
        ("total", "active", "idle", "idle_in_tx", "waiting", "max_connections", "pct_used"),
        "/*diag:connections*/ SELECT count(*), "
        "count(*) FILTER (WHERE state='active'), "
        "count(*) FILTER (WHERE state='idle'), "
        "count(*) FILTER (WHERE state LIKE 'idle in%'), "
        "count(*) FILTER (WHERE wait_event_type IS NOT NULL AND state='active'), "
        "current_setting('max_connections')::int, "
        "round(100.0*count(*)/current_setting('max_connections')::int, 1) "
        "FROM pg_stat_activity",
        watch=True, chart="pct_used"),
    DiagCheck(
        "long_running", "Long-running & idle-in-transaction sessions",
        "Sessions ordered by transaction age. Idle-in-transaction holds locks and "
        "blocks vacuum; long transactions bloat tables.",
        "sessions", "sql",
        ("pid", "usename", "state", "wait_event", "xact_age_s", "query"),
        "/*diag:long_running*/ SELECT pid, usename, state, "
        "coalesce(wait_event_type||':'||wait_event,''), "
        "coalesce(round(extract(epoch FROM now()-xact_start))::text,''), "
        "left(regexp_replace(query, E'[\\n\\r\\x1f]+', ' ', 'g'), 120) "
        "FROM pg_stat_activity WHERE state <> 'idle' AND pid <> pg_backend_pid() "
        "ORDER BY xact_start NULLS LAST LIMIT 20"),
    DiagCheck(
        "blocked", "Blocked queries (lock waits)",
        "Who is waiting on whom: each blocked backend with the pids blocking it.",
        "sessions", "sql",
        ("blocked_pid", "blocked_by", "wait_s", "blocked_query"),
        "/*diag:blocked*/ SELECT a.pid, array_to_string(pg_blocking_pids(a.pid),' '), "
        "coalesce(round(extract(epoch FROM now()-a.state_change))::text,''), "
        "left(regexp_replace(a.query, E'[\\n\\r\\x1f]+', ' ', 'g'), 120) "
        "FROM pg_stat_activity a WHERE cardinality(pg_blocking_pids(a.pid)) > 0"),
    DiagCheck(
        "replication", "Replication status & lag",
        "Streaming replicas as the leader sees them: state and byte/time lag per "
        "standby. Lag that only grows means a replica stopped applying WAL.",
        "replication", "sql",
        ("client", "state", "sent_lag_bytes", "replay_lag_bytes", "replay_lag_s"),
        "/*diag:replication*/ SELECT coalesce(application_name, client_addr::text), state, "
        "pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn), "
        "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn), "
        "coalesce(round(extract(epoch FROM replay_lag),1)::text,'') "
        "FROM pg_stat_replication",
        watch=True, chart="replay_lag_bytes"),
    DiagCheck(
        "slots", "Replication slots & retained WAL",
        "Every slot with the WAL it pins. An INACTIVE slot retains WAL forever and "
        "will fill the disk — the classic silent K8s Postgres killer.",
        "replication", "sql",
        ("slot_name", "type", "active", "retained_wal_bytes"),
        "/*diag:slots*/ SELECT slot_name, slot_type, active::text, "
        "coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::text,'') "
        "FROM pg_replication_slots"),
    DiagCheck(
        "wraparound", "Transaction ID wraparound distance",
        "age(datfrozenxid) per database vs the 2^31 hard limit. Past "
        "autovacuum_freeze_max_age Postgres forces aggressive vacuums; near the "
        "limit it stops accepting writes.",
        "maintenance", "sql",
        ("datname", "xid_age", "pct_toward_wraparound"),
        "/*diag:wraparound*/ SELECT datname, age(datfrozenxid), "
        "round(100.0*age(datfrozenxid)/2147483647, 2) "
        "FROM pg_database ORDER BY 2 DESC"),
    DiagCheck(
        "cache_hit", "Buffer cache hit ratio",
        "Per-database heap cache hit ratio. Below ~0.99 on an OLTP workload means "
        "the working set no longer fits shared_buffers.",
        "maintenance", "sql",
        ("datname", "blks_read", "blks_hit", "hit_ratio"),
        "/*diag:cache_hit*/ SELECT datname, blks_read, blks_hit, "
        "round(blks_hit::numeric/nullif(blks_hit+blks_read,0), 4) "
        "FROM pg_stat_database WHERE datname NOT LIKE 'template%' AND datname IS NOT NULL",
        watch=True, chart="hit_ratio"),
    DiagCheck(
        "dead_tuples", "Dead tuples & autovacuum recency",
        "Tables with the most dead tuples and when autovacuum last visited. "
        "Starved autovacuum = bloat = slow scans.",
        "maintenance", "sql",
        ("relation", "n_live_tup", "n_dead_tup", "dead_pct", "last_autovacuum"),
        "/*diag:dead_tuples*/ SELECT schemaname||'.'||relname, n_live_tup, n_dead_tup, "
        "round(100.0*n_dead_tup/nullif(n_live_tup+n_dead_tup,0),1), "
        "coalesce(last_autovacuum::text,'never') "
        "FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 15"),
    DiagCheck(
        "table_sizes", "Largest relations",
        "Top relations by total size (heap + indexes + toast).",
        "storage", "sql",
        ("relation", "total_bytes", "total_pretty"),
        "/*diag:table_sizes*/ SELECT schemaname||'.'||relname, "
        "pg_total_relation_size(relid), "
        "pg_size_pretty(pg_total_relation_size(relid)) "
        "FROM pg_stat_user_tables ORDER BY 2 DESC LIMIT 15"),
    DiagCheck(
        "temp_files", "Temp file spill",
        "Queries spilling to disk (work_mem too small for the workload).",
        "storage", "sql",
        ("datname", "temp_files", "temp_bytes"),
        "/*diag:temp_files*/ SELECT datname, temp_files, temp_bytes "
        "FROM pg_stat_database WHERE datname IS NOT NULL AND temp_files > 0 "
        "ORDER BY temp_bytes DESC"),
    DiagCheck(
        "checkpoints", "Checkpoint pressure",
        "Requested vs timed checkpoints. A high requested share means "
        "max_wal_size is too small — checkpoints fire on WAL volume, not schedule.",
        "storage", "sql",
        ("timed", "requested", "requested_pct"),
        "/*diag:checkpoints*/ SELECT num_timed, num_requested, "
        "round(100.0*num_requested/nullif(num_timed+num_requested,0),1) "
        "FROM pg_stat_checkpointer",
        watch=True, chart="requested_pct"),
    DiagCheck(
        "wal_activity", "WAL generation",
        "Total WAL bytes generated (rate appears when watched over an interval).",
        "storage", "sql",
        ("wal_bytes", "wal_lsn"),
        "/*diag:wal_activity*/ SELECT wal_bytes, pg_current_wal_lsn()::text FROM pg_stat_wal",
        watch=True, chart="wal_bytes"),
    DiagCheck(
        "patroni_list", "Patroni cluster view",
        "patronictl list: member roles, states, timelines, replica lag — the HA "
        "source of truth.",
        "replication", "patroni",
        ("member", "role", "state", "timeline", "lag_mb"),
        watch=True),
    DiagCheck(
        "backup_info", "pgBackRest backups",
        "Every backup in the repo with type, timestamps, and sizes — verifies the "
        "backup chain and shows how stale the newest backup is.",
        "backups", "pgbackrest",
        ("label", "type", "start_utc", "stop_utc", "size_bytes")),
    DiagCheck(
        "pods", "Pod health & restarts",
        "Cluster pods with phase, readiness, restart counts, and node — restart "
        "loops and pending pods jump out here.",
        "kubernetes", "pods",
        ("pod", "phase", "ready", "restarts", "node")),
    DiagCheck(
        "events_warnings", "Recent Kubernetes warnings",
        "Warning-type events in the namespace (OOMKilled, FailedScheduling, "
        "probe failures...).",
        "kubernetes", "events",
        ("event",)),
    DiagCheck(
        "pvc_usage", "Data volume usage",
        "df on /pgdata inside every instance pod — WAL/bloat growth shows up "
        "here before it becomes an outage.",
        "kubernetes", "disk",
        ("pod", "used_kb", "avail_kb", "use_pct"),
        watch=True, chart="use_pct"),
)

CHECKS_BY_KEY = {c.key: c for c in DIAG_CATALOG}


def catalog_json() -> list[dict[str, Any]]:
    """The catalog as the API serves it (no SQL — the UI never builds queries)."""
    return [{"key": c.key, "title": c.title, "description": c.description,
             "category": c.category, "kind": c.kind, "columns": list(c.columns),
             "watch": c.watch, "chart": c.chart} for c in DIAG_CATALOG]


def _csv_escape(v: Any) -> str:
    s = str(v if v is not None else "")
    if any(ch in s for ch in ",\"\n\r"):
        s = '"' + s.replace('"', '""') + '"'
    return s


class _CheckWriter:
    """Appends rows to parsed/<key>.csv with an epoch_s lead column."""

    def __init__(self, run: OpsRun, check: DiagCheck) -> None:
        self.path = run.run_dir / "parsed" / f"{check.key}.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("epoch_s," + ",".join(check.columns) + "\n",
                                 encoding="utf-8")

    def rows(self, rows: list[list[Any]]) -> None:
        epoch = int(time.time())
        with self.path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(str(epoch) + "," + ",".join(_csv_escape(v) for v in row) + "\n")


def _run_sql_check(kube: Kube, leader: str, check: DiagCheck) -> list[list[str]]:
    res = kube.psql(leader, check.sql, csv_sep=SEP, timeout_s=30)
    if not res.ok:
        raise KubeError((res.stderr or res.stdout).strip()[:300] or "psql failed")
    out = []
    for ln in res.stdout.splitlines():
        if ln.strip():
            out.append(ln.split(SEP))
    return out


def _run_patroni_check(kube: Kube, pod: str) -> list[list[Any]]:
    view = patroni.fetch_view(kube, pod)
    return [[m.name, m.role, m.state, m.timeline if m.timeline is not None else "",
             m.lag_mb if m.lag_mb is not None else ""] for m in view.members]


def _run_pgbackrest_check(kube: Kube, pod: str, run: OpsRun) -> list[list[Any]]:
    res = kube.exec(pod, "database",
                    ["pgbackrest", "--stanza=db", "info", "--output=json"],
                    timeout_s=30)
    if not res.ok:
        raise KubeError((res.stderr or res.stdout).strip()[:300] or "pgbackrest failed")
    (run.run_dir / "raw").mkdir(exist_ok=True)
    (run.run_dir / "raw" / "pgbackrest_info.json").write_text(res.stdout,
                                                              encoding="utf-8")
    doc = json.loads(res.stdout)
    rows = []
    for stanza in doc if isinstance(doc, list) else []:
        for b in stanza.get("backup") or []:
            ts = b.get("timestamp") or {}
            info = b.get("info") or {}
            rows.append([b.get("label", ""), b.get("type", ""),
                         ts.get("start", ""), ts.get("stop", ""),
                         info.get("size", "")])
    return rows


def _run_pods_check(kube: Kube, cr_name: str) -> list[list[Any]]:
    from pgbench_harness.ops.discover import classify_pods
    items = kube.json(["get", "pods"]).get("items") or []
    rows = []
    for item in items:
        meta = item.get("metadata", {})
        name = meta.get("name", "")
        if cr_name and not name.startswith(cr_name):
            continue
        status = item.get("status", {})
        cs = status.get("containerStatuses") or []
        ready = sum(1 for c in cs if c.get("ready"))
        restarts = sum(int(c.get("restartCount") or 0) for c in cs)
        rows.append([name, status.get("phase", ""), f"{ready}/{len(cs)}",
                     restarts, (item.get("spec") or {}).get("nodeName", "")])
    return rows


def _run_events_check(kube: Kube) -> list[list[Any]]:
    res = kube.run(["get", "events", "--field-selector", "type=Warning",
                    "--sort-by=.lastTimestamp", "--no-headers"], timeout_s=20)
    if not res.ok:
        raise KubeError((res.stderr or res.stdout).strip()[:300])
    lines = [ln for ln in res.stdout.splitlines()
             if ln.strip() and "No resources found" not in ln]
    return [[ln] for ln in lines[-30:]]


def _run_disk_check(kube: Kube, instances: list[str]) -> list[list[Any]]:
    rows = []
    for pod in instances:
        res = kube.exec(pod, "database", ["df", "-P", "/pgdata"], timeout_s=15)
        if not res.ok:
            continue
        for ln in res.stdout.splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 5:
                rows.append([pod, parts[2], parts[3], parts[4].rstrip("%")])
    return rows


def run_diag(spec: OpsSpec, results_dir: Path) -> int:
    """Execute the selected checks (optionally in watch mode). Exit codes:
    0 all ok, 1 some checks errored (results partial), 3 nothing ran."""
    keys = spec.params.get("checks") or [c.key for c in DIAG_CATALOG]
    unknown = [k for k in keys if k not in CHECKS_BY_KEY]
    checks = [CHECKS_BY_KEY[k] for k in keys if k in CHECKS_BY_KEY]
    watch_s = float(spec.params.get("watch_s") or 0)
    interval_s = max(1.0, float(spec.params.get("interval_s") or 2))

    t = spec.target
    run = OpsRun(results_dir, "diag", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=dict(spec.params))
    kube = Kube(context=t.context, namespace=t.namespace)
    for k in unknown:
        run.event("diag", f"unknown check '{k}' skipped")

    try:
        from pgbench_harness.ops.crconfig import resolve_leader
        instances, leader, _view = resolve_leader(kube, t.cr_name)
        run.event("diag", f"leader {leader}", f"{len(instances)} instance pod(s)")
    except KubeError as exc:
        run.event("diag", "cannot resolve leader", str(exc)[:300])
        run.finalize("failed", headline={"error": str(exc)[:200]})
        return EXIT_FAILED

    def execute(check: DiagCheck) -> Optional[str]:
        """Run one check once; returns an error string or None."""
        try:
            if check.kind == "sql":
                rows = _run_sql_check(kube, leader, check)
            elif check.kind == "patroni":
                rows = _run_patroni_check(kube, leader)
            elif check.kind == "pgbackrest":
                rows = _run_pgbackrest_check(kube, leader, run)
            elif check.kind == "pods":
                rows = _run_pods_check(kube, t.cr_name)
            elif check.kind == "events":
                rows = _run_events_check(kube)
            elif check.kind == "disk":
                rows = _run_disk_check(kube, instances)
            else:
                return f"unknown check kind {check.kind}"
            _CheckWriter(run, check).rows(rows)
            return None
        except (KubeError, ValueError) as exc:
            return str(exc)[:300]

    errors: dict[str, str] = {}
    ran: list[str] = []
    for check in checks:
        err = execute(check)
        if err is None:
            ran.append(check.key)
            run.event("check", f"{check.key} ok")
        else:
            errors[check.key] = err
            run.event("check", f"{check.key} FAILED", err)

    if watch_s > 0:
        watchable = [c for c in checks if c.watch and c.key not in errors]
        run.event("diag", f"watch mode: {', '.join(c.key for c in watchable)}",
                  f"{watch_s:.0f}s @ {interval_s:.0f}s")
        deadline = time.time() + watch_s
        while time.time() < deadline:
            run.status_update(phase="watching",
                              remaining_s=max(0, int(deadline - time.time())))
            time.sleep(interval_s)
            for check in watchable:
                err = execute(check)
                if err is not None:
                    errors[check.key] = err
                    run.event("check", f"{check.key} FAILED mid-watch", err)
                    watchable = [c for c in watchable if c.key != check.key]

    headline = {"checks": len(ran), "failed": sorted(errors),
                "watch_s": watch_s or None}
    if not ran:
        run.finalize("failed", headline=headline)
        return EXIT_FAILED
    if errors:
        run.finalize("warning", headline=headline)
        return EXIT_WARNING
    run.finalize("complete", headline=headline)
    return EXIT_OK
