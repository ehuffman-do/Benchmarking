"""Built-in intelligence: evaluate field-standard health heuristics and emit
findings with severities and concrete remediations.

``ops health`` gathers a compact set of signals (SQL on the leader, patronictl,
pgBackRest info, kubectl pod/disk state) and runs them through threshold rules
distilled from production Postgres-on-Kubernetes operations. The output is a
single ``OPS_HEALTH_JSON`` document the webapp caches onto the Kube Target:

    {"status": "warn", "findings": [{"id", "severity", "title", "value",
      "detail", "remediation", "action"}, ...], "checked": 14, ...}

``severity``: ok < info < warn < crit. Every non-ok finding carries a
one-line remediation and an ``action`` hint the console turns into a
deep-link (e.g. open the matching diagnostic, or stage a config change).
Thresholds are defaults, overridable per run via ``params.thresholds``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pgbench_harness.ops import patroni
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.util import utc_now_iso

HEALTH_MARKER = "OPS_HEALTH_JSON"

SEP = "\x1f"

SEVERITY_ORDER = ("ok", "info", "warn", "crit")

# Default thresholds — every one overridable via params.thresholds.<key>.
THRESHOLDS: dict[str, float] = {
    "conn_pct_warn": 80.0, "conn_pct_crit": 95.0,
    "idle_tx_warn_s": 600.0, "idle_tx_crit_s": 3600.0,
    "long_tx_warn_s": 3600.0,
    "slot_retained_warn_bytes": 1 << 30, "slot_retained_crit_bytes": 8 << 30,
    "xid_age_warn": 1_000_000_000, "xid_age_crit": 1_500_000_000,
    "cache_hit_info": 0.99, "cache_hit_warn": 0.95,
    "lag_mb_warn": 100.0,
    "disk_pct_warn": 80.0, "disk_pct_crit": 90.0,
    "pod_restarts_warn": 3,
    "mem_pct_warn": 90.0,
    "backup_age_warn_s": 25 * 3600.0, "backup_age_crit_s": 49 * 3600.0,
}


def _q(kube: Kube, leader: str, sql: str) -> Optional[list[str]]:
    """One-row SQL signal; None when the query failed (finding: unknown)."""
    res = kube.psql(leader, sql, csv_sep=SEP, timeout_s=20)
    if not res.ok:
        return None
    line = res.stdout.strip().splitlines()
    return line[0].split(SEP) if line else None


def _f(vals: Optional[list[str]], idx: int) -> Optional[float]:
    try:
        return float(vals[idx]) if vals and vals[idx] != "" else None
    except (ValueError, IndexError):
        return None


class Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.checked = 0
        # Raw numeric signals, kept even when no finding fired — the webapp
        # stores these per run so it can do trend analysis (disk-fill
        # projection, connection growth) across health history.
        self.metrics: dict[str, float] = {}

    def add(self, fid: str, severity: str, title: str, value: str,
            detail: str, remediation: str = "",
            action: Optional[dict[str, Any]] = None) -> None:
        self.items.append({"id": fid, "severity": severity, "title": title,
                           "value": value, "detail": detail,
                           "remediation": remediation, "action": action or {}})

    def check(self) -> None:
        self.checked += 1

    @property
    def status(self) -> str:
        worst = 0
        for f in self.items:
            try:
                worst = max(worst, SEVERITY_ORDER.index(f["severity"]))
            except ValueError:
                continue
        return SEVERITY_ORDER[worst]


def _check(name: str, status: str, detail: str = "") -> None:
    print(json.dumps({"name": name, "status": status, "detail": detail}), flush=True)


def evaluate_sql_signals(kube: Kube, leader: str, th: dict[str, float],
                         out: Findings) -> None:
    # Connection saturation.
    out.check()
    v = _q(kube, leader, "/*health:connections*/ SELECT count(*), "
                         "current_setting('max_connections')::int "
                         "FROM pg_stat_activity")
    used, mx = _f(v, 0), _f(v, 1)
    if used is not None and mx:
        out.metrics["conn_pct"] = round(100.0 * used / mx, 1)
    if used is None or not mx:
        out.add("connections", "info", "Connection saturation", "unknown",
                "could not query pg_stat_activity")
    else:
        pct = 100.0 * used / mx
        sev = ("crit" if pct >= th["conn_pct_crit"] else
               "warn" if pct >= th["conn_pct_warn"] else "ok")
        if sev != "ok":
            out.add("connections", sev, "Connection saturation",
                    f"{used:.0f}/{mx:.0f} ({pct:.0f}%)",
                    "Backends near max_connections — new connections will be "
                    "refused at the limit.",
                    "Point apps at pgBouncer, lower app pool sizes, or raise "
                    "max_connections (DCS-coordinated restart).",
                    {"type": "diag", "checks": ["connections", "long_running"]})

    # Idle-in-transaction / long transactions.
    out.check()
    v = _q(kube, leader, "/*health:idle_tx*/ SELECT count(*), "
                         "coalesce(max(round(extract(epoch FROM now()-xact_start))),0) "
                         "FROM pg_stat_activity WHERE state LIKE 'idle in%'")
    n_idle, age = _f(v, 0), _f(v, 1)
    if n_idle and age:
        sev = ("crit" if age >= th["idle_tx_crit_s"] else
               "warn" if age >= th["idle_tx_warn_s"] else "ok")
        if sev != "ok":
            out.add("idle_tx", sev, "Idle-in-transaction sessions",
                    f"{n_idle:.0f} session(s), oldest {age:.0f}s",
                    "Idle transactions hold locks and block autovacuum from "
                    "reclaiming dead tuples.",
                    "Find and fix the app path that leaves transactions open; "
                    "consider idle_in_transaction_session_timeout.",
                    {"type": "diag", "checks": ["long_running"]})
    out.check()
    v = _q(kube, leader, "/*health:longtx*/ SELECT "
                         "coalesce(max(round(extract(epoch FROM now()-xact_start))),0) "
                         "FROM pg_stat_activity WHERE state <> 'idle'")
    age = _f(v, 0)
    if age and age >= th["long_tx_warn_s"]:
        out.add("long_tx", "warn", "Long-running transaction",
                f"oldest {age:.0f}s",
                "A transaction open for hours pins the xmin horizon: vacuum "
                "cannot clean anything newer.",
                "Inspect the session (diagnostics → long-running) and "
                "terminate it if safe.",
                {"type": "diag", "checks": ["long_running"]})

    # Inactive replication slots retaining WAL.
    out.check()
    v = _q(kube, leader, "/*health:slots*/ SELECT coalesce(sum(CASE WHEN NOT active "
                         "THEN pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) "
                         "ELSE 0 END),0)::bigint, "
                         "count(*) FILTER (WHERE NOT active) "
                         "FROM pg_replication_slots")
    retained, n_inactive = _f(v, 0), _f(v, 1)
    if n_inactive and retained is not None:
        sev = ("crit" if retained >= th["slot_retained_crit_bytes"] else
               "warn" if retained >= th["slot_retained_warn_bytes"] else "info")
        out.add("slots", sev, "Inactive replication slot(s) retaining WAL",
                f"{n_inactive:.0f} inactive, {retained / (1 << 30):.2f} GiB retained",
                "An inactive slot pins WAL forever; left alone it fills the "
                "data volume and takes the cluster down.",
                "Drop the slot if its consumer is gone "
                "(SELECT pg_drop_replication_slot(...)) or fix the consumer.",
                {"type": "diag", "checks": ["slots"]})

    # Wraparound distance.
    out.check()
    v = _q(kube, leader, "/*health:wraparound*/ SELECT "
                         "coalesce(max(age(datfrozenxid)),0) FROM pg_database")
    age = _f(v, 0)
    if age is not None:
        sev = ("crit" if age >= th["xid_age_crit"] else
               "warn" if age >= th["xid_age_warn"] else "ok")
        if sev != "ok":
            out.add("wraparound", sev, "Transaction ID wraparound approaching",
                    f"max age {age:,.0f} of 2,147,483,647",
                    "Past the limit Postgres refuses writes until a "
                    "single-user-mode vacuum.",
                    "Run VACUUM FREEZE on the oldest databases/tables and fix "
                    "whatever blocks autovacuum (long transactions, idle-in-tx).",
                    {"type": "diag", "checks": ["wraparound", "dead_tuples"]})

    # Cache hit ratio.
    out.check()
    v = _q(kube, leader, "/*health:cache_hit*/ SELECT "
                         "round(sum(blks_hit)::numeric/"
                         "nullif(sum(blks_hit+blks_read),0),4) "
                         "FROM pg_stat_database")
    ratio = _f(v, 0)
    if ratio is not None and ratio < th["cache_hit_info"]:
        sev = "warn" if ratio < th["cache_hit_warn"] else "info"
        out.add("cache_hit", sev, "Buffer cache hit ratio low",
                f"{ratio:.4f}",
                "The working set is not fitting in shared_buffers; reads go "
                "to disk.",
                "Consider raising shared_buffers (restart) or scaling memory; "
                "verify with the cache-hit diagnostic per database.",
                {"type": "diag", "checks": ["cache_hit"]})

    # pg_stat_monitor loaded: known memory-growth issue under sustained load
    # (field report 2026-07-22 — took down long-running tests; switching the
    # query source to pg_stat_statements resolved it).
    out.check()
    res = kube.psql(leader, "SHOW shared_preload_libraries", timeout_s=20)
    if res.ok and "pg_stat_monitor" in res.stdout:
        out.add("spl_pg_stat_monitor", "warn",
                "pg_stat_monitor is loaded",
                "in shared_preload_libraries",
                "pg_stat_monitor has a known memory-growth issue under "
                "sustained load (verified 2026-07 on long TPC-C runs) — it "
                "can OOM a member mid-test.",
                "Prefer pg_stat_statements for multi-hour tests: re-register "
                "the PMM query source as pgstatements, CREATE EXTENSION "
                "pg_stat_statements, then remove pg_stat_monitor from "
                "shared_preload_libraries (restart required) and DROP the "
                "extension.",
                {"type": "params", "filter": "shared_preload_libraries"})

    # Parameters awaiting a restart.
    out.check()
    v = _q(kube, leader, "/*health:pending*/ SELECT count(*) "
                         "FROM pg_settings WHERE pending_restart")
    pend = _f(v, 0)
    if pend is not None:
        out.metrics["pending_restart"] = pend
    if pend:
        out.add("pending_restart", "warn", "Parameters awaiting restart",
                f"{pend:.0f} parameter(s)",
                "Config changes are staged but not live — the running values "
                "differ from the configured ones until the members restart.",
                "Schedule/allow the operator's rolling restart (expect a "
                "failover) so the change takes effect.",
                {"type": "params", "filter": "pending"})


def evaluate_patroni(kube: Kube, leader_pod: str, th: dict[str, float],
                     out: Findings) -> None:
    out.check()
    try:
        view = patroni.fetch_view(kube, leader_pod)
    except KubeError as exc:
        out.add("patroni", "crit", "Patroni unreachable", "no view",
                str(exc)[:200], "Check instance pods and operator logs.",
                {"type": "diag", "checks": ["pods", "events_warnings"]})
        return
    if not view.leader_name:
        out.add("patroni_leader", "crit", "No Patroni leader", "leaderless",
                "The cluster has no writable primary right now.",
                "Check patronictl list / DCS health; a failover should elect a "
                "leader within seconds — if not, inspect operator and pod logs.",
                {"type": "diag", "checks": ["patroni_list", "events_warnings"]})
    for m in view.members:
        state = (m.state or "").lower()
        if state not in ("running", "streaming"):
            out.add(f"member_{m.name}", "crit", f"Member {m.name} is {m.state}",
                    m.state,
                    "This member is not healthy; the cluster is running with "
                    "reduced redundancy.",
                    "Check the pod's patroni container logs and recent "
                    "Kubernetes warnings.",
                    {"type": "diag", "checks": ["patroni_list", "pods"]})
        if m.lag_mb is not None:
            out.metrics["lag_mb_max"] = max(out.metrics.get("lag_mb_max", 0.0),
                                            float(m.lag_mb))
        if m.lag_mb is not None and m.lag_mb >= th["lag_mb_warn"]:
            out.add(f"lag_{m.name}", "warn", f"Replica {m.name} lagging",
                    f"{m.lag_mb:.0f} MB",
                    "A lagging replica weakens failover (data loss window) and "
                    "backup-standby backups.",
                    "Check replica I/O and network; see replication diagnostics.",
                    {"type": "diag", "checks": ["replication", "patroni_list"]})


def evaluate_kube(kube: Kube, cr_name: str, instances: list[str],
                  th: dict[str, float], out: Findings) -> None:
    # Pod restarts / phases.
    out.check()
    try:
        items = kube.json(["get", "pods"]).get("items") or []
    except KubeError as exc:
        out.add("pods", "info", "Pod state unknown", "query failed", str(exc)[:200])
        items = []
    oom_events = 0
    mem_limits: dict[str, float] = {}
    for item in items:
        name = (item.get("metadata") or {}).get("name", "")
        if cr_name and not name.startswith(cr_name):
            continue
        status = item.get("status") or {}
        phase = status.get("phase", "")
        cs = status.get("containerStatuses") or []
        restarts = sum(int(c.get("restartCount") or 0) for c in cs)
        # cgroup OOM kill: the kernel killed a process — container status
        # carries lastState.terminated.reason=OOMKilled / exitCode 137. This
        # is the pg_stat_monitor-leak signature (distinct from the startup
        # allocation failure below, which keeps the container running).
        for c in cs:
            term = ((c.get("lastState") or {}).get("terminated") or {})
            if (str(term.get("reason") or "") == "OOMKilled"
                    or int(term.get("exitCode") or 0) == 137):
                oom_events += 1
                out.add(f"oom_{name}_{c.get('name')}", "crit",
                        f"Container OOM-killed: {name}/{c.get('name')}",
                        f"OOMKilled at {term.get('finishedAt') or '?'} "
                        f"({restarts} restarts)",
                        "The kernel killed the process for exceeding its "
                        "memory limit — under load this repeats and takes "
                        "long runs down.",
                        "Check memory limits vs shared_buffers/work_mem; if "
                        "pg_stat_monitor is loaded, suspect its known "
                        "memory-growth issue and switch to pg_stat_statements.",
                        {"type": "diag", "checks": ["pods", "events_warnings"]})
        for c in ((item.get("spec") or {}).get("containers") or []):
            lim = (((c.get("resources") or {}).get("limits") or {})
                   .get("memory"))
            if lim and c.get("name") == "database":
                from pgbench_harness.ops.paramcheck import _mem_to_bytes
                b = _mem_to_bytes(lim)
                if b:
                    mem_limits[name] = b
        if phase not in ("Running", "Succeeded"):
            out.add(f"pod_{name}", "crit", f"Pod {name} is {phase}", phase,
                    "A cluster pod is not running.",
                    "Check recent Kubernetes warnings (scheduling, images, "
                    "OOM) and the pod's events.",
                    {"type": "diag", "checks": ["pods", "events_warnings"]})
        elif restarts >= th["pod_restarts_warn"]:
            out.add(f"restarts_{name}", "warn", f"Pod {name} restart loop?",
                    f"{restarts} restarts",
                    "Container restarts usually mean OOMKilled or a "
                    "crash-looping process.",
                    "kubectl describe the pod; check memory limits vs "
                    "shared_buffers/work_mem.",
                    {"type": "diag", "checks": ["pods", "events_warnings"]})
    out.metrics["oom_events"] = float(oom_events)

    # Allocation failure inside PostgreSQL (huge_pages-incident signature):
    # the container keeps running while Patroni loops on a postmaster that
    # cannot map shared memory — invisible to OOMKilled checks.
    out.check()
    for pod in instances:
        try:
            res = kube.run(["logs", pod, "-c", "database", "--tail=120"],
                           timeout_s=20)
        except KubeError:
            continue
        if not res.ok:
            continue
        hits = [ln.strip()[:240] for ln in res.stdout.splitlines()
                if ("Cannot allocate memory" in ln
                    or "out of memory" in ln.lower())]
        if hits:
            out.add(f"alloc_{pod}", "crit",
                    f"Memory allocation failure on {pod}",
                    hits[-1][:120],
                    "PostgreSQL cannot allocate/map memory — with a "
                    "shared-memory failure the postmaster never starts and "
                    "the member crash-loops.",
                    "Check huge_pages/hugepages-2Mi resources and "
                    "shared_buffers vs the container memory limit.",
                    {"type": "diag", "checks": ["pods", "events_warnings"]})

    # Memory pressure: working set vs limit (best-effort — metrics-server may
    # be absent; degrade silently). Also feeds the leak-trend history.
    out.check()
    try:
        res = kube.run(["top", "pods", "--no-headers"], timeout_s=20)
        rows = res.stdout.splitlines() if res.ok else []
    except KubeError:
        rows = []
    from pgbench_harness.ops.paramcheck import _mem_to_bytes
    ws_max = 0.0
    for ln in rows:
        parts = ln.split()
        if len(parts) < 3 or (cr_name and not parts[0].startswith(cr_name)):
            continue
        ws = _mem_to_bytes(parts[2])
        if not ws:
            continue
        ws_max = max(ws_max, ws)
        lim = mem_limits.get(parts[0])
        if lim and ws / lim * 100 >= th["mem_pct_warn"]:
            out.add(f"mem_{parts[0]}", "warn",
                    f"Memory pressure on {parts[0]}",
                    f"{ws / 1024 ** 2:.0f} MiB of {lim / 1024 ** 2:.0f} MiB "
                    f"limit ({ws / lim * 100:.0f}%)",
                    "The working set is close to the container limit — the "
                    "next allocation spike gets the container OOM-killed.",
                    "Lower work_mem/connection count or raise the limit; if "
                    "pg_stat_monitor is loaded, suspect its memory growth.",
                    {"type": "diag", "checks": ["pods"]})
    if ws_max:
        out.metrics["mem_ws_mb_max"] = round(ws_max / 1024 ** 2, 1)

    # Disk headroom on every instance pod.
    out.check()
    for pod in instances:
        res = kube.exec(pod, "database", ["df", "-P", "/pgdata"], timeout_s=15)
        if not res.ok:
            continue
        for ln in res.stdout.splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 5 and parts[4].endswith("%"):
                pct = float(parts[4].rstrip("%"))
                out.metrics["disk_pct_max"] = max(out.metrics.get("disk_pct_max", 0.0), pct)
                sev = ("crit" if pct >= th["disk_pct_crit"] else
                       "warn" if pct >= th["disk_pct_warn"] else "ok")
                if sev != "ok":
                    out.add(f"disk_{pod}", sev, f"Data volume filling on {pod}",
                            f"{pct:.0f}% used",
                            "When /pgdata fills, Postgres PANICs and the pod "
                            "crash-loops.",
                            "Expand the PVC (storage class must allow volume "
                            "expansion), clear inactive slots, or lower WAL "
                            "retention.",
                            {"type": "diag", "checks": ["pvc_usage", "slots"]})


def evaluate_backups(kube: Kube, leader_pod: str, th: dict[str, float],
                     out: Findings) -> None:
    import time as _time
    out.check()
    res = kube.exec(leader_pod, "database",
                    ["pgbackrest", "--stanza=db", "info", "--output=json"],
                    timeout_s=30)
    if not res.ok:
        out.add("backups", "warn", "pgBackRest info unavailable",
                "query failed", (res.stderr or res.stdout).strip()[:200],
                "Check repo-host pod / repo credentials.",
                {"type": "diag", "checks": ["backup_info"]})
        return
    try:
        doc = json.loads(res.stdout)
        backups = (doc[0].get("backup") or []) if isinstance(doc, list) and doc else []
    except (ValueError, AttributeError, IndexError):
        out.add("backups", "warn", "pgBackRest info unparsable", "bad JSON",
                "info output did not parse", "",
                {"type": "diag", "checks": ["backup_info"]})
        return
    if not backups:
        out.add("backups", "warn", "No backups in the repository", "0 backups",
                "There is nothing to restore from.",
                "Run a full backup and add schedules to the CR.",
                {"type": "backup"})
        return
    stops = [((b.get("timestamp") or {}).get("stop") or 0) for b in backups]
    newest = max(float(s) for s in stops if s) if any(stops) else 0
    if newest:
        age_s = _time.time() - newest
        out.metrics["backup_age_h"] = round(age_s / 3600, 2)
        sev = ("crit" if age_s >= th["backup_age_crit_s"] else
               "warn" if age_s >= th["backup_age_warn_s"] else "ok")
        if sev != "ok":
            out.add("backup_age", sev, "Latest backup is stale",
                    f"{age_s / 3600:.1f}h old",
                    "Your recovery point objective is only as good as the "
                    "newest backup + archived WAL.",
                    "Run an on-demand backup and check why schedules aren't "
                    "producing fresh ones.",
                    {"type": "backup"})


def run_health(spec: OpsSpec) -> int:
    """Evaluate all heuristics; print check lines + OPS_HEALTH_JSON. 0 or 3."""
    t = spec.target
    kube = Kube(context=t.context, namespace=t.namespace)
    th = dict(THRESHOLDS)
    overrides = spec.params.get("thresholds") or {}
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k in th:
                try:
                    th[k] = float(v)
                except (TypeError, ValueError):
                    continue

    payload: dict[str, Any] = {"collected_utc": utc_now_iso(), "status": "ok",
                               "findings": [], "checked": 0, "leader": ""}
    out = Findings()
    try:
        from pgbench_harness.ops.crconfig import resolve_leader
        instances, leader, _view = resolve_leader(kube, t.cr_name)
        payload["leader"] = leader
        _check("leader", "ok", leader)
    except KubeError as exc:
        _check("leader", "fail", str(exc)[:300])
        out.add("cluster", "crit", "Cluster unreachable", "no leader",
                str(exc)[:200],
                "Validate the target and check pods/operator.",
                {"type": "validate"})
        payload["findings"] = out.items
        payload["status"] = out.status
        print(f"{HEALTH_MARKER} {json.dumps(payload)}", flush=True)
        return 3

    evaluate_sql_signals(kube, leader, th, out)
    evaluate_patroni(kube, leader, th, out)
    evaluate_kube(kube, t.cr_name, instances, th, out)
    evaluate_backups(kube, leader, th, out)

    payload["metrics"] = out.metrics
    payload["findings"] = sorted(
        out.items, key=lambda f: -SEVERITY_ORDER.index(f.get("severity", "ok")))
    payload["checked"] = out.checked
    payload["status"] = out.status
    counts = {s: sum(1 for f in out.items if f["severity"] == s)
              for s in ("crit", "warn", "info")}
    _check("health", "ok" if out.status in ("ok", "info") else out.status,
           f"{out.checked} checks: {counts['crit']} critical, "
           f"{counts['warn']} warning, {counts['info']} info")
    print(f"{HEALTH_MARKER} {json.dumps(payload)}", flush=True)
    return 0
