"""CR configuration operations: read -> diff -> (dry-run | patch) -> verify.

Ports apply_and_prep.sh with its field lessons intact:
* dry-run is first-class: the exact merge patch AND a value-level diff against
  the live CR are written to the run dir before anything is applied;
* after an apply, poll until the values are live in pg_settings on the leader,
  and FAIL LOUDLY (exit code 1 / status 'warning') when any parameter shows
  pending_restart = t — the operator will roll pods, expect a failover;
* pgBackRest globals never appear in pg_settings — verification is
  CR -> rendered config inside the pod;
* rollback is a NEW patch built from the pre-change snapshot's values — never
  a blind kubectl apply of the whole snapshot.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.ops import patroni
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.oprun import (EXIT_ABORTED, EXIT_FAILED, EXIT_OK,
                                       EXIT_WARNING, OpsRun, read_meta)
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.util import atomic_write_text

# The proven tuning bundles (user-editable in the UI before apply).
PATRONI_BUNDLE = {
    "max_wal_size": "49152",            # MB
    "min_wal_size": "2048",
    "archive_timeout": "300",           # s
    "wal_keep_size": "2048",
    "checkpoint_timeout": "900",
    "checkpoint_completion_target": "0.9",
}
PGBACKREST_BUNDLE = {
    "process-max": "4",
    "archive-async": "y",
    "spool-path": "/pgdata",
}

import re

# A plain, unquoted PostgreSQL identifier we are willing to interpolate into
# DROP/CREATE DATABASE (which cannot be parameterized).
_SAFE_DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

PATRONI_PARAMS_PATH = ("spec", "patroni", "dynamicConfiguration", "postgresql",
                       "parameters")
PGBACKREST_GLOBAL_PATH = ("spec", "backups", "pgbackrest", "global")
PGBOUNCER_GLOBAL_PATH = ("spec", "proxy", "pgBouncer", "config", "global")

SCHEDULES_MARKER = "OPS_SCHEDULES_JSON"


def _dig(doc: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    cur: Any = doc
    for key in path:
        cur = (cur or {}).get(key)
        if cur is None:
            return {}
    return dict(cur) if isinstance(cur, dict) else {}


def _nest(path: tuple[str, ...], leaf: Any) -> dict[str, Any]:
    out: Any = leaf
    for key in reversed(path):
        out = {key: out}
    return out


def value_diff(current: dict[str, Any], proposed: dict[str, Any],
               rows: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, list]:
    """Per-key [old, new] for keys whose value would change (all values as str).

    With *rows* (pg_settings catalog rows by name), values are compared under
    GUC normalization — "1GB" vs "1024" (unit MB), "on" vs "true", "0.9" vs
    "0.90" are NOT diffs. Without rows, plain string comparison."""
    from pgbench_harness.ops.paramcheck import values_equal
    changes: dict[str, list] = {}
    for key, new in proposed.items():
        old = current.get(key)
        if old is None:
            changes[key] = [None, str(new)]
            continue
        row = (rows or {}).get(key) or {}
        if not values_equal(old, new, row.get("unit"), row.get("vartype")):
            changes[key] = [str(old), str(new)]
    return changes


def resolve_leader(kube: Kube, cr_name: str) -> tuple[list[str], str, patroni.PatroniView]:
    """(instance pod names, leader pod name, patroni view). Raises KubeError."""
    from pgbench_harness.ops.discover import classify_pods
    pods = kube.json(["get", "pods"]).get("items") or []
    buckets = classify_pods(pods, cr_name)
    instances = [p["name"] for p in buckets["instances"] if p["phase"] == "Running"]
    if not instances:
        raise KubeError(f"no running instance pods for cluster '{cr_name}'")
    view = patroni.fetch_view(kube, instances[0])
    leader = view.leader_name
    if not leader:
        raise KubeError("patroni reports no leader")
    return instances, leader, view


def resolve_leader_poll(kube: Kube, cr_name: str, timeout_s: float = 300,
                        poll_s: float = 5.0, run: Optional[OpsRun] = None
                        ) -> tuple[list[str], str, patroni.PatroniView]:
    """resolve_leader with retry/backoff up to *timeout_s*.

    Field lesson (huge_pages incident): after a restart-required CR patch the
    operator may already be rolling pods, so "no running instance pods" /
    "patroni reports no leader" 600 ms after the patch is the EXPECTED state,
    not a failure. A single instantaneous check is never valid after a
    restart-required change — poll until a leader is elected or the timeout
    expires."""
    deadline = time.monotonic() + max(1.0, timeout_s)
    attempts = 0
    while True:
        try:
            return resolve_leader(kube, cr_name)
        except KubeError as exc:
            attempts += 1
            if time.monotonic() >= deadline:
                raise KubeError(
                    f"no leader within {timeout_s:.0f}s ({attempts} attempts) "
                    f"— last: {str(exc)[:200]}")
            if run is not None and attempts == 1:
                run.event("verify", "cluster transiently leaderless — polling",
                          f"{str(exc)[:150]} (expected while the operator "
                          f"rolls pods; waiting up to {timeout_s:.0f}s)")
        time.sleep(poll_s)


def catalog_rows(kube: Kube, leader: str,
                 names: list[str]) -> Optional[dict[str, dict[str, Any]]]:
    """pg_settings rows for *names* from the leader, keyed by name.

    One JSON document out of psql (like the params snapshot): no separator
    pitfalls — a GUC value containing '|' can never corrupt the parse.

    Returns None when the QUERY failed (catalog unavailable — callers degrade
    gracefully); a dict otherwise. A name absent from the returned dict after
    a successful query really is unknown to the server."""
    safe = [str(k) for k in names if re.match(r"^[A-Za-z0-9_.]+$", str(k))]
    if not safe:
        return {}
    in_list = ",".join(f"'{k}'" for k in safe)
    sql = ("SELECT coalesce(json_agg(row_to_json(s)), '[]'::json) FROM ("
           "SELECT name, setting, unit, vartype, min_val, max_val, enumvals, "
           f"context, pending_restart FROM pg_settings WHERE name IN ({in_list})) s")
    try:
        res = kube.psql(leader, sql, timeout_s=20)
    except KubeError:
        return None
    if not res.ok:
        return None
    try:
        doc = json.loads(res.stdout.strip() or "[]")
    except ValueError:
        return None
    want = set(safe)
    return {str(r["name"]): r for r in doc
            if isinstance(r, dict) and str(r.get("name", "")) in want}


def _snapshot_cr(kube: Kube, run: OpsRun, cr_kind: str, cr_name: str) -> dict[str, Any]:
    """Full CR snapshot (yaml + json) into the run dir before any patch."""
    res = kube.run(["get", cr_kind, cr_name, "-o", "yaml"], check=True)
    atomic_write_text(run.run_dir / "cr_snapshot.yaml", res.stdout)
    cr = kube.cluster_cr(cr_kind, cr_name)
    atomic_write_text(run.run_dir / "cr_snapshot.json", json.dumps(cr, indent=2))
    run.event("snapshot", "CR snapshot captured", f"{cr_kind}/{cr_name}")
    return cr


def verify_pg_settings(kube: Kube, leader: str, expected: dict[str, Any],
                       timeout_s: float, poll_s: float = 2.0,
                       logger: Any = None,
                       rows: Optional[dict[str, dict[str, Any]]] = None
                       ) -> tuple[dict[str, str], list[str], bool]:
    """Poll pg_settings on the leader until every expected value is live.

    Values are compared NORMALIZED (paramcheck): ``pg_settings.setting`` is
    always the base-unit number, so a CR value written as "1GB" or "on" must
    be converted before comparing or a correct, live value reads as a verify
    failure. The query returns one JSON document — a value containing '|'
    can never corrupt the parse.

    Returns (live values, pending_restart names, all_matched)."""
    from pgbench_harness.ops.paramcheck import values_equal
    if not expected:
        # Nothing with a target value to confirm (e.g. a removal-only change).
        # Return not-matched so the caller never reports a vacuous "verified".
        return {}, [], False
    # pg_settings names are validated GUC identifiers, but guard the interpolation
    # anyway — only word-characters can reach the IN () list.
    safe = [k for k in expected if re.match(r"^[A-Za-z0-9_.]+$", str(k))]
    names = ",".join(f"'{k}'" for k in safe)
    sql = ("SELECT coalesce(json_agg(row_to_json(s)), '[]'::json) FROM ("
           "SELECT name, setting, unit, vartype, pending_restart "
           f"FROM pg_settings WHERE name IN ({names})) s")

    def _match(name: str, live_val: str, row: dict[str, Any]) -> bool:
        meta = row or (rows or {}).get(name) or {}
        return values_equal(live_val, expected[name],
                            meta.get("unit"), meta.get("vartype"))

    deadline = time.monotonic() + timeout_s
    live: dict[str, str] = {}
    pending: list[str] = []
    while True:
        res = kube.psql(leader, sql, timeout_s=20)
        live, pending = {}, []
        live_rows: dict[str, dict[str, Any]] = {}
        if res.ok:
            try:
                doc = json.loads(res.stdout.strip() or "[]")
            except ValueError:
                doc = []
            for r in doc if isinstance(doc, list) else []:
                nm = str(r.get("name", "")) if isinstance(r, dict) else ""
                if nm not in expected:
                    continue
                live[nm] = "" if r.get("setting") is None else str(r["setting"])
                live_rows[nm] = r
                if r.get("pending_restart"):
                    pending.append(nm)
            unconverged = [nm for nm in expected
                           if not (nm in live and _match(nm, live[nm],
                                                         live_rows.get(nm, {})))]
            # pending_restart params will NEVER converge without a restart —
            # stop polling for them, surface loudly instead.
            if not unconverged or all(nm in pending for nm in unconverged):
                return live, pending, not unconverged
        if time.monotonic() >= deadline:
            return live, pending, all(
                nm in live and _match(nm, live[nm], live_rows.get(nm, {}))
                for nm in expected)
        if logger:
            logger.info("verify: waiting for pg_settings to converge "
                        "(%s pending)", len(expected) - sum(
                            1 for nm in expected
                            if nm in live and _match(nm, live[nm],
                                                     live_rows.get(nm, {}))))
        time.sleep(poll_s)


def await_cluster_ready(kube: Kube, cr_name: str, names: list[str],
                        timeout_s: float, poll_s: float = 5.0) -> tuple[bool, str]:
    """Wait for full cluster recovery after a restart-required change: a
    leader elected AND every member running/streaming AND none of *names*
    still flagged pending_restart on the leader.

    Returns (converged, detail) — detail carries the last blocking condition
    so a timeout message says WHY ("still crash-looping" vs "be patient")."""
    deadline = time.monotonic() + max(1.0, timeout_s)
    detail = ""
    while True:
        try:
            _insts, leader, view = resolve_leader(kube, cr_name)
            bad = [f"{m.name}={m.state}" for m in view.members
                   if (m.state or "").lower() not in ("running", "streaming")]
            if bad:
                detail = f"member(s) not healthy: {', '.join(bad)}"
            else:
                still = [n for n, r in (catalog_rows(kube, leader, names)
                                        or {}).items()
                         if r.get("pending_restart")] if names else []
                if still:
                    detail = f"still pending restart: {', '.join(still)}"
                else:
                    return True, leader
        except KubeError as exc:
            detail = str(exc)[:200]
        if time.monotonic() >= deadline:
            return False, detail
        time.sleep(poll_s)


_FATAL_RE = re.compile(r"FATAL|PANIC|Cannot allocate memory|out of memory",
                       re.IGNORECASE)


def verify_diagnostics(kube: Kube, cr_name: str) -> dict[str, Any]:
    """Evidence pack for a verify/rollout failure: patronictl member states +
    the last FATAL/PANIC lines from each instance's database container.

    Field lesson: a bare 'patroni reports no leader' cannot distinguish
    "still crash-looping — roll back" from "recovering — be patient". This
    dump makes that call possible at a glance."""
    diag: dict[str, Any] = {"members": [], "fatal_lines": {}}
    names: list[str] = []
    try:
        from pgbench_harness.ops.discover import classify_pods
        pods = kube.json(["get", "pods"]).get("items") or []
        buckets = classify_pods(pods, cr_name)
        names = [p["name"] for p in buckets["instances"]]
        diag["pod_phases"] = {p["name"]: p["phase"] for p in buckets["instances"]}
    except KubeError as exc:
        diag["pods_error"] = str(exc)[:200]
    for pod in names:
        try:
            view = patroni.fetch_view(kube, pod, timeout_s=10)
            diag["members"] = view.to_dict()["members"]
            break
        except KubeError as exc:
            diag["patronictl_error"] = str(exc)[:200]
    for pod in names:
        try:
            res = kube.run(["logs", pod, "-c", "database", "--tail=120"],
                           timeout_s=20)
        except KubeError:
            continue
        if not res.ok:
            continue
        hits = [ln.strip()[:300] for ln in res.stdout.splitlines()
                if _FATAL_RE.search(ln)]
        if hits:
            diag["fatal_lines"][pod] = hits[-5:]
    return diag


def diagnostics_summary(diag: dict[str, Any]) -> str:
    parts = []
    members = diag.get("members") or []
    if members:
        parts.append("members: " + ", ".join(
            f"{m.get('name')}={m.get('state')}" for m in members))
    elif diag.get("patronictl_error"):
        parts.append(f"patronictl: {diag['patronictl_error']}")
    for pod, lines in (diag.get("fatal_lines") or {}).items():
        if lines:
            parts.append(f"{pod}: {lines[-1]}")
    return "; ".join(parts)[:600]


def _ini_pattern(expected: dict[str, Any]) -> str:
    """grep -E pattern matching section headers + ANCHORED expected keys.

    Field lesson: an unescaped, unanchored ``"|".join(expected)`` made key
    ``compress-level`` also match ``compress-level-network`` lines; anchoring
    (``^key\\s*=``) and escaping close that off. Section headers ride along so
    the parser can attribute each key to its section."""
    return "|".join([r"^\s*\["] +
                    [rf"^\s*{re.escape(str(k))}\s*=" for k in expected])


def verify_pgbackrest_config(kube: Kube, leader: str, expected: dict[str, Any],
                             timeout_s: float, poll_s: float = 2.0,
                             section: str = "global") -> tuple[dict[str, str], bool]:
    """CR -> rendered config in the pod, section-aware.

    A key present in both [global] and a stanza section must be read from the
    section the CR change targets — last-write-wins across sections reported
    the stanza's value for a global change (false pass/fail)."""
    pattern = _ini_pattern(expected)
    deadline = time.monotonic() + timeout_s
    rendered: dict[str, str] = {}
    while True:
        res = kube.exec(leader, "database",
                        ["grep", "-rE", pattern, "/etc/pgbackrest/"], timeout_s=20)
        # grep -r prefixes each line with the file path; track the current
        # section PER FILE so multi-file renders can't bleed into each other.
        sections: dict[str, str] = {}
        rendered = {}
        for line in res.stdout.splitlines():
            fname, body = line.split(":", 1) if ":" in line else ("", line)
            stripped = body.strip()
            m = re.match(r"^\[([^\]]+)\]", stripped)
            if m:
                sections[fname] = m.group(1).strip()
                continue
            if "=" in stripped and sections.get(fname, "") == section:
                k, v = stripped.split("=", 1)
                if k.strip() in expected:
                    rendered[k.strip()] = v.strip()
        ok = all(rendered.get(k) == str(v) for k, v in expected.items())
        if ok or time.monotonic() >= deadline:
            return rendered, ok
        time.sleep(poll_s)


def _parse_ini_sections(text: str) -> dict[str, dict[str, str]]:
    """section -> {key: value}. Keys before any header land in section ''.

    Field lesson: flattening all sections into one namespace let a
    per-database override in [databases] satisfy (or contradict) a
    [pgbouncer] global check."""
    out: dict[str, dict[str, str]] = {}
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "%")):
            continue
        m = re.match(r"^\[([^\]]+)\]", stripped)
        if m:
            current = m.group(1).strip()
            continue
        if "=" in stripped:
            k, v = stripped.split("=", 1)
            out.setdefault(current, {})[k.strip()] = v.strip()
    return out


def verify_pgbouncer_config(kube: Kube, cr_name: str, expected: dict[str, Any],
                            timeout_s: float,
                            poll_s: float = 2.0) -> tuple[dict[str, str], bool, str]:
    """CR -> operator-rendered pgBouncer config. Two sources, most reliable
    first: (1) the pgBouncer ConfigMap — API-side, updated the moment the
    operator reconciles, no kubelet volume-propagation lag; (2) grep of
    /etc/pgbouncer inside a pgbouncer pod. Field lesson: the mounted file can
    trail the ConfigMap by a minute or more, and an exec failure used to be
    silently read as "no values" — the last error is now surfaced in the note.

    Values are read from the [pgbouncer] section ONLY — a same-named key in
    [databases]/[users] must never satisfy (or contradict) a global check.

    Returns (rendered, matched, note)."""
    from pgbench_harness.ops.discover import classify_pods
    pattern = _ini_pattern(expected)
    deadline = time.monotonic() + timeout_s
    rendered: dict[str, str] = {}
    note = ""

    def _take(text: str) -> None:
        section = _parse_ini_sections(text).get("pgbouncer") or {}
        rendered.update({k: v for k, v in section.items() if k in expected})

    while True:
        # 1) ConfigMap (source of truth for what the operator rendered)
        try:
            doc = kube.json(["get", "configmaps"])
            for item in doc.get("items") or []:
                name = str(item.get("metadata", {}).get("name", ""))
                if cr_name not in name or "pgbouncer" not in name.lower():
                    continue
                for text in (item.get("data") or {}).values():
                    _take(str(text))
            if rendered and all(rendered.get(k) == str(v) for k, v in expected.items()):
                return rendered, True, "confirmed in the operator-rendered ConfigMap"
        except KubeError as exc:
            note = f"configmap read failed: {str(exc)[:150]}"
        # 2) the mounted file inside a pgbouncer pod
        try:
            pods = classify_pods(kube.json(["get", "pods"]).get("items") or [], cr_name)
            pod = next((p["name"] for p in pods["pgbouncer"]
                        if p["phase"] == "Running"), None)
            if pod is None:
                note = "no running pgbouncer pod to inspect"
            else:
                res = kube.exec(pod, "pgbouncer",
                                ["grep", "-hrE", pattern, "/etc/pgbouncer/"],
                                timeout_s=20)
                if res.ok:
                    _take(res.stdout)
                    if all(rendered.get(k) == str(v) for k, v in expected.items()):
                        return rendered, True, f"confirmed in /etc/pgbouncer on {pod}"
                else:
                    note = (f"pod grep failed on {pod}: "
                            f"{(res.stderr or res.stdout).strip()[:150]}")
        except KubeError as exc:
            note = f"pod inspection failed: {str(exc)[:150]}"
        if time.monotonic() >= deadline:
            return rendered, False, note or "values not yet rendered anywhere visible"
        time.sleep(poll_s)


def _capture_patroni_config(kube: Kube, run: OpsRun, leader: str, scope: str) -> None:
    """patronictl show-config — makes CR -> DCS -> live-GUC propagation visible."""
    res = kube.exec(leader, "database",
                    ["patronictl", "show-config"] + ([scope] if scope else []),
                    timeout_s=20)
    if res.ok:
        atomic_write_text(run.raw_path("patronictl_show_config.txt"), res.stdout)


def _prep_actions(kube: Kube, run: OpsRun, leader: str, db_name: str,
                  prep: dict[str, Any]) -> None:
    if prep.get("reset_checkpointer"):
        # PG17+ split the checkpointer into its own stats target; older
        # releases take 'bgwriter'. Try modern-first.
        res = kube.psql(leader, "SELECT pg_stat_reset_shared('checkpointer')")
        if not res.ok:
            res = kube.psql(leader, "SELECT pg_stat_reset_shared('bgwriter')")
        run.event("prep", "checkpointer stats reset",
                  "ok" if res.ok else f"failed: {(res.stderr or '')[:200]}")
    recreate = prep.get("recreate_db") or ""
    if recreate:
        if prep.get("confirm") != recreate:
            run.event("prep", "recreate_db refused",
                      "confirmation mismatch — type the database name")
            return
        if not _SAFE_DB_NAME.match(recreate):
            # Never interpolate an unvalidated name into DROP/CREATE DATABASE
            # (identifiers can't be parameterized) — reject anything that isn't
            # a plain PostgreSQL identifier rather than risk SQL injection.
            run.event("prep", "recreate_db refused",
                      f"'{recreate[:40]}' is not a valid database name "
                      "(letters, digits, underscore; must not start with a digit)")
            return
        ident = f'"{recreate}"'
        lit = recreate.replace("'", "''")
        kube.psql(leader,
                  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  f"WHERE datname = '{lit}' AND pid <> pg_backend_pid()",
                  database="postgres")
        r1 = kube.psql(leader, f'DROP DATABASE IF EXISTS {ident}',
                       database="postgres", timeout_s=60)
        r2 = kube.psql(leader, f'CREATE DATABASE {ident}',
                       database="postgres", timeout_s=60)
        run.event("prep", f"database '{recreate}' recreated",
                  "ok" if (r1.ok and r2.ok) else
                  f"failed: {((r1.stderr or '') + (r2.stderr or ''))[:200]}")


def _patroni_dcs_action(kube: Kube, run: OpsRun, spec: OpsSpec,
                        cr: dict[str, Any], params: dict[str, Any]) -> int:
    """Patroni DCS settings (ttl, loop_wait, synchronous_mode, postgresql.use_slots,
    standby_cluster.*, ...). Routing is per-operator: Percona v2 exposes ttl and
    loop_wait as dedicated CR fields (leaderLeaseDurationSeconds /
    syncPeriodSeconds) and merges the rest of dynamicConfiguration; Crunchy
    takes everything under dynamicConfiguration. Verified via
    ``patronictl show-config`` — the DCS document Patroni actually runs on."""
    t = spec.target
    settings = {str(k): v for k, v in dict(params.get("settings") or {}).items()}
    if not settings:
        run.finalize("failed", error="patroni_dcs: params.settings is empty")
        return EXIT_FAILED

    def coerce(v: Any) -> Any:
        sv = str(v)
        if sv.lower() in ("true", "false"):
            return sv.lower() == "true"
        try:
            return int(sv)
        except ValueError:
            try:
                # checkpoint_completion_target-style values must travel as
                # numbers, not strings, or Patroni re-validates them oddly.
                return float(sv)
            except ValueError:
                return sv

    percona = t.cr_kind == "perconapgcluster"
    spec_doc_pre = cr.get("spec") or {}
    pat_pre = spec_doc_pre.get("patroni") or {}
    dyn_pre = pat_pre.get("dynamicConfiguration") or {}

    # Patroni's own invariant: loop_wait + 2*retry_timeout <= ttl. Violating
    # it makes Patroni clamp/complain and the effective behavior diverges from
    # the staged intent — exactly what a TTL-tuning study must not have.
    # Pull current values for the unstaged members of the trio.
    if any(k in settings for k in ("ttl", "loop_wait", "retry_timeout")):
        def _eff(name: str, cr_field: str, default: float) -> Optional[float]:
            if name in settings:
                try:
                    return float(settings[name])
                except (TypeError, ValueError):
                    return None
            if percona and cr_field and pat_pre.get(cr_field) is not None:
                try:
                    return float(pat_pre[cr_field])
                except (TypeError, ValueError):
                    pass
            try:
                return float(dyn_pre.get(name, default))
            except (TypeError, ValueError):
                return default
        ttl = _eff("ttl", "leaderLeaseDurationSeconds", 30.0)
        loop_wait = _eff("loop_wait", "syncPeriodSeconds", 10.0)
        retry = _eff("retry_timeout", "", 10.0)
        if None in (ttl, loop_wait, retry):
            run.finalize("failed", error="patroni_dcs: ttl/loop_wait/"
                         "retry_timeout must be numeric")
            return EXIT_FAILED
        if loop_wait + 2 * retry > ttl:
            run.finalize("failed", error=(
                f"patroni_dcs: invariant violated — loop_wait + 2*retry_timeout "
                f"must be <= ttl (effective: loop_wait={loop_wait:g} + "
                f"2*{retry:g} = {loop_wait + 2 * retry:g} > ttl={ttl:g}). "
                "Patroni would clamp/ignore the staged values and the "
                "effective behavior would diverge from the staged intent. "
                "Stage the full trio consistently."))
            return EXIT_FAILED
    special = {"ttl": "leaderLeaseDurationSeconds",
               "loop_wait": "syncPeriodSeconds"} if percona else {}
    patch: dict[str, Any] = {}
    current: dict[str, Any] = {}
    spec_doc = cr.get("spec") or {}
    for name, value in settings.items():
        val = coerce(value)
        if name in special:
            patch.setdefault("spec", {}).setdefault("patroni", {})[special[name]] = val
            current[name] = (spec_doc.get("patroni") or {}).get(special[name])
        else:
            node = patch.setdefault("spec", {}).setdefault("patroni", {}) \
                .setdefault("dynamicConfiguration", {})
            cur_node: Any = (spec_doc.get("patroni") or {}).get("dynamicConfiguration") or {}
            parts = name.split(".")
            for seg in parts[:-1]:
                node = node.setdefault(seg, {})
                cur_node = cur_node.get(seg) or {} if isinstance(cur_node, dict) else {}
            node[parts[-1]] = val
            current[name] = cur_node.get(parts[-1]) if isinstance(cur_node, dict) else None
    changes = {k: [None if current.get(k) is None else str(current[k]), str(v)]
               for k, v in settings.items() if str(current.get(k)) != str(v)}
    atomic_write_text(run.run_dir / "patch.json", json.dumps(patch, indent=2))
    atomic_write_text(run.run_dir / "diff.json", json.dumps(
        {"action": "patroni_dcs", "current": {k: current.get(k) for k in settings},
         "proposed": settings, "changed": changes}, indent=2))
    headline: dict[str, Any] = {"action": "patroni_dcs", "changed": changes}
    if params.get("dry_run"):
        run.event("dry-run", "no changes applied", f"{len(changes)} value(s) would change")
        headline["dry_run"] = True
        run.finalize("complete", headline=headline)
        return EXIT_OK
    if not changes:
        run.event("apply", "nothing to do", "all values already live in the CR")
        run.finalize("complete", headline=headline)
        return EXIT_OK
    kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
              "-p", json.dumps(patch)], check=True)
    run.event("apply", "CR patched (patroni_dcs)",
              ", ".join(f"{k}={v[1]}" for k, v in changes.items()))
    # verify against the live DCS document (operator reconciles it into
    # patronictl edit-config; propagation takes up to a reconcile + loop_wait)
    import yaml as _yaml
    _instances, leader, _view = resolve_leader(kube, t.cr_name)
    deadline = time.monotonic() + float(params.get("verify_timeout_s", 120))
    live: dict[str, Any] = {}
    while True:
        res = kube.exec(leader, "database",
                        ["patronictl", "show-config"], timeout_s=20)
        try:
            doc = _yaml.safe_load(res.stdout) if res.ok else None
        except _yaml.YAMLError:
            doc = None
        live = {}
        if isinstance(doc, dict):
            for name in settings:
                node: Any = doc
                for seg in name.split("."):
                    node = node.get(seg) if isinstance(node, dict) else None
                live[name] = node
        ok = all(str(live.get(k)).lower() == str(coerce(v)).lower()
                 for k, v in settings.items())
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(2)
    atomic_write_text(run.run_dir / "verify.json",
                      json.dumps({"live_dcs": live, "matched": ok}, indent=2))
    headline["verified"] = ok
    if not ok:
        run.event("verify", "DCS not yet showing the new values",
                  "the CR is patched — the operator rewrites DCS on its next "
                  f"reconcile; live: {json.dumps({k: str(v) for k, v in live.items()})[:200]}")
        run.finalize("warning", headline=headline)
        return EXIT_WARNING
    run.event("verify", "confirmed in the live DCS document (patronictl show-config)")
    run.finalize("complete", headline=headline)
    return EXIT_OK


def _schedules_action(kube: Kube, run: OpsRun, spec: OpsSpec,
                      cr: dict[str, Any], action: str) -> int:
    """Pause (snapshot + remove) or restore the operator's backup schedules."""
    t = spec.target
    repos = (((cr.get("spec") or {}).get("backups") or {})
             .get("pgbackrest") or {}).get("repos") or []
    if action == "pause_schedules":
        snapshot = {r.get("name", f"repo{i+1}"): (r.get("schedules") or {})
                    for i, r in enumerate(repos)}
        atomic_write_text(run.run_dir / "schedules_snapshot.json",
                          json.dumps(snapshot, indent=2))
        new_repos = []
        for r in repos:
            r = dict(r)
            r.pop("schedules", None)
            new_repos.append(r)
        patch = _nest(("spec", "backups", "pgbackrest", "repos"), new_repos)
        kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
                  "-p", json.dumps(patch)], check=True)
        run.event("schedules", "operator backup schedules PAUSED",
                  "restore them after the test window — the UI will nag until you do")
        print(f"{SCHEDULES_MARKER} {json.dumps({'paused': True, 'snapshot': snapshot})}",
              flush=True)
        run.finalize("complete", headline={"action": action, "paused": True,
                                           "repos": list(snapshot)})
        return EXIT_OK
    # restore
    snapshot = dict(spec.params.get("snapshot") or {})
    if not snapshot:
        run.finalize("failed", error="no schedules snapshot supplied")
        return EXIT_FAILED
    new_repos = []
    for i, r in enumerate(repos):
        r = dict(r)
        name = r.get("name", f"repo{i+1}")
        if snapshot.get(name):
            r["schedules"] = snapshot[name]
        new_repos.append(r)
    patch = _nest(("spec", "backups", "pgbackrest", "repos"), new_repos)
    kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
              "-p", json.dumps(patch)], check=True)
    run.event("schedules", "operator backup schedules RESTORED", "")
    print(f"{SCHEDULES_MARKER} {json.dumps({'paused': False})}", flush=True)
    run.finalize("complete", headline={"action": action, "paused": False})
    return EXIT_OK


def run_cr_apply(spec: OpsSpec, results_dir: Path) -> int:
    t = spec.target
    params = spec.params
    action = str(params.get("action") or "patroni_params")
    run = OpsRun(results_dir, "cr-apply", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=params)
    log = run.get_logger()
    kube = Kube(context=t.context, namespace=t.namespace)
    try:
        cr = _snapshot_cr(kube, run, t.cr_kind, t.cr_name)

        if action in ("pause_schedules", "restore_schedules"):
            return _schedules_action(kube, run, spec, cr, action)
        if action == "patroni_dcs":
            return _patroni_dcs_action(kube, run, spec, cr, params)

        # Resolve what we're changing.
        if action == "rollback":
            src_dir = results_dir / "ops" / str(params.get("rollback_of") or "")
            src = read_meta(src_dir)
            if src is None:
                run.finalize("failed", error="rollback_of run not found")
                return EXIT_FAILED
            src_action = str((src.get("headline") or {}).get("action", "patroni_params"))
            changed = (src.get("headline") or {}).get("changed") or {}
            if not changed:
                # The source run died before finalize could record the diff
                # (the exact situation where rollback is needed most) — fall
                # back to diff.json, which is written BEFORE the patch.
                try:
                    d = json.loads((src_dir / "diff.json").read_text(encoding="utf-8"))
                    if str(d.get("action") or ""):
                        src_action = str(d["action"])
                    changed = d.get("changed") or {}
                    if changed:
                        run.event("rollback", "source headline empty — using "
                                  "diff.json", f"{len(changed)} value(s)")
                except (OSError, ValueError):
                    pass
            if not changed:
                run.finalize("failed", error="source run recorded no changes to roll back")
                return EXIT_FAILED
            # Old values from the source run's diff; keys that didn't exist
            # before revert to removal (None in a merge patch).
            proposed = {k: old for k, (old, _new) in changed.items()}
            action = src_action
            run.event("rollback", f"rolling back {len(proposed)} parameter(s)",
                      f"from run {params.get('rollback_of')}")
            if action == "patroni_dcs":
                # DCS settings have no removal semantics in a rollback — keys
                # that didn't exist before are skipped with a note.
                skipped = [k for k, v in proposed.items() if v is None]
                if skipped:
                    run.event("rollback", "skipping keys with no previous value",
                              ", ".join(skipped))
                settings = {k: v for k, v in proposed.items() if v is not None}
                if not settings:
                    run.finalize("failed", error="rollback: every rolled-back DCS "
                                 "key was newly added — nothing to restore")
                    return EXIT_FAILED
                return _patroni_dcs_action(kube, run, spec, cr,
                                           {**params, "settings": settings})
        elif action == "patroni_params":
            proposed = dict(params.get("parameters") or PATRONI_BUNDLE)
        elif action == "pgbackrest_global":
            proposed = dict(params.get("global") or PGBACKREST_BUNDLE)
        elif action == "pgbouncer_global":
            proposed = dict(params.get("global") or {})
            if not proposed:
                run.finalize("failed", error="pgbouncer_global: params.global is empty")
                return EXIT_FAILED
        else:
            run.finalize("failed", error=f"unknown action '{action}'")
            return EXIT_FAILED

        path = {"patroni_params": PATRONI_PARAMS_PATH,
                "pgbackrest_global": PGBACKREST_GLOBAL_PATH,
                "pgbouncer_global": PGBOUNCER_GLOBAL_PATH}[action]
        current = _dig(cr, path)

        # ── validate phase (server-side twin of the UI's checks + hazard
        # guardrails). The raw API path used to skip all of this — an unknown
        # GUC or huge_pages=on with no hugepages resources went straight into
        # the CR and took the cluster down. ──
        from pgbench_harness.ops import paramcheck
        from pgbench_harness.ops.params import classify
        rows: dict[str, dict[str, Any]] = {}
        blockers: list[str] = []
        warnings: list[str] = []
        force = bool(params.get("force"))
        if action == "patroni_params":
            fetched: Optional[dict[str, dict[str, Any]]] = None
            skip_reason = "pg_settings catalog query failed"
            try:
                _pre_i, pre_leader, _pre_v = resolve_leader(kube, t.cr_name)
                # Also fetch the hazard-context settings so shared-memory /
                # PVC arithmetic can use live values for unstaged params.
                fetched = catalog_rows(
                    kube, pre_leader,
                    [k for k, v in proposed.items() if v is not None]
                    + ["shared_buffers", "max_connections", "max_wal_size"])
            except KubeError as exc:
                # A recovery apply (e.g. huge_pages=on -> try on a
                # crash-looping cluster) must never be blocked by the outage
                # it is trying to fix — degrade to unvalidated, loudly.
                skip_reason = f"cluster unreachable ({str(exc)[:150]})"
            if fetched is None:
                run.event("validate", "pre-apply validation skipped",
                          skip_reason + " — applying unvalidated")
            else:
                rows = fetched
                locked = {}
                for name in proposed:
                    ch = classify(name, str((rows.get(name) or {}).get("context")
                                            or ""))
                    if ch in ("patroni-locked", "operator-managed", "readonly"):
                        locked[name] = ch
                b, w = paramcheck.validate_against_catalog(rows, proposed, locked)
                blockers += b
                warnings += w
                b, w = paramcheck.hazard_findings(proposed, cr, rows)
                blockers += b
                warnings += w
        elif action == "pgbackrest_global":
            warnings += paramcheck.pgbackrest_hazards(proposed)
        if blockers or warnings:
            atomic_write_text(run.run_dir / "validation.json", json.dumps(
                {"blockers": blockers, "warnings": warnings, "forced": force},
                indent=2))
        for w in warnings:
            run.event("validate", "hazard warning", w)
        if blockers:
            if force:
                run.event("validate", "hazards OVERRIDDEN (force=true)",
                          " | ".join(blockers)[:600])
            elif params.get("dry_run"):
                run.event("validate", "validation would refuse this change",
                          " | ".join(blockers)[:600])
            else:
                run.event("validate", "REFUSED by validation",
                          " | ".join(blockers)[:600])
                run.finalize("aborted",
                             headline={"action": action,
                                       "outcome": "refused by validation",
                                       "blockers": blockers},
                             error="validation refused the change: "
                                   + " | ".join(blockers)[:400])
                return EXIT_ABORTED

        changes = value_diff(current,
                             {k: v for k, v in proposed.items() if v is not None},
                             rows=rows)
        removed = [k for k, v in proposed.items() if v is None and k in current]
        patch = _nest(path, {k: (str(v) if v is not None else None)
                             for k, v in proposed.items()})
        atomic_write_text(run.run_dir / "patch.json", json.dumps(patch, indent=2))
        atomic_write_text(run.run_dir / "diff.json", json.dumps(
            {"action": action, "current": current, "proposed": proposed,
             "changed": changes, "removed": removed}, indent=2))
        log.info("planned change (%s): %d value(s) differ, %d removal(s)",
                 action, len(changes), len(removed))

        if params.get("dry_run"):
            run.event("dry-run", "no changes applied",
                      f"{len(changes)} value(s) would change")
            run.finalize("complete", headline={"action": action, "dry_run": True,
                                               "changed": changes, "removed": removed,
                                               "blockers": blockers,
                                               "warnings": warnings})
            return EXIT_OK

        if not changes and not removed:
            run.event("apply", "nothing to do", "all values already live in the CR")
            run.finalize("complete", headline={"action": action, "changed": {},
                                               "applied": False,
                                               "outcome": "no change needed"})
            return EXIT_OK

        kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
                  "-p", json.dumps(patch)], check=True)
        # Persist what was applied THE MOMENT the patch lands — if verify
        # crashes or times out, the summary card and rollback still know the
        # truth (the incident's 'Changed 0' card + unusable rollback).
        run.headline_update(action=action, changed=changes, removed=removed,
                            applied=True, outcome="applied, unverified")
        run.event("apply", f"CR patched ({action})",
                  ", ".join(f"{k}={v[1]}" for k, v in changes.items()))

        verify_timeout = float(params.get(
            "verify_timeout_s", 180 if action == "pgbouncer_global" else 60))
        rollout_timeout = float(params.get("rollout_timeout_s", 300))
        headline: dict[str, Any] = {"action": action, "changed": changes,
                                    "removed": removed, "applied": True}

        # Re-resolve the leader WITH RETRY: for restart-required params the
        # operator may already be rolling pods; transient leaderlessness
        # after a patch is expected, not a failure (A1/E4).
        try:
            instances, leader, view = resolve_leader_poll(
                kube, t.cr_name, timeout_s=rollout_timeout, run=run)
        except KubeError as exc:
            diag = verify_diagnostics(kube, t.cr_name)
            atomic_write_text(run.run_dir / "verify.json", json.dumps(
                {"matched": None, "error": str(exc)[:300],
                 "diagnostics": diag}, indent=2))
            summary = diagnostics_summary(diag)
            if diag.get("fatal_lines"):
                headline["outcome"] = "applied, rollout failed"
                run.finalize("failed", headline=headline,
                             error=f"CR patched but the cluster did not recover: "
                                   f"{exc} — {summary}")
                return EXIT_FAILED
            headline["outcome"] = "applied, verify timed out"
            run.finalize("warning", headline=headline,
                         error=f"CR patched; cluster still recovering when the "
                               f"verify window closed: {exc}"
                               + (f" — {summary}" if summary else ""))
            return EXIT_WARNING
        run.status_update(leader=leader, members=view.to_dict()["members"])

        if action == "patroni_params":
            expected = {k: v[1] for k, v in changes.items()}
            _capture_patroni_config(kube, run, leader, t.patroni_scope)
            if not expected:
                # Removal-only change (e.g. a rollback that only deletes keys):
                # there is no target value to confirm in pg_settings, so don't
                # run a vacuous verify — record the patch as applied, unverified.
                run.event("apply", "removal-only change applied",
                          f"removed {', '.join(removed)}; no live value to verify")
                headline.update({"verified": None, "pending_restart": [],
                                 "outcome": "applied (removal only)"})
                _prep_actions(kube, run, leader, t.db_name,
                              dict(params.get("prep") or {}))
                run.finalize("complete", headline=headline)
                return EXIT_OK
            live, pending, matched = verify_pg_settings(
                kube, leader, expected, verify_timeout, logger=log, rows=rows)
            atomic_write_text(run.run_dir / "verify.json", json.dumps(
                {"live": live, "pending_restart": pending, "matched": matched},
                indent=2))
            headline.update({"verified": matched, "pending_restart": pending})
            if pending:
                run.event("pending-restart",
                          f"{len(pending)} parameter(s) require a restart",
                          "the operator will roll pods to apply them — EXPECT A "
                          f"FAILOVER: {', '.join(pending)}")
                watch = params.get("watch_rollout")
                if watch is None or bool(watch):
                    # A6: don't just warn about the rollout — WATCH it. The
                    # 3-hour huge_pages outage happened entirely after a
                    # 'finished: warning' that never looked back.
                    run.status_update(phase="rollout")
                    run.event("rollout", "watching the operator's rolling restart",
                              f"leader + all members healthy + no pending "
                              f"restart, timeout {rollout_timeout:.0f}s")
                    ok, detail = await_cluster_ready(
                        kube, t.cr_name, list(expected), rollout_timeout)
                    if ok:
                        leader2 = detail
                        live2, pending2, matched2 = verify_pg_settings(
                            kube, leader2, expected,
                            min(60.0, verify_timeout), logger=log, rows=rows)
                        atomic_write_text(run.run_dir / "verify.json", json.dumps(
                            {"live": live2, "pending_restart": pending2,
                             "matched": matched2, "rollout": "converged"},
                            indent=2))
                        headline.update({"verified": matched2,
                                         "pending_restart": pending2})
                        if matched2 and not pending2:
                            headline["outcome"] = "applied+verified (after rolling restart)"
                            run.event("rollout", "rolling restart converged",
                                      f"leader {leader2}; all values live")
                            _prep_actions(kube, run, leader2, t.db_name,
                                          dict(params.get("prep") or {}))
                            run.finalize("complete", headline=headline)
                            return EXIT_OK
                        headline["outcome"] = "applied, verify failed"
                        run.event("verify", "rollout converged but values did "
                                  "not verify", json.dumps(live2)[:300])
                        run.finalize("warning", headline=headline,
                                     error="rollout converged but pg_settings "
                                           "does not show the new values")
                        return EXIT_WARNING
                    diag = verify_diagnostics(kube, t.cr_name)
                    atomic_write_text(run.run_dir / "verify.json", json.dumps(
                        {"live": live, "pending_restart": pending,
                         "matched": matched, "rollout": "timeout",
                         "rollout_detail": detail, "diagnostics": diag},
                        indent=2))
                    summary = diagnostics_summary(diag)
                    if diag.get("fatal_lines"):
                        headline["outcome"] = "applied, rollout failed"
                        run.event("rollout", "ROLLOUT FAILED — members not "
                                  "starting", summary)
                        run.finalize("failed", headline=headline,
                                     error=f"rollout failed after the patch "
                                           f"({detail}) — {summary}")
                        return EXIT_FAILED
                    headline["outcome"] = "applied, rollout not confirmed"
                    run.event("rollout", "rollout did not converge in time",
                              f"{detail}" + (f" — {summary}" if summary else ""))
                    run.finalize("warning", headline=headline,
                                 error=f"CR patched; rollout not confirmed "
                                       f"within {rollout_timeout:.0f}s ({detail})")
                    return EXIT_WARNING
                headline["outcome"] = "applied, pending restart"
                _prep_actions(kube, run, leader, t.db_name,
                              dict(params.get("prep") or {}))
                run.finalize("warning", headline=headline)
                return EXIT_WARNING
            if not matched:
                # The patch DID land (the CR carries it) — only pg_settings
                # never showed the value. That is amber, not red: 'failed'
                # invited dangerous re-runs of changes that had landed (A3).
                diag = verify_diagnostics(kube, t.cr_name)
                atomic_write_text(run.run_dir / "verify.json", json.dumps(
                    {"live": live, "pending_restart": pending,
                     "matched": matched, "diagnostics": diag}, indent=2))
                summary = diagnostics_summary(diag)
                headline["outcome"] = "applied, verify failed"
                run.event("verify", "values did not converge in pg_settings",
                          json.dumps(live)[:300]
                          + (f" — {summary}" if summary else ""))
                run.finalize("warning", headline=headline,
                             error="verify timeout: CR patched but pg_settings "
                                   "never showed the new values"
                                   + (f" — {summary}" if summary else ""))
                return EXIT_WARNING
            headline["outcome"] = "applied+verified"
            run.event("verify", "all values live in pg_settings on the leader", leader)
        elif action == "pgbouncer_global":
            expected = {k: v[1] for k, v in changes.items()}
            if not expected:
                run.event("apply", "removal-only change applied",
                          f"removed {', '.join(removed)}; no live value to verify")
                headline["verified"] = None
                run.finalize("complete", headline=headline)
                return EXIT_OK
            rendered, ok, note = verify_pgbouncer_config(kube, t.cr_name, expected,
                                                         verify_timeout)
            atomic_write_text(run.run_dir / "verify.json", json.dumps(
                {"rendered": rendered, "matched": ok, "note": note}, indent=2))
            headline["verified"] = ok
            if not ok:
                # The CR patch DID land — only the rendering wasn't observable
                # within the window (ConfigMap propagation can lag, or the pod
                # image lacks grep). Warning, not failure.
                headline["outcome"] = "applied, verify not confirmed"
                run.event("verify", "pgbouncer rendering not confirmed in time",
                          f"{note}; the CR is patched — re-check the pgBouncer "
                          "tab in a minute")
                run.finalize("warning", headline=headline)
                return EXIT_WARNING
            headline["outcome"] = "applied+verified"
            run.event("verify", note + " (SIGHUP reload; no pod restart)")
        else:   # pgbackrest_global
            expected = {k: v[1] for k, v in changes.items()}
            if not expected:
                run.event("apply", "removal-only change applied",
                          f"removed {', '.join(removed)}; no live value to verify")
                headline["verified"] = None
                run.finalize("complete", headline=headline)
                return EXIT_OK
            rendered, ok = verify_pgbackrest_config(kube, leader, expected,
                                                    verify_timeout)
            atomic_write_text(run.run_dir / "verify.json", json.dumps(
                {"rendered": rendered, "matched": ok}, indent=2))
            headline["verified"] = ok
            if not ok:
                # Same contract as the other channels: the patch landed, only
                # the rendered file didn't confirm in the window — amber.
                headline["outcome"] = "applied, verify failed"
                run.event("verify", "rendered pgbackrest config did not converge",
                          json.dumps(rendered)[:300])
                run.finalize("warning", headline=headline,
                             error="verify timeout: CR patched but "
                                   "/etc/pgbackrest never rendered the new values")
                return EXIT_WARNING
            headline["outcome"] = "applied+verified"
            run.event("verify", "values rendered in /etc/pgbackrest on the leader",
                      leader)

        _prep_actions(kube, run, leader, t.db_name, dict(params.get("prep") or {}))
        run.finalize("complete", headline=headline)
        return EXIT_OK
    except KubeError as exc:
        log.error("cr-apply failed: %s", exc)
        run.finalize("failed", error=str(exc)[:500])
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — never leave the run stuck 'running'
        log.exception("cr-apply crashed")
        run.finalize("failed", error=f"internal error: {str(exc)[:300]}")
        return EXIT_FAILED
