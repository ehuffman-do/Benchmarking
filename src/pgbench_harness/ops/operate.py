"""Day-2 operations catalog: restart, switchover, scale, resize, schedules.

One runner, five operations, one proven shape for each:

    preflight -> plan (dry-run shows the exact patch/commands + current state)
    -> execute -> live watch -> verify -> summary

Mechanics follow the research catalog (docs/research/day2-operations.md),
verified against the operators' source:

* restart   — cluster-wide: stamp ``spec.metadata.annotations`` (the operator
  performs a Patroni-coordinated rolling restart, replicas first, primary
  last); single member: ``patronictl restart <scope> <member> --force``.
  Never ``kubectl rollout restart`` (it fights the operator).
* switchover / failover — ``patronictl switchover|failover --force``
  (optionally ``--candidate <member>``): immediate, identical on Percona and
  Crunchy, and the same command the scenario runner has measured for months.
  (The CR-declarative spec.patroni.switchover block exists, but patronictl is
  synchronous and reports errors directly — better for an interactive op.)
* scale     — JSON-patch ``/spec/instances/<i>/replicas`` (precise on list
  indices; a merge patch would replace the whole instances array). Scale-down
  WARNS: the operator deletes the removed members' PVCs.
* resize    — JSON-patch ``/spec/instances/<i>/resources``. Preflight compares
  the new memory limit against shared_buffers on the leader — the classic
  OOM-loop cause. Rolling recreate, primary last (one switchover).
* schedules — repo backup crons + retention: JSON-patch
  ``/spec/backups/pgbackrest/repos/<i>/schedules`` + merge-patch retention
  keys into the pgBackRest global map.

Exit codes: 0 ok, 1 completed-with-warnings, 3 failed, 4 preflight refused.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.ops import patroni
from pgbench_harness.ops.crconfig import resolve_leader, _snapshot_cr
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.oprun import (EXIT_ABORTED, EXIT_FAILED, EXIT_OK,
                                       EXIT_WARNING, OpsRun)
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.util import atomic_write_text

OPERATIONS = ("restart", "switchover", "failover", "scale", "resize", "schedules")

_CRON_RE = re.compile(r"^\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*$")   # 5 fields
_QTY_RE = re.compile(r"^\d+(\.\d+)?(m|Mi|Gi|Ki|G|M|k)?$")


def _pods_view(kube: Kube, cr_name: str) -> dict[str, Any]:
    from pgbench_harness.ops.discover import classify_pods
    items = kube.json(["get", "pods"]).get("items") or []
    return classify_pods(items, cr_name)


def _watch_until(run: OpsRun, what: str, check, timeout_s: float,
                 poll_s: float = 2.0) -> bool:
    """Poll ``check()`` (returns (done, detail)) until done or timeout,
    streaming progress into status.json for the live cockpit."""
    deadline = time.monotonic() + timeout_s
    last_detail = ""
    while time.monotonic() < deadline:
        try:
            done, detail = check()
        except KubeError as exc:
            done, detail = False, f"(transient) {str(exc)[:120]}"
        if detail != last_detail:
            run.status_update(phase=what, detail=detail)
            last_detail = detail
        if done:
            return True
        time.sleep(poll_s)
    run.event("verify", f"TIMEOUT waiting for {what}", last_detail)
    return False


def _members_settled(kube: Kube, leader_pod: str,
                     expect_n: Optional[int] = None) -> tuple[bool, str]:
    view = patroni.fetch_view(kube, leader_pod)
    states = {m.name: (m.state or "").lower() for m in view.members}
    healthy = all(s in ("running", "streaming") for s in states.values())
    n_ok = expect_n is None or len(view.members) == expect_n
    detail = f"{len(view.members)} members: " + ", ".join(
        f"{n.rsplit('-', 2)[-2]}-{n.rsplit('-', 2)[-1]}={s}" for n, s in states.items())
    return (healthy and n_ok and bool(view.leader_name)), detail


def _any_running_instance(kube: Kube, cr_name: str) -> Optional[str]:
    pods = _pods_view(kube, cr_name)
    for p in pods["instances"]:
        if p["phase"] == "Running":
            return str(p["name"])
    return None


# ── the operations ──

def _op_restart(kube: Kube, run: OpsRun, spec: OpsSpec, params: dict[str, Any],
                dry_run: bool) -> int:
    t = spec.target
    scope = str(params.get("scope") or "cluster")
    timeout_s = float(params.get("timeout_s") or 600)
    instances, leader, view = resolve_leader(kube, t.cr_name)
    tl_before = view.timeline

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    patch = {"spec": {"metadata": {"annotations": {
        "pgbench-harness/restartedAt": stamp}}}}
    if scope == "cluster":
        plan = (f"kubectl patch {t.cr_kind} {t.cr_name} --type merge -p "
                f"'{json.dumps(patch)}'")
    else:
        if scope not in instances:
            run.event("preflight", f"ABORT: '{scope}' is not a running instance pod",
                      f"members: {', '.join(instances)}")
            run.finalize("aborted", headline={"operation": "restart",
                                              "reason": "unknown-member"})
            return EXIT_ABORTED
        plan = (f"kubectl exec {leader} -c database -- patronictl restart "
                f"{t.patroni_scope} {scope} --force")
    run.event("plan", f"restart {scope}", plan)
    if scope == "cluster":
        run.event("plan", "operator behavior",
                  "Patroni-coordinated rolling restart: replicas first, "
                  "primary last (expect one switchover-like blip)")
    if dry_run:
        run.finalize("complete", headline={"operation": "restart", "dry_run": True,
                                           "scope": scope, "plan": plan})
        return EXIT_OK

    if scope == "cluster":
        kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
                  "-p", json.dumps(patch)], check=True)
        run.event("fire", "restart annotation stamped", stamp)
    else:
        res = kube.exec(leader, "database",
                        ["patronictl", "restart", t.patroni_scope, scope,
                         "--force"], timeout_s=120)
        if not res.ok:
            run.event("fire", "patronictl restart failed",
                      (res.stderr or res.stdout).strip()[:300])
            run.finalize("failed", headline={"operation": "restart", "scope": scope})
            return EXIT_FAILED
        run.event("fire", f"member {scope} restarted via patronictl")

    # settle: wait for every member back to running/streaming with a leader
    time.sleep(float(params.get("settle_grace_s", 3)))
    probe = _any_running_instance(kube, t.cr_name) or leader
    ok = _watch_until(run, "waiting for members to settle",
                      lambda: _members_settled(kube,
                                               _any_running_instance(kube, t.cr_name) or probe,
                                               expect_n=len(instances)),
                      timeout_s)
    view_after = patroni.fetch_view(kube, _any_running_instance(kube, t.cr_name) or probe)
    headline = {"operation": "restart", "scope": scope,
                "leader_before": leader, "leader_after": view_after.leader_name,
                "tl_before": tl_before, "tl_after": view_after.timeline,
                "settled": ok}
    run.event("verify", "cluster settled" if ok else "cluster NOT settled in time",
              f"leader {view_after.leader_name}, TL {view_after.timeline}")
    run.finalize("complete" if ok else "warning", headline=headline)
    return EXIT_OK if ok else EXIT_WARNING


def _op_switchover(kube: Kube, run: OpsRun, spec: OpsSpec, params: dict[str, Any],
                   dry_run: bool, failover: bool) -> int:
    t = spec.target
    target = str(params.get("target") or "")
    timeout_s = float(params.get("timeout_s") or 300)
    instances, leader, view = resolve_leader(kube, t.cr_name)
    tl_before = view.timeline
    verb = "failover" if failover else "switchover"

    if failover and not target:
        # patronictl (3.x+) refuses a forced failover without a candidate;
        # catch it in preflight instead of dying mid-run.
        run.event("preflight", "ABORT: failover requires an explicit target member",
                  "pick the replica to promote (switchover can auto-select)")
        run.finalize("aborted", headline={"operation": verb,
                                          "reason": "failover-needs-target"})
        return EXIT_ABORTED
    if target and target == leader:
        run.event("preflight", "ABORT: target is already the leader", target)
        run.finalize("aborted", headline={"operation": verb, "reason": "target-is-leader"})
        return EXIT_ABORTED
    if target and target not in instances:
        run.event("preflight", f"ABORT: '{target}' is not a running instance pod",
                  f"members: {', '.join(instances)}")
        run.finalize("aborted", headline={"operation": verb, "reason": "unknown-member"})
        return EXIT_ABORTED
    replicas_ok = [m for m in view.members
                   if m.name != leader and (m.state or "").lower() == "streaming"]
    if not replicas_ok:
        run.event("preflight", "ABORT: no streaming replica to promote")
        run.finalize("aborted", headline={"operation": verb, "reason": "no-streaming-replica"})
        return EXIT_ABORTED

    argv = ["patronictl", verb, t.patroni_scope, "--force"]
    if target:
        argv += ["--candidate", target]
    plan = f"kubectl exec {leader} -c database -- {' '.join(argv)}"
    run.event("plan", f"{verb} (leader {leader} → {target or 'best replica'})", plan)
    if dry_run:
        run.finalize("complete", headline={"operation": verb, "dry_run": True,
                                           "plan": plan})
        return EXIT_OK

    res = kube.exec(leader, "database", argv, timeout_s=120)
    atomic_write_text(run.raw_path(f"{verb}_output.txt"), res.stdout + res.stderr)
    if not res.ok:
        run.event("fire", f"patronictl {verb} failed",
                  (res.stderr or res.stdout).strip()[:300])
        run.finalize("failed", headline={"operation": verb})
        return EXIT_FAILED
    run.event("fire", f"{verb} requested", res.stdout.strip()[:200])

    def check() -> tuple[bool, str]:
        pod = _any_running_instance(kube, t.cr_name) or leader
        v = patroni.fetch_view(kube, pod)
        new_leader = v.leader_name or ""
        done = bool(new_leader) and new_leader != leader and \
            (not target or new_leader == target)
        return done, f"leader {new_leader or '(none)'} TL {v.timeline}"

    ok = _watch_until(run, "waiting for the new leader", check, timeout_s)
    pod = _any_running_instance(kube, t.cr_name) or leader
    after = patroni.fetch_view(kube, pod)
    settled = _watch_until(run, "waiting for replicas to re-stream",
                           lambda: _members_settled(kube, pod, expect_n=len(instances)),
                           timeout_s)
    headline = {"operation": verb, "leader_before": leader,
                "leader_after": after.leader_name, "target": target or None,
                "tl_before": tl_before, "tl_after": after.timeline,
                "flipped": after.leader_name != leader, "settled": settled}
    run.event("verify", f"leader {after.leader_name}, TL {tl_before} → {after.timeline}")
    run.finalize("complete" if ok and settled else "warning", headline=headline)
    return EXIT_OK if ok and settled else EXIT_WARNING


def _op_scale(kube: Kube, run: OpsRun, spec: OpsSpec, params: dict[str, Any],
              dry_run: bool) -> int:
    t = spec.target
    idx = int(params.get("instance_set") or 0)
    replicas = int(params["replicas"])
    timeout_s = float(params.get("timeout_s") or 900)

    cr = kube.cluster_cr(t.cr_kind, t.cr_name)
    sets = (cr.get("spec") or {}).get("instances") or []
    if not 0 <= idx < len(sets):
        run.event("preflight", f"ABORT: instance set index {idx} out of range",
                  f"{len(sets)} set(s) in the CR")
        run.finalize("aborted", headline={"operation": "scale", "reason": "bad-index"})
        return EXIT_ABORTED
    current = int(sets[idx].get("replicas") or 1)
    if replicas == current:
        run.event("preflight", f"ABORT: already at {current} replicas")
        run.finalize("aborted", headline={"operation": "scale", "reason": "no-change"})
        return EXIT_ABORTED
    if replicas < current:
        run.event("preflight", f"scale DOWN {current} → {replicas}",
                  "the operator DELETES the removed members' PVCs — their local "
                  "data copies are gone (repo backups are unaffected)")
    patch = [{"op": "replace", "path": f"/spec/instances/{idx}/replicas",
              "value": replicas}]
    plan = (f"kubectl patch {t.cr_kind} {t.cr_name} --type json -p "
            f"'{json.dumps(patch)}'")
    run.event("plan", f"scale instance set {idx}: {current} → {replicas}", plan)
    if dry_run:
        run.finalize("complete", headline={"operation": "scale", "dry_run": True,
                                           "from": current, "to": replicas})
        return EXIT_OK

    _snapshot_cr(kube, run, t.cr_kind, t.cr_name)
    kube.run(["patch", t.cr_kind, t.cr_name, "--type", "json",
              "-p", json.dumps(patch)], check=True)
    run.event("fire", "replicas patched")

    def check() -> tuple[bool, str]:
        pods = _pods_view(kube, t.cr_name)
        inst = pods["instances"]
        running = [p for p in inst if p["phase"] == "Running"]
        if len(inst) != replicas or len(running) != replicas:
            return False, f"{len(running)}/{replicas} instance pods running"
        return _members_settled(kube, running[0]["name"], expect_n=replicas)

    ok = _watch_until(run, "waiting for the new member set", check, timeout_s)
    headline = {"operation": "scale", "from": current, "to": replicas, "settled": ok}
    run.finalize("complete" if ok else "warning", headline=headline)
    return EXIT_OK if ok else EXIT_WARNING


def _op_resize(kube: Kube, run: OpsRun, spec: OpsSpec, params: dict[str, Any],
               dry_run: bool) -> int:
    t = spec.target
    idx = int(params.get("instance_set") or 0)
    resources = params.get("resources") or {}
    timeout_s = float(params.get("timeout_s") or 900)
    for section in ("requests", "limits"):
        for key, val in (resources.get(section) or {}).items():
            if key not in ("cpu", "memory") or not _QTY_RE.match(str(val)):
                run.event("preflight", f"ABORT: bad quantity {section}.{key}={val}")
                run.finalize("aborted", headline={"operation": "resize",
                                                  "reason": "bad-quantity"})
                return EXIT_ABORTED

    cr = kube.cluster_cr(t.cr_kind, t.cr_name)
    sets = (cr.get("spec") or {}).get("instances") or []
    if not 0 <= idx < len(sets):
        run.event("preflight", f"ABORT: instance set index {idx} out of range")
        run.finalize("aborted", headline={"operation": "resize", "reason": "bad-index"})
        return EXIT_ABORTED
    current = sets[idx].get("resources") or {}

    # The classic OOM loop: memory limit below what shared_buffers needs.
    mem_limit = str(((resources.get("limits") or {}).get("memory")) or "")
    if mem_limit:
        try:
            _instances, leader, _view = resolve_leader(kube, t.cr_name)
            res = kube.psql(leader, "/*op:shared_buffers*/ SELECT setting::bigint * 8192 "
                                    "FROM pg_settings WHERE name='shared_buffers'")
            sb_bytes = int(res.stdout.strip() or 0) if res.ok else 0
            m = re.match(r"^(\d+(?:\.\d+)?)(Mi|Gi)$", mem_limit)
            lim_bytes = int(float(m.group(1)) * (1 << 20 if m.group(2) == "Mi" else 1 << 30)) if m else 0
            if sb_bytes and lim_bytes and lim_bytes < 2 * sb_bytes:
                run.event("preflight",
                          f"WARNING: memory limit {mem_limit} < 2× shared_buffers "
                          f"({sb_bytes / (1 << 30):.1f} GiB)",
                          "Postgres needs headroom beyond shared_buffers "
                          "(work_mem, connections) — this risks OOMKilled loops. "
                          "Lower shared_buffers first or raise the limit.")
        except (KubeError, ValueError):
            pass

    patch = [{"op": "add" if not current else "replace",
              "path": f"/spec/instances/{idx}/resources", "value": resources}]
    plan = (f"kubectl patch {t.cr_kind} {t.cr_name} --type json -p "
            f"'{json.dumps(patch)}'")
    run.event("plan", f"resize instance set {idx}", plan)
    run.event("plan", "current resources", json.dumps(current) or "(unset)")
    run.event("plan", "operator behavior",
              "rolling pod recreate, primary last — expect one switchover blip")
    if dry_run:
        run.finalize("complete", headline={"operation": "resize", "dry_run": True,
                                           "resources": resources})
        return EXIT_OK

    _snapshot_cr(kube, run, t.cr_kind, t.cr_name)
    kube.run(["patch", t.cr_kind, t.cr_name, "--type", "json",
              "-p", json.dumps(patch)], check=True)
    run.event("fire", "resources patched")

    n = len([p for p in _pods_view(kube, t.cr_name)["instances"]])

    def check() -> tuple[bool, str]:
        pods = _pods_view(kube, t.cr_name)["instances"]
        running = [p for p in pods if p["phase"] == "Running"]
        if len(running) < n:
            return False, f"{len(running)}/{n} pods running (rolling)"
        return _members_settled(kube, running[0]["name"], expect_n=n)

    ok = _watch_until(run, "waiting for the rolling recreate", check, timeout_s)
    headline = {"operation": "resize", "resources": resources, "settled": ok}
    run.finalize("complete" if ok else "warning", headline=headline)
    return EXIT_OK if ok else EXIT_WARNING


def _op_schedules(kube: Kube, run: OpsRun, spec: OpsSpec, params: dict[str, Any],
                  dry_run: bool) -> int:
    t = spec.target
    repo_name = str(params.get("repo") or "repo1")
    schedules = params.get("schedules")          # {full/differential/incremental: cron|null}
    retention = params.get("retention") or {}    # repoN-retention-* -> value

    for key, cron in (schedules or {}).items():
        if key not in ("full", "differential", "incremental"):
            run.event("preflight", f"ABORT: unknown schedule kind '{key}'")
            run.finalize("aborted", headline={"operation": "schedules",
                                              "reason": "bad-kind"})
            return EXIT_ABORTED
        if cron is not None and not _CRON_RE.match(str(cron)):
            run.event("preflight", f"ABORT: '{cron}' is not a 5-field cron expression")
            run.finalize("aborted", headline={"operation": "schedules",
                                              "reason": "bad-cron"})
            return EXIT_ABORTED
    for key in retention:
        if not re.match(r"^repo\d-retention-[a-z-]+$", str(key)):
            run.event("preflight", f"ABORT: '{key}' is not a repoN-retention-* option")
            run.finalize("aborted", headline={"operation": "schedules",
                                              "reason": "bad-retention-key"})
            return EXIT_ABORTED

    cr = kube.cluster_cr(t.cr_kind, t.cr_name)
    repos = (((cr.get("spec") or {}).get("backups") or {})
             .get("pgbackrest") or {}).get("repos") or []
    ridx = next((i for i, r in enumerate(repos)
                 if (r or {}).get("name") == repo_name), None)
    if schedules is not None and ridx is None:
        run.event("preflight", f"ABORT: repo '{repo_name}' not found in the CR",
                  f"repos: {', '.join((r or {}).get('name', '?') for r in repos)}")
        run.finalize("aborted", headline={"operation": "schedules",
                                          "reason": "unknown-repo"})
        return EXIT_ABORTED

    plans = []
    if schedules is not None:
        cur = dict((repos[ridx] or {}).get("schedules") or {})
        merged = dict(cur)
        for k, v in schedules.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = str(v)
        sched_patch = [{"op": "add", "path":
                        f"/spec/backups/pgbackrest/repos/{ridx}/schedules",
                        "value": merged}]
        plans.append(f"kubectl patch --type json -p '{json.dumps(sched_patch)}'")
        run.event("plan", f"schedules on {repo_name}",
                  json.dumps({"before": cur, "after": merged}))
    if retention:
        ret_patch = {"spec": {"backups": {"pgbackrest": {"global": {
            k: str(v) for k, v in retention.items()}}}}}
        plans.append(f"kubectl patch --type merge -p '{json.dumps(ret_patch)}'")
        run.event("plan", "retention (pgBackRest global)", json.dumps(retention))
    if not plans:
        run.event("preflight", "ABORT: nothing to change")
        run.finalize("aborted", headline={"operation": "schedules", "reason": "no-op"})
        return EXIT_ABORTED
    if dry_run:
        run.finalize("complete", headline={"operation": "schedules", "dry_run": True,
                                           "plans": len(plans)})
        return EXIT_OK

    _snapshot_cr(kube, run, t.cr_kind, t.cr_name)
    if schedules is not None:
        kube.run(["patch", t.cr_kind, t.cr_name, "--type", "json",
                  "-p", json.dumps(sched_patch)], check=True)
    if retention:
        kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
                  "-p", json.dumps(ret_patch)], check=True)
    run.event("fire", "CR patched")

    # verify: read back the CR and confirm the values landed
    after = kube.cluster_cr(t.cr_kind, t.cr_name)
    pgb = (((after.get("spec") or {}).get("backups") or {}).get("pgbackrest") or {})
    ok = True
    if schedules is not None:
        live = dict(((pgb.get("repos") or [{}])[ridx] or {}).get("schedules") or {})
        ok = ok and live == merged
        run.event("verify", f"schedules now {json.dumps(live)}")
    for k, v in retention.items():
        got = str((pgb.get("global") or {}).get(k, ""))
        ok = ok and got == str(v)
        run.event("verify", f"{k} = {got or '(unset)'}")
    headline = {"operation": "schedules", "repo": repo_name,
                "schedules": schedules, "retention": retention or None,
                "verified": ok}
    run.finalize("complete" if ok else "warning", headline=headline)
    return EXIT_OK if ok else EXIT_WARNING


def run_operate(spec: OpsSpec, results_dir: Path) -> int:
    t = spec.target
    params = dict(spec.params)
    operation = str(params.get("operation") or "")
    dry_run = bool(params.get("dry_run"))
    run = OpsRun(results_dir, "operate", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=params)
    kube = Kube(context=t.context, namespace=t.namespace)
    run.status_update(phase="preflight", operation=operation)
    try:
        if operation == "restart":
            return _op_restart(kube, run, spec, params, dry_run)
        if operation in ("switchover", "failover"):
            return _op_switchover(kube, run, spec, params, dry_run,
                                  failover=(operation == "failover"))
        if operation == "scale":
            return _op_scale(kube, run, spec, params, dry_run)
        if operation == "resize":
            return _op_resize(kube, run, spec, params, dry_run)
        if operation == "schedules":
            return _op_schedules(kube, run, spec, params, dry_run)
        run.event("preflight", f"unknown operation '{operation}'")
        run.finalize("failed", headline={"operation": operation,
                                         "error": "unknown-operation"})
        return EXIT_FAILED
    except KubeError as exc:
        run.event("error", "kubectl error", str(exc)[:300])
        run.finalize("failed", headline={"operation": operation},
                     error=str(exc)[:300])
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — the run must always finalize
        run.event("error", "unexpected error", str(exc)[:300])
        run.finalize("failed", headline={"operation": operation},
                     error=str(exc)[:200])
        return EXIT_FAILED
