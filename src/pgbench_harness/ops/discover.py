"""Topology discovery — read-only snapshot of the cluster's moving parts.

Streams progress as JSON check lines (same contract as validate) and finishes
with ``OPS_TOPOLOGY_JSON {...}`` — the worker caches that document onto the
Kube Target row so the UI's topology panel has an instant last-known view.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pgbench_harness.ops import patroni
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.opspec import CR_KINDS, OpsSpec
from pgbench_harness.util import utc_now_iso

TOPOLOGY_MARKER = "OPS_TOPOLOGY_JSON"


def _check(name: str, status: str, detail: str = "") -> None:
    print(json.dumps({"name": name, "status": status, "detail": detail}), flush=True)


def _pod_summary(item: dict[str, Any]) -> dict[str, Any]:
    meta, status, spec = item.get("metadata", {}), item.get("status", {}), item.get("spec", {})
    cs = status.get("containerStatuses") or []
    ready = sum(1 for c in cs if c.get("ready"))
    return {"name": meta.get("name", ""), "phase": status.get("phase", ""),
            "ready": f"{ready}/{len(cs)}" if cs else "0/0",
            "node": spec.get("nodeName", ""), "pod_ip": status.get("podIP", ""),
            "labels": meta.get("labels", {}) or {},
            "containers": [c.get("name", "") for c in spec.get("containers", [])]}


def classify_pods(items: list[dict[str, Any]], cr_name: str) -> dict[str, list[dict[str, Any]]]:
    """Split pods into instance / pgbouncer / backup-job / other buckets.

    Instance pods are identified by carrying a ``database`` container (both
    Percona and Crunchy layouts) — label schemes differ between the two
    operators, container layout does not.
    """
    out: dict[str, list[dict[str, Any]]] = {"instances": [], "pgbouncer": [],
                                            "backup_jobs": [], "other": []}
    for item in items:
        pod = _pod_summary(item)
        name = pod["name"]
        if cr_name and not name.startswith(cr_name):
            out["other"].append(pod)
        elif "database" in pod["containers"]:
            out["instances"].append(pod)
        elif "pgbouncer" in name or "pgbouncer" in pod["containers"]:
            out["pgbouncer"].append(pod)
        elif ("backup" in name or "pgbackrest" in name) \
                and "database" not in pod["containers"]:
            out["backup_jobs"].append(pod)
        else:
            out["other"].append(pod)
    return out


# The operator stamps the primary's pod with Patroni's role label. Patroni <4
# writes "master", Patroni 4+ writes "primary" — accept both.
ROLE_LABEL = "postgres-operator.crunchydata.com/role"
LEADER_ROLES = ("master", "primary")


def leader_by_label(pods: list[dict[str, Any]]) -> Optional[str]:
    """Leader pod name from the operator's role label — no exec needed."""
    for pod in pods:
        if str((pod.get("labels") or {}).get(ROLE_LABEL, "")).lower() in LEADER_ROLES \
                and pod.get("phase") == "Running":
            return str(pod["name"])
    return None


def resolve_leader_resilient(kube: Any, cr_name: str, timeout_s: float = 120,
                             poll_s: float = 5.0,
                             notify=None) -> tuple[list[str], str, Any, list[str]]:
    """Leader discovery that survives mid-roll elections and dying exec targets.

    Strategy per attempt: (1) the operator's role label on pods (no exec at
    all); (2) ``patronictl list`` tried against EVERY running instance pod,
    not just the first (a terminating pod fails exec — that is retryable, not
    fatal). "No leader right now" (election window) is also retryable. Only
    the deadline fails, and the error carries what each attempt observed.

    Returns (instance pod names, leader pod, PatroniView | None, attempts).
    """
    from pgbench_harness.ops import patroni as _patroni
    from pgbench_harness.ops.kube import KubeError
    import time as _time
    attempts: list[str] = []
    deadline = _time.monotonic() + timeout_s
    while True:
        stamp = f"t+{timeout_s - max(0, deadline - _time.monotonic()):.0f}s"
        try:
            items = kube.json(["get", "pods"]).get("items") or []
        except KubeError as exc:
            attempts.append(f"{stamp}: pod list failed: {str(exc)[:80]}")
            items = []
        buckets = classify_pods(items, cr_name)
        instances = [p["name"] for p in buckets["instances"]]
        running = [p["name"] for p in buckets["instances"] if p["phase"] == "Running"]
        leader = leader_by_label(buckets["instances"])
        view = None
        if leader:
            try:                       # members/TL are nice-to-have, not required
                view = _patroni.fetch_view(kube, leader)
            except KubeError:
                view = None
            attempts.append(f"{stamp}: leader '{leader}' via role label")
            return instances, leader, view, attempts
        for pod in running:            # every pod is a candidate exec target
            try:
                view = _patroni.fetch_view(kube, pod)
            except KubeError as exc:
                attempts.append(f"{stamp}: exec {pod}: {str(exc)[:80]}")
                continue
            if view.leader_name:
                attempts.append(f"{stamp}: leader '{view.leader_name}' via "
                                f"patronictl on {pod}")
                return instances, view.leader_name, view, attempts
            attempts.append(f"{stamp}: {pod}: patroni reports no leader "
                            "(election window?)")
            break                      # one good view without a leader is enough
        if not running:
            attempts.append(f"{stamp}: no running instance pods "
                            f"({len(instances)} exist)")
        if notify is not None:
            notify(attempts[-1] if attempts else stamp)
        if _time.monotonic() >= deadline:
            from pgbench_harness.ops.kube import KubeError as _KE
            raise _KE("leader discovery failed after "
                      f"{timeout_s:.0f}s; attempts: " + " | ".join(attempts[-8:]))
        _time.sleep(poll_s)


def _first_running_instance(instances: list[dict[str, Any]]) -> Optional[str]:
    for pod in instances:
        if pod["phase"] == "Running":
            return str(pod["name"])
    return None


def cr_backup_config(cr: dict[str, Any]) -> dict[str, Any]:
    """Backup-relevant CR slices: repo schedules, manual block, global opts."""
    pgb = (((cr.get("spec") or {}).get("backups") or {}).get("pgbackrest") or {})
    repos = pgb.get("repos") or []
    schedules = [{"repo": r.get("name", ""), "schedules": r.get("schedules") or {}}
                 for r in repos if isinstance(r, dict)]
    return {"schedules": schedules, "manual": pgb.get("manual") or None,
            "global": pgb.get("global") or {}}


def run_discover(spec: OpsSpec) -> int:
    t = spec.target
    kube = Kube(context=t.context, namespace=t.namespace)
    topo: dict[str, Any] = {"collected_utc": utc_now_iso(), "namespace": t.namespace,
                            "cr_kind": "", "cr_name": t.cr_name}

    # CR (kind fallback, name auto-pick when the target doesn't pin one).
    # Track whether any list actually SUCCEEDED: "no CR found" is only true
    # when a query came back empty — if every attempt errored (bad auth,
    # missing CRD, unreachable API server) report the real cause instead of
    # a misleading empty-namespace message.
    cr_doc: Optional[dict[str, Any]] = None
    listed_ok = False
    last_err = ""
    for kind in ([t.cr_kind] + [k for k in CR_KINDS if k != t.cr_kind]):
        try:
            if t.cr_name:
                cr_doc = kube.cluster_cr(kind, t.cr_name)
                topo["cr_kind"], topo["cr_name"] = kind, t.cr_name
            else:
                listing = kube.json(["get", kind])
                listed_ok = True
                items = listing.get("items") or []
                if not items:
                    continue
                cr_doc = items[0]
                topo["cr_kind"] = kind
                topo["cr_name"] = cr_doc.get("metadata", {}).get("name", "")
            break
        except KubeError as exc:
            last_err = str(exc)
            continue
    if cr_doc is not None:
        topo["postgres_version"] = str(((cr_doc.get("spec") or {}).get("postgresVersion", "")))
        topo["backups"] = cr_backup_config(cr_doc)
        _check("cluster-cr", "ok", f"{topo['cr_kind']}/{topo['cr_name']}")
    else:
        if not listed_ok and last_err:
            detail = f"could not query cluster CRs: {last_err[:260]}"
            low = last_err.lower()
            if "credentials" in low or "unauthorized" in low or "forbidden" in low:
                detail += " — kubeconfig auth problem; run Validate on this target for details"
            _check("cluster-cr", "fail", detail)
        else:
            _check("cluster-cr", "fail",
                   f"no {' / '.join(CR_KINDS)} found in '{t.namespace}'")
        print(f"{TOPOLOGY_MARKER} {json.dumps(topo)}", flush=True)
        return 3

    # Pods.
    try:
        pods = kube.json(["get", "pods"]).get("items") or []
    except KubeError as exc:
        _check("pods", "fail", str(exc)[:300])
        print(f"{TOPOLOGY_MARKER} {json.dumps(topo)}", flush=True)
        return 3
    buckets = classify_pods(pods, topo["cr_name"])
    topo["pods"] = buckets
    _check("pods", "ok", f"{len(buckets['instances'])} instance, "
           f"{len(buckets['pgbouncer'])} pgbouncer")

    # Patroni view (leader, roles, TL, lag) via the first running instance pod.
    exec_pod = _first_running_instance(buckets["instances"])
    if exec_pod:
        try:
            view = patroni.fetch_view(kube, exec_pod)
            topo["patroni"] = view.to_dict()
            _check("patroni", "ok",
                   f"leader {view.leader_name or '(none)'} TL {view.timeline}")
        except KubeError as exc:
            topo["patroni"] = {"error": str(exc)[:300]}
            _check("patroni", "warn", str(exc)[:300])
    else:
        topo["patroni"] = {"error": "no running instance pod"}
        _check("patroni", "warn", "no running instance pod to query")

    # StatefulSets + services.
    try:
        sts = kube.json(["get", "sts"]).get("items") or []
        topo["statefulsets"] = [
            {"name": s.get("metadata", {}).get("name", ""),
             "replicas": (s.get("spec") or {}).get("replicas"),
             "ready": (s.get("status") or {}).get("readyReplicas") or 0}
            for s in sts
            if not topo["cr_name"] or
               str(s.get("metadata", {}).get("name", "")).startswith(topo["cr_name"])]
        svcs = kube.json(["get", "svc"]).get("items") or []
        topo["services"] = [
            {"name": v.get("metadata", {}).get("name", ""),
             "type": (v.get("spec") or {}).get("type", ""),
             "cluster_ip": (v.get("spec") or {}).get("clusterIP", "")}
            for v in svcs
            if not topo["cr_name"] or
               str(v.get("metadata", {}).get("name", "")).startswith(topo["cr_name"])]
        _check("workloads", "ok", f"{len(topo['statefulsets'])} sts, "
               f"{len(topo['services'])} services")
    except KubeError as exc:
        _check("workloads", "warn", str(exc)[:300])

    # pgBackRest repo info (raw text kept small; parse happens in backup ops).
    # Guarded: a hung `pgbackrest info` must not crash discover after all the
    # topology above has been collected — this is the last step and its output
    # (OPS_TOPOLOGY_JSON) must always be printed for the worker to cache.
    if exec_pod:
        try:
            res = kube.exec(exec_pod, "database",
                            ["pgbackrest", "--stanza=db", "info"], timeout_s=30)
            if res.ok:
                topo["pgbackrest_info"] = res.stdout[-4000:]
                _check("pgbackrest", "ok", "repo info collected")
            else:
                topo["pgbackrest_info"] = ""
                _check("pgbackrest", "warn",
                       (res.stderr or res.stdout).strip()[:300] or "info unavailable")
        except KubeError as exc:
            topo["pgbackrest_info"] = ""
            _check("pgbackrest", "warn", str(exc)[:300])

    print(f"{TOPOLOGY_MARKER} {json.dumps(topo)}", flush=True)
    return 0
