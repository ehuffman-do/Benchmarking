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
from pgbench_harness.ops.oprun import (EXIT_FAILED, EXIT_OK, EXIT_WARNING,
                                       OpsRun, read_meta)
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


def value_diff(current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, list]:
    """Per-key [old, new] for keys whose value would change (all values as str)."""
    changes: dict[str, list] = {}
    for key, new in proposed.items():
        old = current.get(key)
        if old is None or str(old) != str(new):
            changes[key] = [None if old is None else str(old), str(new)]
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
                       logger: Any = None) -> tuple[dict[str, str], list[str], bool]:
    """Poll pg_settings on the leader until every expected value is live.

    Returns (live values, pending_restart names, all_matched)."""
    if not expected:
        # Nothing with a target value to confirm (e.g. a removal-only change).
        # Return not-matched so the caller never reports a vacuous "verified".
        return {}, [], False
    # pg_settings names are validated GUC identifiers, but guard the interpolation
    # anyway — only word-characters can reach the IN () list.
    safe = [k for k in expected if re.match(r"^[A-Za-z0-9_.]+$", str(k))]
    names = ",".join(f"'{k}'" for k in safe)
    sql = (f"SELECT name, setting, unit, pending_restart FROM pg_settings "
           f"WHERE name IN ({names}) ORDER BY name")
    deadline = time.monotonic() + timeout_s
    live: dict[str, str] = {}
    pending: list[str] = []
    while True:
        res = kube.psql(leader, sql)
        live, pending = {}, []
        if res.ok:
            for line in res.stdout.splitlines():
                parts = line.split("|")
                if len(parts) >= 4 and parts[0] in expected:
                    live[parts[0]] = parts[1]
                    if parts[3].strip() == "t":
                        pending.append(parts[0])
            matched = all(live.get(k) == str(v) for k, v in expected.items())
            # pending_restart params will NEVER converge without a restart —
            # stop polling for them, surface loudly instead.
            unconverged = [k for k, v in expected.items() if live.get(k) != str(v)]
            if matched or all(k in pending for k in unconverged):
                return live, pending, matched
        if time.monotonic() >= deadline:
            return live, pending, all(live.get(k) == str(v) for k, v in expected.items())
        if logger:
            logger.info("verify: waiting for pg_settings to converge "
                        "(%s pending)", len(expected) - sum(
                            1 for k, v in expected.items() if live.get(k) == str(v)))
        time.sleep(poll_s)


def verify_pgbackrest_config(kube: Kube, leader: str, expected: dict[str, Any],
                             timeout_s: float, poll_s: float = 2.0) -> tuple[dict[str, str], bool]:
    """CR -> rendered config in the pod: grep /etc/pgbackrest for the keys."""
    pattern = "|".join(expected)
    deadline = time.monotonic() + timeout_s
    rendered: dict[str, str] = {}
    while True:
        res = kube.exec(leader, "database",
                        ["grep", "-rE", pattern, "/etc/pgbackrest/"], timeout_s=20)
        rendered = {}
        for line in res.stdout.splitlines():
            body = line.split(":", 1)[-1] if ":" in line else line
            if "=" in body:
                k, v = body.split("=", 1)
                rendered[k.strip()] = v.strip()
        ok = all(rendered.get(k) == str(v) for k, v in expected.items())
        if ok or time.monotonic() >= deadline:
            return rendered, ok
        time.sleep(poll_s)


def _parse_ini_pairs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[", "%")):
            continue
        if "=" in stripped:
            k, v = stripped.split("=", 1)
            out[k.strip()] = v.strip()
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

    Returns (rendered, matched, note)."""
    from pgbench_harness.ops.discover import classify_pods
    pattern = "|".join(expected)
    deadline = time.monotonic() + timeout_s
    rendered: dict[str, str] = {}
    note = ""
    while True:
        # 1) ConfigMap (source of truth for what the operator rendered)
        try:
            doc = kube.json(["get", "configmaps"])
            for item in doc.get("items") or []:
                name = str(item.get("metadata", {}).get("name", ""))
                if cr_name not in name or "pgbouncer" not in name.lower():
                    continue
                for text in (item.get("data") or {}).values():
                    rendered.update({k: v for k, v in _parse_ini_pairs(str(text)).items()
                                     if k in expected})
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
                    for line in res.stdout.splitlines():
                        if "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() in expected:
                                rendered[k.strip()] = v.strip()
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

        # Resolve what we're changing.
        if action == "rollback":
            src = read_meta(results_dir / "ops" / str(params.get("rollback_of") or ""))
            if src is None:
                run.finalize("failed", error="rollback_of run not found")
                return EXIT_FAILED
            src_action = str((src.get("headline") or {}).get("action", "patroni_params"))
            changed = (src.get("headline") or {}).get("changed") or {}
            if not changed:
                run.finalize("failed", error="source run recorded no changes to roll back")
                return EXIT_FAILED
            # Old values from the source run's diff; keys that didn't exist
            # before revert to removal (None in a merge patch).
            proposed = {k: old for k, (old, _new) in changed.items()}
            action = src_action
            run.event("rollback", f"rolling back {len(proposed)} parameter(s)",
                      f"from run {params.get('rollback_of')}")
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
        changes = value_diff(current, {k: v for k, v in proposed.items() if v is not None})
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
                                               "changed": changes, "removed": removed})
            return EXIT_OK

        if not changes and not removed:
            run.event("apply", "nothing to do", "all values already live in the CR")
            run.finalize("complete", headline={"action": action, "changed": {},
                                               "applied": False})
            return EXIT_OK

        kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
                  "-p", json.dumps(patch)], check=True)
        run.event("apply", f"CR patched ({action})",
                  ", ".join(f"{k}={v[1]}" for k, v in changes.items()))

        # Verify loop.
        instances, leader, view = resolve_leader(kube, t.cr_name)
        run.status_update(leader=leader, members=view.to_dict()["members"])
        verify_timeout = float(params.get(
            "verify_timeout_s", 180 if action == "pgbouncer_global" else 60))
        headline: dict[str, Any] = {"action": action, "changed": changes,
                                    "removed": removed, "applied": True}
        if action == "patroni_params":
            expected = {k: v[1] for k, v in changes.items()}
            _capture_patroni_config(kube, run, leader, t.patroni_scope)
            if not expected:
                # Removal-only change (e.g. a rollback that only deletes keys):
                # there is no target value to confirm in pg_settings, so don't
                # run a vacuous verify — record the patch as applied, unverified.
                run.event("apply", "removal-only change applied",
                          f"removed {', '.join(removed)}; no live value to verify")
                headline.update({"verified": None, "pending_restart": []})
                _prep_actions(kube, run, leader, t.db_name,
                              dict(params.get("prep") or {}))
                run.finalize("complete", headline=headline)
                return EXIT_OK
            live, pending, matched = verify_pg_settings(
                kube, leader, expected, verify_timeout, logger=log)
            atomic_write_text(run.run_dir / "verify.json", json.dumps(
                {"live": live, "pending_restart": pending, "matched": matched},
                indent=2))
            headline.update({"verified": matched, "pending_restart": pending})
            if pending:
                run.event("pending-restart",
                          f"{len(pending)} parameter(s) require a restart",
                          "the operator will roll pods to apply them — EXPECT A "
                          f"FAILOVER: {', '.join(pending)}")
                _prep_actions(kube, run, leader, t.db_name,
                              dict(params.get("prep") or {}))
                run.finalize("warning", headline=headline)
                return EXIT_WARNING
            if not matched:
                run.event("verify", "values did not converge in pg_settings",
                          json.dumps(live)[:300])
                run.finalize("failed", headline=headline,
                             error="verify timeout: CR patched but pg_settings "
                                   "never showed the new values")
                return EXIT_FAILED
            run.event("verify", "all values live in pg_settings on the leader", leader)
        elif action == "pgbouncer_global":
            expected = {k: v[1] for k, v in changes.items()}
            rendered, ok, note = verify_pgbouncer_config(kube, t.cr_name, expected,
                                                         verify_timeout)
            atomic_write_text(run.run_dir / "verify.json", json.dumps(
                {"rendered": rendered, "matched": ok, "note": note}, indent=2))
            headline["verified"] = ok
            if not ok:
                # The CR patch DID land — only the rendering wasn't observable
                # within the window (ConfigMap propagation can lag, or the pod
                # image lacks grep). Warning, not failure.
                run.event("verify", "pgbouncer rendering not confirmed in time",
                          f"{note}; the CR is patched — re-check the pgBouncer "
                          "tab in a minute")
                run.finalize("warning", headline=headline)
                return EXIT_WARNING
            run.event("verify", note + " (SIGHUP reload; no pod restart)")
        else:   # pgbackrest_global
            expected = {k: v[1] for k, v in changes.items()}
            rendered, ok = verify_pgbackrest_config(kube, leader, expected,
                                                    verify_timeout)
            atomic_write_text(run.run_dir / "verify.json", json.dumps(
                {"rendered": rendered, "matched": ok}, indent=2))
            headline["verified"] = ok
            if not ok:
                run.event("verify", "rendered pgbackrest config did not converge",
                          json.dumps(rendered)[:300])
                run.finalize("failed", headline=headline,
                             error="verify timeout: /etc/pgbackrest never rendered "
                                   "the new values")
                return EXIT_FAILED
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
