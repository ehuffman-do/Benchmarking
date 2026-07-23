"""Live PostgreSQL parameter catalog — introspected, never hand-typed.

``ops pg-params`` snapshots the FULL ``pg_settings`` catalog from the current
Patroni leader: every GUC the running server actually has (version- and
extension-accurate), with its live value, type, unit, min/max, enum values,
context (how a change is applied) and description. The webapp caches the
snapshot onto the Kube Target so the console can offer a searchable,
click-to-apply parameter map whose validation (types, ranges, enums) comes
from the server itself — a wrong name or out-of-range value is impossible to
stage.

A static overlay classifies each parameter's APPLY CHANNEL, because on a
Patroni-managed cluster not everything may go through
``spec.patroni.dynamicConfiguration.postgresql.parameters``:

* ``patroni-locked``  — Patroni owns these and silently overrides user values
  (listen_addresses, port, hot_standby, ...): never apply via the CR.
* ``dcs-coordinated`` — Patroni validates/coordinates these cluster-wide
  through DCS and orders a coordinated restart when they shrink
  (max_connections, wal_level, ...): applying works but expect
  pending_restart + a rolling restart.
* ``cr``              — normal: patch the CR, operator syncs DCS, Patroni
  reloads; postmaster-context ones still need a restart.
* ``readonly``        — pg_settings context 'internal': compiled in, display
  only.
"""

from __future__ import annotations

import json
from typing import Any

from pgbench_harness.ops.crconfig import (PATRONI_PARAMS_PATH,
                                          PGBACKREST_GLOBAL_PATH,
                                          PGBOUNCER_GLOBAL_PATH, _dig)
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.util import utc_now_iso

PARAMS_MARKER = "OPS_PARAMS_JSON"

# Patroni force-sets these on every member (CMDLINE_OPTIONS with a
# false-validator, passed as postmaster args — precedence above ALTER SYSTEM);
# a CR-supplied value is silently dropped. Verified against Patroni
# patroni/postgresql/config.py.
PATRONI_LOCKED = frozenset({
    "listen_addresses", "port", "cluster_name", "hot_standby", "wal_log_hints",
})

# Patroni accepts these only through DCS dynamic configuration and coordinates
# them cluster-wide (must match on primary and replicas; shared-memory ones
# have a restart-ordering rule — increase: replicas first; decrease: primary
# first, replicas may stay pending_restart until replay catches up).
PATRONI_DCS = frozenset({
    "max_connections", "max_locks_per_transaction", "max_worker_processes",
    "max_prepared_transactions", "max_replication_slots", "max_wal_senders",
    "wal_level", "wal_keep_segments", "wal_keep_size", "track_commit_timestamp",
})

# The operator itself owns these (mandatory values reverted on every
# reconcile, or written by Patroni on replicas): TLS/socket/log plumbing,
# pgBackRest archiving, and recovery/replication identity parameters.
# Union of the Crunchy v5.8 spec.config CEL-forbidden list, both operators'
# Mandatory sets, and Patroni's recovery-parameter set.
OPERATOR_LOCKED = frozenset({
    "config_file", "data_directory", "external_pid_file", "hba_file",
    "ident_file", "logging_collector", "log_file_mode",
    "ssl", "ssl_ca_file", "ssl_cert_file", "ssl_key_file", "ssl_crl_file",
    "ssl_crl_dir", "ssl_ciphers", "ssl_dh_params_file", "ssl_library",
    "ssl_max_protocol_version", "ssl_min_protocol_version",
    "ssl_passphrase_command", "ssl_passphrase_command_supports_reload",
    "ssl_prefer_server_ciphers",
    "unix_socket_directories", "unix_socket_group", "unix_socket_permissions",
    "archive_mode", "archive_command", "restore_command",
    "synchronous_standby_names", "primary_conninfo", "primary_slot_name",
    "recovery_min_apply_delay", "recovery_target", "recovery_target_action",
    "recovery_target_inclusive", "recovery_target_lsn", "recovery_target_name",
    "recovery_target_time", "recovery_target_timeline", "recovery_target_xid",
})

# One JSON document out of psql: no separator/quoting pitfalls, enumvals stays
# a real array. ORDER BY inside the aggregate keeps the catalog stable.
_CATALOG_SQL = (
    "SELECT coalesce(json_agg(row_to_json(s) ORDER BY s.name), '[]'::json) FROM ("
    "SELECT name, setting, unit, vartype, min_val, max_val, enumvals, context, "
    "category, short_desc, boot_val, reset_val, source, pending_restart "
    "FROM pg_settings) s")


def _check(name: str, status: str, detail: str = "") -> None:
    print(json.dumps({"name": name, "status": status, "detail": detail}), flush=True)


def sidecar_catalog() -> dict[str, Any]:
    """Static option catalogs for the sidecar systems (pgBackRest, Patroni DCS,
    pgBouncer) — research-curated with per-operator CR paths. Unlike the PG
    catalog these cannot be introspected from a live system, so they ship as
    packaged data; each entry names the exact CR path it applies through."""
    from pathlib import Path
    path = Path(__file__).parent / "sidecar_catalog.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else {}


def classify(name: str, context: str) -> str:
    """The apply channel for one parameter (see module docstring)."""
    if context == "internal":
        return "readonly"
    if name in PATRONI_LOCKED:
        return "patroni-locked"
    if name in OPERATOR_LOCKED:
        return "operator-managed"
    if name in PATRONI_DCS:
        return "dcs-coordinated"
    return "cr"


def build_catalog(rows: list[dict[str, Any]], cr_params: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge live pg_settings rows with the channel overlay + CR-managed values."""
    out = []
    for r in rows:
        name = str(r.get("name", ""))
        context = str(r.get("context", ""))
        cr_val = cr_params.get(name)
        out.append({
            "name": name,
            "setting": r.get("setting"),
            "unit": r.get("unit"),
            "vartype": r.get("vartype"),
            "min_val": r.get("min_val"),
            "max_val": r.get("max_val"),
            "enumvals": r.get("enumvals") or [],
            "context": context,
            "category": r.get("category"),
            "short_desc": r.get("short_desc"),
            "boot_val": r.get("boot_val"),
            "reset_val": r.get("reset_val"),
            "source": r.get("source"),
            "pending_restart": bool(r.get("pending_restart")),
            "channel": classify(name, context),
            "restart_required": context == "postmaster",
            "cr_value": None if cr_val is None else str(cr_val),
        })
    return out


def run_pg_params(spec: OpsSpec) -> int:
    """Snapshot the parameter catalog from the leader. Exit 0 ok / 3 failed."""
    t = spec.target
    kube = Kube(context=t.context, namespace=t.namespace)
    payload: dict[str, Any] = {"collected_utc": utc_now_iso(), "leader": "",
                               "pg_version": "", "params": [],
                               "cr_managed": {}, "pgbackrest_global": {},
                               "pgbouncer_global": {}, "patroni_dcs": {}}

    try:
        from pgbench_harness.ops.crconfig import resolve_leader
        _instances, leader, _view = resolve_leader(kube, t.cr_name)
        payload["leader"] = leader
        _check("leader", "ok", leader)
    except KubeError as exc:
        _check("leader", "fail", str(exc)[:300])
        print(f"{PARAMS_MARKER} {json.dumps(payload)}", flush=True)
        return 3

    cr_params: dict[str, Any] = {}
    try:
        cr = kube.cluster_cr(t.cr_kind, t.cr_name)
        cr_params = _dig(cr, PATRONI_PARAMS_PATH)
        payload["cr_managed"] = {k: str(v) for k, v in cr_params.items()}
        payload["pgbackrest_global"] = {
            k: str(v) for k, v in _dig(cr, PGBACKREST_GLOBAL_PATH).items()}
        payload["pgbouncer_global"] = {
            k: str(v) for k, v in _dig(cr, PGBOUNCER_GLOBAL_PATH).items()}
        pat = (cr.get("spec") or {}).get("patroni") or {}
        dcs: dict[str, Any] = {}
        if pat.get("leaderLeaseDurationSeconds") is not None:
            dcs["ttl"] = str(pat["leaderLeaseDurationSeconds"])
        if pat.get("syncPeriodSeconds") is not None:
            dcs["loop_wait"] = str(pat["syncPeriodSeconds"])

        def _flatten(prefix: str, node: Any) -> None:
            if not isinstance(node, dict):
                dcs[prefix] = str(node)
                return
            for k2, v2 in node.items():
                if prefix == "" and k2 == "postgresql" and isinstance(v2, dict):
                    sub = {a: b for a, b in v2.items()
                           if a not in ("parameters", "pg_hba", "pg_ident")}
                    _flatten("postgresql", sub) if sub else None
                    continue
                _flatten(f"{prefix}.{k2}" if prefix else str(k2), v2)
        _flatten("", pat.get("dynamicConfiguration") or {})
        payload["patroni_dcs"] = dcs
        _check("cluster-cr", "ok",
               f"{len(cr_params)} parameter(s) managed via the CR")
    except KubeError as exc:
        # Catalog still valuable without the CR overlay — degrade, don't die.
        _check("cluster-cr", "warn", str(exc)[:300])

    try:
        res = kube.psql(leader, _CATALOG_SQL, timeout_s=30)
        if not res.ok:
            raise KubeError((res.stderr or res.stdout).strip()[:300]
                            or "psql failed")
        rows = json.loads(res.stdout.strip() or "[]")
        if not isinstance(rows, list) or not rows:
            raise KubeError("pg_settings returned no rows")
    except (KubeError, ValueError) as exc:
        _check("pg-settings", "fail", str(exc)[:300])
        print(f"{PARAMS_MARKER} {json.dumps(payload)}", flush=True)
        return 3

    payload["params"] = build_catalog(rows, cr_params)
    ver = kube.psql(leader, "SHOW server_version")
    if ver.ok:
        payload["pg_version"] = ver.stdout.strip()

    # Locked-set drift audit: the PATRONI_LOCKED/OPERATOR_LOCKED sets were
    # verified against Patroni's source at one point in time and drift with
    # releases. Cross-check classification against the OBSERVED source on the
    # live server: Patroni's force-set params surface as source='command line'
    # (CMDLINE_OPTIONS), so a 'cr'-classified param showing 'command line' is
    # a param Patroni actually owns — and a locked one showing a config-file
    # source suggests the lock no longer applies. Warning only, never fatal.
    drift = []
    for p in payload["params"]:
        src = str(p.get("source") or "")
        if p["channel"] == "cr" and src == "command line":
            drift.append(f"{p['name']} (classified 'cr' but source=command "
                         "line — Patroni appears to force it)")
        elif p["channel"] == "patroni-locked" \
                and src in ("configuration file", "database", "user"):
            drift.append(f"{p['name']} (classified patroni-locked but "
                         f"source={src} — the lock may have been lifted)")
    payload["channel_drift"] = drift
    if drift:
        _check("channel-drift", "warn",
               "classification disagrees with observed pg_settings.source for "
               + ", ".join(d.split(" ")[0] for d in drift[:6])
               + (f" (+{len(drift) - 6} more)" if len(drift) > 6 else "")
               + " — the locked sets may have drifted with a Patroni/operator "
                 "release")
    n_mod = sum(1 for p in payload["params"]
                if p["source"] not in ("default", None))
    n_pend = sum(1 for p in payload["params"] if p["pending_restart"])
    _check("pg-settings", "ok",
           f"{len(payload['params'])} parameters ({n_mod} non-default"
           + (f", {n_pend} pending restart" if n_pend else "") + ")")

    print(f"{PARAMS_MARKER} {json.dumps(payload)}", flush=True)
    return 0
