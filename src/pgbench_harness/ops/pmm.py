"""PMM 3.x enablement as a first-class operation (port of enable-pmm.sh).

``ops pmm-enable`` takes a Percona PostgreSQL cluster from unmonitored to
fully monitored in one run: state backup, PMM3 secret, single CR patch
(pmm block + shared_preload_libraries), spec-aware rollout wait, resilient
topology re-discovery, extension on the primary, HA-preserving sidecar
bounce, log/QAN validation, and server-side confirmation against the PMM
inventory API. ``ops pmm-status`` re-runs only the validation; ``ops
pmm-disable`` restores the backed-up CR and deletes the secret.

Two reference-script bugs are fixed here, not ported:

* Rollout completion is SPEC-AWARE: a pod only counts as rolled when it is
  Running + Ready **and** carries the patched spec (pmm-client container
  present, secret + serverHost visible in its env) **and**, if its pre-patch
  spec did not already match, its UID has changed. The script's readiness
  poll could pass in the gap between OnDelete recreations.
* Leader discovery is retry-with-deadline via
  ``discover.resolve_leader_resilient``: role label first (no exec), then
  patronictl against EVERY running pod; election windows and dying exec
  targets are retryable, and the pre-change, post-rollout, and post-bounce
  discoveries all share it.

Secrets: the API token comes ONLY from the ``PGB_PMM_TOKEN`` environment
variable (same precedent as the DB password's ``target.password_env``). It
is registered with the output redactor, delivered to kubectl via an
``apply -f -`` stdin manifest (never argv — KubeError echoes argv), and
dry-run renders ``<token>``.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from pgbench_harness.ops import patroni
from pgbench_harness.ops.discover import (classify_pods, leader_by_label,
                                          resolve_leader_resilient)
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.oprun import (EXIT_FAILED, EXIT_OK, EXIT_WARNING,
                                       OpsRun, read_meta)
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.util import atomic_write_text, get_redactor, utc_now_iso

TOKEN_ENV = "PGB_PMM_TOKEN"

PAIRINGS = {"pgstatmonitor": "pg_stat_monitor", "pgstatements": "pg_stat_statements"}


def _cfg(params: dict[str, Any]) -> dict[str, Any]:
    """Normalize the pmm params with the reference script's defaults."""
    return {
        "server_host": str(params.get("server_host") or ""),
        "client_image": str(params.get("client_image")
                            or "docker.io/percona/pmm-client:3.8.1"),
        "query_source": str(params.get("query_source") or "pgstatmonitor"),
        "extension": str(params.get("extension") or "pg_stat_monitor"),
        "base_libs": str(params.get("base_libs") or "pgaudit"),
        "database": str(params.get("database") or "postgres"),
        "secret_name": str(params.get("secret_name") or ""),
        "rollout_timeout_s": float(params.get("rollout_timeout_s") or 600),
        "discover_timeout_s": float(params.get("discover_timeout_s") or 120),
        "qan_timeout_s": float(params.get("qan_timeout_s") or 120),
        "poll_s": float(params.get("poll_s") or 5),
    }


def _token(run: OpsRun, required: bool) -> Optional[str]:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        if required:
            run.event("preflight", f"ABORT: {TOKEN_ENV} is not set",
                      "export the PMM service-account token in the worker/CLI "
                      "environment — it never goes in the spec")
        return None
    get_redactor().register(token)
    if not token.startswith("glsa_"):
        run.event("preflight", "token does not start with 'glsa_'",
                  "PMM3 service-account tokens normally do — continuing anyway")
    return token


def _instance_pods(kube: Kube, cr_name: str) -> list[dict[str, Any]]:
    items = kube.json(["get", "pods"]).get("items") or []
    return classify_pods(items, cr_name)["instances"]


def _pod_raw(kube: Kube, name: str) -> Optional[dict[str, Any]]:
    try:
        return kube.json(["get", "pod", name])
    except KubeError:
        return None


def _pmm_container(pod_raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    for c in (pod_raw.get("spec") or {}).get("containers") or []:
        if c.get("name") == "pmm-client":
            return c
    return None


def _pmm_env(container: dict[str, Any]) -> dict[str, str]:
    return {e.get("name", ""): str(e.get("value", ""))
            for e in container.get("env") or []}


def _pod_spec_matches(pod_raw: dict[str, Any], cfg: dict[str, Any],
                      secret_name: str) -> bool:
    """Does this pod carry the patched spec? pmm-client present + the expected
    secret referenced + serverHost visible in its env."""
    c = _pmm_container(pod_raw)
    if c is None:
        return False
    env = _pmm_env(c)
    blob = json.dumps(c)
    secret_ok = secret_name in blob
    server_ok = any(cfg["server_host"] in v for v in env.values()) or \
        cfg["server_host"] in blob
    return secret_ok and server_ok


def _pod_ready(pod_raw: dict[str, Any]) -> bool:
    status = pod_raw.get("status") or {}
    if status.get("phase") != "Running":
        return False
    cs = status.get("containerStatuses") or []
    return bool(cs) and all(c.get("ready") for c in cs)


def _wait_rollout(kube: Kube, run: OpsRun, cr_name: str, cfg: dict[str, Any],
                  secret_name: str, pre_uids: dict[str, str],
                  pre_matched: dict[str, bool], what: str) -> bool:
    """Spec-aware rollout wait (reference-script bug #1 fixed).

    Done only when every instance pod exists, is Running with all containers
    Ready, carries the patched spec, and — if its pre-patch spec did NOT
    already match — has a new UID (i.e. was actually recreated). A pod that
    is merely Ready on the old spec counts as not-done."""
    deadline = time.monotonic() + cfg["rollout_timeout_s"]
    last = ""
    while True:
        pods = _instance_pods(kube, cr_name)
        pending: list[str] = []
        if not pods:
            pending.append("(no instance pods)")
        for p in pods:
            raw = _pod_raw(kube, p["name"])
            if raw is None:
                pending.append(f"{p['name']}: unreadable")
                continue
            uid = str((raw.get("metadata") or {}).get("uid", ""))
            recreated = uid != pre_uids.get(p["name"], "")
            spec_ok = _pod_spec_matches(raw, cfg, secret_name)
            needs_recreate = not pre_matched.get(p["name"], False)
            if not spec_ok:
                pending.append(f"{p['name']}: old spec")
            elif needs_recreate and not recreated:
                pending.append(f"{p['name']}: spec text matches but pod not "
                               "recreated yet")
            elif not _pod_ready(raw):
                pending.append(f"{p['name']}: not ready")
        if not pending:
            run.event("rollout", f"all {len(pods)} instance pods rolled and "
                      f"Ready ({what})")
            return True
        detail = "; ".join(pending[:4])
        if detail != last:
            run.status_update(phase=f"rollout ({what})", detail=detail)
            last = detail
        if time.monotonic() >= deadline:
            run.event("rollout", f"TIMEOUT waiting for rollout ({what})",
                      detail + " — continuing to verification (never leaving "
                      "the cluster half-configured silently)")
            return False
        time.sleep(cfg["poll_s"])


def _discover(kube: Kube, run: OpsRun, cr_name: str, cfg: dict[str, Any],
              what: str) -> tuple[list[str], str, Optional[Any]]:
    """Resilient discovery (reference-script bug #2 fixed) with progress."""
    run.status_update(phase=f"discovering topology ({what})")
    instances, leader, view, attempts = resolve_leader_resilient(
        kube, cr_name, timeout_s=cfg["discover_timeout_s"], poll_s=cfg["poll_s"],
        notify=lambda a: run.status_update(phase=f"discovering ({what})", detail=a))
    run.event("discover", f"leader {leader} ({what})",
              f"{len(instances)} instance pod(s); {attempts[-1]}")
    return instances, leader, view


def _psql(kube: Kube, pod: str, db: str, sql: str) -> Optional[str]:
    res = kube.psql(pod, sql, database=db, timeout_s=30)
    return res.stdout.strip() if res.ok else None


def _backup_state(kube: Kube, run: OpsRun, t: Any, cfg: dict[str, Any],
                  secret_name: str, leader: str, instances: list[str]) -> Path:
    bdir = run.run_dir / "backup"
    bdir.mkdir(exist_ok=True)
    res = kube.run(["get", t.cr_kind, t.cr_name, "-o", "yaml"], check=True)
    atomic_write_text(bdir / f"cr-{t.cr_name}.yaml", res.stdout)
    res = kube.run(["get", "secret", secret_name, "-o", "yaml"])
    atomic_write_text(bdir / f"secret-{secret_name}.yaml",
                      res.stdout if res.ok else f"(no existing secret {secret_name})\n")
    res = kube.exec(leader, "database", ["patronictl", "show-config"], timeout_s=20)
    atomic_write_text(bdir / "patroni-show-config.yaml",
                      res.stdout if res.ok else "(unavailable)\n")
    res = kube.exec(leader, "database", ["patronictl", "list"], timeout_s=20)
    atomic_write_text(bdir / "patroni-list.txt",
                      res.stdout if res.ok else "(unavailable)\n")
    for pod in instances:
        raw = _pod_raw(kube, pod)
        c = _pmm_container(raw) if raw else None
        atomic_write_text(bdir / f"pmmenv-{pod}.json",
                          json.dumps(c.get("env", []), indent=1) if c
                          else f"(no pmm-client container on {pod} yet)\n")
    spl = _psql(kube, leader, cfg["database"], "SHOW shared_preload_libraries;")
    exts = _psql(kube, leader, cfg["database"],
                 "SELECT extname, extversion FROM pg_extension ORDER BY extname;")
    atomic_write_text(bdir / "preload-and-extensions.txt",
                      f"# shared_preload_libraries (runtime, leader)\n{spl or ''}\n\n"
                      f"# installed extensions in db={cfg['database']} (leader)\n"
                      f"{exts or ''}\n")
    run.event("backup", f"state backed up to {bdir.name}/",
              f"restore CR with: kubectl apply -n {t.namespace} -f "
              f"{bdir / f'cr-{t.cr_name}.yaml'}")
    return bdir


def _apply_secret(kube: Kube, secret_name: str, token: str) -> None:
    """Create/refresh the PMM3 secret via an apply -f - stdin manifest so the
    token never appears on a command line (KubeError echoes argv)."""
    manifest = json.dumps({
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": secret_name}, "type": "Opaque",
        "stringData": {"PMM_SERVER_TOKEN": token},
    })
    kube.run(["apply", "-f", "-"], input_text=manifest, check=True)


def _verify_pmm3_mode(kube: Kube, run: OpsRun, leader: str,
                      cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"prerun_query_source": None, "legacy_pmm2_path": None}
    raw = _pod_raw(kube, leader)
    c = _pmm_container(raw) if raw else None
    if c is None:
        run.event("verify", "no pmm-client container on the leader yet")
        return out
    env = _pmm_env(c)
    prerun = env.get("PMM_AGENT_PRERUN_SCRIPT", "")
    cfg_path = env.get("PMM_AGENT_CONFIG_FILE", "")
    out["prerun_query_source"] = f"--query-source={cfg['query_source']}" in prerun
    out["legacy_pmm2_path"] = "/pmm2/" in cfg_path
    if out["prerun_query_source"]:
        run.event("verify", f"prerun uses --query-source={cfg['query_source']}")
    else:
        run.event("verify", "prerun does NOT show the expected --query-source",
                  prerun[:150])
    if out["legacy_pmm2_path"]:
        run.event("verify", "config path still /pmm2 — likely PMM2 mode",
                  "wrong secret key (PMM_SERVER_KEY instead of PMM_SERVER_TOKEN)?")
    else:
        run.event("verify", "config path is not the legacy /pmm2 (PMM3 mode)")
    return out


def _qan_seen(kube: Kube, pod: str, query_source: str) -> bool:
    res = kube.run(["logs", pod, "-c", "pmm-client", "--tail=80"], timeout_s=20)
    if not res.ok:
        return False
    low = res.stdout.lower()
    return (f"qan_postgresql_{query_source}_agent" in low
            or ("metrics buckets" in low and "sending" in low))


def _inventory_check(run: OpsRun, cfg: dict[str, Any], token: Optional[str],
                     instances: list[str]) -> dict[str, Any]:
    """Server-side confirmation: PMM3 REST inventory (GET /v1/inventory/services,
    Bearer auth). Unreachable server degrades to a recorded warning."""
    out: dict[str, Any] = {"reachable": False, "postgresql_services": [],
                           "nodes_covered": 0}
    if not token:
        run.event("inventory", "skipped: no token available")
        return out
    host = cfg["server_host"]
    base = host if "://" in host else f"https://{host}"
    url = f"{base.rstrip('/')}/v1/inventory/services?service_type=SERVICE_TYPE_POSTGRESQL_SERVICE"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # PMM ships a self-signed cert
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        run.event("inventory", "PMM server unreachable from the harness host — "
                  "recorded as a warning, not a failure", str(exc)[:150])
        return out
    out["reachable"] = True
    services = doc.get("services") or doc.get("postgresql") or []
    names = [str(s.get("service_name", "")) for s in services
             if isinstance(s, dict)]
    out["postgresql_services"] = names
    out["nodes_covered"] = sum(1 for pod in instances
                               if any(pod in n or n in pod for n in names))
    run.event("inventory",
              f"{len(names)} PostgreSQL service(s) registered; "
              f"{out['nodes_covered']}/{len(instances)} instance pods matched")
    return out


def _validation(kube: Kube, run: OpsRun, t: Any, cfg: dict[str, Any],
                secret_name: str, token: Optional[str],
                wait_for_qan: bool) -> tuple[dict[str, Any], bool]:
    """Phases 8+9(+12): per-node + cluster-level validation and the report.
    Returns (results, healthy)."""
    instances, leader, view = _discover(kube, run, t.cr_name, cfg, "validation")
    mode = _verify_pmm3_mode(kube, run, leader, cfg)

    qan_global = False
    deadline = time.monotonic() + (cfg["qan_timeout_s"] if wait_for_qan else 1)
    while True:
        qan_global = any(_qan_seen(kube, p, cfg["query_source"]) for p in instances)
        if qan_global or time.monotonic() >= deadline:
            break
        run.status_update(phase="waiting for QAN agents",
                          detail=f"up to {cfg['qan_timeout_s']:.0f}s")
        time.sleep(min(10.0, cfg["poll_s"] * 2))

    cr = kube.cluster_cr(t.cr_kind, t.cr_name)
    cr_spl = str((((((cr.get("spec") or {}).get("patroni") or {})
                    .get("dynamicConfiguration") or {}).get("postgresql") or {})
                  .get("parameters") or {}).get("shared_preload_libraries", ""))
    rt_spl = _psql(kube, leader, cfg["database"],
                   "SHOW shared_preload_libraries;") or ""
    want = [x.strip() for x in f"{cfg['base_libs']},{cfg['extension']}".split(",")
            if x.strip()]
    libs = {lib: lib in rt_spl for lib in want}
    if cr_spl and rt_spl and cr_spl != rt_spl:
        run.event("verify", "runtime shared_preload_libraries differs from the "
                  "CR spec value", "operator reconcile doubling — cosmetic only; "
                  "Postgres dedupes at load. Not an error.")

    nodes = []
    for pod in instances:
        raw = _pod_raw(kube, pod) or {}
        cs = (raw.get("status") or {}).get("containerStatuses") or []
        pc = next((c for c in cs if c.get("name") == "pmm-client"), {})
        rec = _psql(kube, pod, cfg["database"], "SELECT pg_is_in_recovery();")
        ext = _psql(kube, pod, cfg["database"],
                    f"SELECT 1 FROM pg_extension WHERE extname='{cfg['extension']}';")
        nodes.append({
            "pod": pod,
            "role": "LEADER" if pod == leader else "REPLICA",
            "pmm_restarts": pc.get("restartCount"),
            "sidecar_ready": bool(pc.get("ready")),
            "extension": ext == "1",
            "qan": _qan_seen(kube, pod, cfg["query_source"]),
            "recovery": {"t": "in-rec", "f": "primary"}.get(rec or "", "?"),
        })

    inventory = _inventory_check(run, cfg, token, instances)
    results = {"generated_utc": utc_now_iso(), "cluster": t.cr_name,
               "namespace": t.namespace, "server_host": cfg["server_host"],
               "client_image": cfg["client_image"], "secret": secret_name,
               "query_source": cfg["query_source"], "extension": cfg["extension"],
               "auth_mode": "PMM3 (PMM_SERVER_TOKEN)",
               "cr_spl": cr_spl, "runtime_spl": rt_spl, "libs": libs,
               "spl_doubling_note": bool(cr_spl and rt_spl and cr_spl != rt_spl),
               "pmm3_mode": mode, "qan_observed": qan_global,
               "nodes": nodes, "inventory": inventory, "leader": leader}
    atomic_write_text(run.run_dir / "validation.json",
                      json.dumps(results, indent=2))
    atomic_write_text(run.run_dir / "validation-report.txt",
                      _render_report(results))
    healthy = (qan_global and all(libs.values())
               and all(n["sidecar_ready"] for n in nodes)
               and not mode.get("legacy_pmm2_path"))
    run.event("report", "validation report written",
              f"QAN={'YES' if qan_global else 'NOT YET'}; "
              f"libs {'ok' if all(libs.values()) else 'MISSING'}; "
              f"inventory {inventory['nodes_covered']}/{len(instances)}")
    return results, healthy


def _render_report(r: dict[str, Any]) -> str:
    lines = [
        "=" * 64,
        " PMM 3.x ENABLEMENT — VALIDATION REPORT",
        f" Cluster:    {r['cluster']}   Namespace: {r['namespace']}",
        f" PMM server: {r['server_host']}",
        f" Generated:  {r['generated_utc']}",
        "=" * 64, "",
        "CONFIGURATION",
        f"  Client image      : {r['client_image']}",
        f"  Auth mode         : {r['auth_mode']}",
        f"  Secret            : {r['secret']}",
        f"  Query source      : {r['query_source']}",
        f"  Target extension  : {r['extension']}", "",
        "CLUSTER-LEVEL CHECKS",
        f"  CR spec SPL       : {r['cr_spl']}",
        f"  Runtime SPL       : {r['runtime_spl']}",
    ]
    if r["spl_doubling_note"]:
        lines += ["    NOTE: runtime differs from CR spec — operator reconcile "
                  "doubling.", "          Cosmetic only; Postgres dedupes at "
                  "load. Not an error."]
    for lib, ok in r["libs"].items():
        lines.append(f"  lib {'present' if ok else 'MISSING'}       : {lib}  "
                     f"[{'OK' if ok else 'FAIL'}]")
    lines.append(f"  QAN observed      : "
                 f"{'YES (source ' + r['query_source'] + ')' if r['qan_observed'] else 'NOT YET — check logs'}")
    inv = r["inventory"]
    lines.append(f"  PMM inventory     : "
                 + (f"{len(inv['postgresql_services'])} PostgreSQL service(s); "
                    f"{inv['nodes_covered']}/{len(r['nodes'])} nodes matched"
                    if inv["reachable"] else "server unreachable (warning)"))
    lines += ["", "PER-NODE CHECKS",
              f"  {'NODE (pod)':<44} {'ROLE':<8} {'RESTART':<8} {'EXT':<5} "
              f"{'QAN':<5} {'RECOVERY':<9} SIDECAR"]
    for n in r["nodes"]:
        lines.append(f"  {n['pod']:<44} {n['role']:<8} "
                     f"{str(n['pmm_restarts'] if n['pmm_restarts'] is not None else '?'):<8} "
                     f"{'OK' if n['extension'] else '--':<5} "
                     f"{'OK' if n['qan'] else '--':<5} {n['recovery']:<9} "
                     f"{'UP' if n['sidecar_ready'] else 'DOWN'}")
    lines += ["", f"VERIFY IN PMM UI (https://{r['server_host']})",
              "  Configuration > Inventory > Services  -> one PostgreSQL "
              "service per instance",
              "  Query Analytics (QAN)                 -> queries via "
              + r["query_source"], "=" * 64, ""]
    return "\n".join(lines)


# ── the three verbs ──

def run_pmm_enable(spec: OpsSpec, results_dir: Path) -> int:
    t = spec.target
    params = dict(spec.params)
    cfg = _cfg(params)
    dry_run = bool(params.get("dry_run"))
    secret_name = cfg["secret_name"] or f"{t.cr_name}-pmm-secret"
    run = OpsRun(results_dir, "pmm-enable", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params={k: v for k, v in params.items()})
    kube = Kube(context=t.context, namespace=t.namespace)
    try:
        # 1. preflight
        token = _token(run, required=not dry_run)
        if token is None and not dry_run:
            run.finalize("aborted", headline={"op": "pmm-enable",
                                              "reason": "no-token"})
            return EXIT_FAILED
        if PAIRINGS.get(cfg["query_source"]) != cfg["extension"]:
            run.event("preflight", f"extension '{cfg['extension']}' vs "
                      f"query-source '{cfg['query_source']}' may not match",
                      "expected pairing: pgstatmonitor<->pg_stat_monitor, "
                      "pgstatements<->pg_stat_statements")
        kube.cluster_cr(t.cr_kind, t.cr_name)        # raises if missing
        run.event("preflight", f"found {t.cr_kind}/{t.cr_name}")

        spl = f"{cfg['base_libs']},{cfg['extension']}"
        patch = {"spec": {
            "pmm": {"enabled": True, "image": cfg["client_image"],
                    "imagePullPolicy": "IfNotPresent",
                    "querySource": cfg["query_source"],
                    "secret": secret_name, "serverHost": cfg["server_host"]},
            "patroni": {"dynamicConfiguration": {"postgresql": {"parameters": {
                "shared_preload_libraries": spl}}}}}}

        if dry_run:
            run.event("dry-run", "secret", f"kubectl apply -f - <<< "
                      f"'{{\"kind\":\"Secret\",\"metadata\":{{\"name\":\"{secret_name}\"}},"
                      f"\"stringData\":{{\"PMM_SERVER_TOKEN\":\"<token>\"}}}}'")
            run.event("dry-run", "CR patch",
                      f"kubectl patch {t.cr_kind} {t.cr_name} --type merge -p "
                      f"'{json.dumps(patch)}'")
            run.event("dry-run", "then", "rollout wait -> re-discover -> "
                      f"CREATE EXTENSION IF NOT EXISTS {cfg['extension']} on the "
                      "leader -> HA bounce -> validation")
            run.finalize("complete", headline={"op": "pmm-enable",
                                               "dry_run": True,
                                               "secret": secret_name,
                                               "server_host": cfg["server_host"]})
            return EXIT_OK

        # 2. pre-change topology (resilient)
        instances, leader, _view = _discover(kube, run, t.cr_name, cfg,
                                             "pre-change")

        # 3. state backup before any mutation
        _backup_state(kube, run, t, cfg, secret_name, leader, instances)

        # snapshot pre-patch pod identity for the spec-aware rollout wait
        pre_uids: dict[str, str] = {}
        pre_matched: dict[str, bool] = {}
        for pod in instances:
            raw = _pod_raw(kube, pod)
            if raw:
                pre_uids[pod] = str((raw.get("metadata") or {}).get("uid", ""))
                pre_matched[pod] = _pod_spec_matches(raw, cfg, secret_name)

        # 4. secret (PMM3 key)
        _apply_secret(kube, secret_name, token or "")
        run.event("secret", f"{secret_name} in place (key: PMM_SERVER_TOKEN — "
                  "PMM3 mode)")

        # 5. single CR patch
        kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
                  "-p", json.dumps(patch)], check=True)
        run.event("apply", "CR patched",
                  f"pmm enabled + shared_preload_libraries={spl}")

        # 6. spec-aware rollout wait
        rolled = _wait_rollout(kube, run, t.cr_name, cfg, secret_name,
                               pre_uids, pre_matched, "post CR-patch")

        # 7. post-rollout re-discovery (resilient — leader may have moved)
        instances, leader, _view = _discover(kube, run, t.cr_name, cfg,
                                             "post-rollout")

        # 9. extension on the PRIMARY (writes go to the leader)
        rt = _psql(kube, leader, cfg["database"],
                   "SHOW shared_preload_libraries;") or ""
        if cfg["extension"] not in rt:
            run.event("extension", f"{cfg['extension']} not yet in runtime "
                      "shared_preload_libraries", "CREATE EXTENSION may fail")
        kube.psql(leader, f"CREATE EXTENSION IF NOT EXISTS {cfg['extension']};",
                  database=cfg["database"], timeout_s=30)
        cnt = _psql(kube, leader, cfg["database"],
                    f"SELECT count(*) FROM {cfg['extension']};")
        if cnt is None:
            run.event("extension", f"{cfg['extension']} view NOT queryable on "
                      "the primary", "check library load + restart")
            run.finalize("failed", headline={"op": "pmm-enable",
                                             "error": "extension-not-queryable"})
            return EXIT_FAILED
        run.event("extension", f"{cfg['extension']} live on primary "
                  f"({cnt} rows)")

        # 10. HA-preserving sidecar bounce so QAN re-registers
        for pod in list(instances):
            run.event("bounce", f"deleting pod {pod} (operator recreates)")
            kube.run(["delete", "pod", pod, "--wait=false"])
            time.sleep(min(5.0, cfg["poll_s"]))
            _wait_rollout(kube, run, t.cr_name, cfg, secret_name,
                          pre_uids={pod: pre_uids.get(pod, "gone")},
                          pre_matched={pod: False}, what=f"after {pod}")

        # post-bounce re-discovery, then 11+12: validation + report
        results, healthy = _validation(kube, run, t, cfg, secret_name, token,
                                       wait_for_qan=True)
        headline = {"op": "pmm-enable", "server_host": cfg["server_host"],
                    "query_source": cfg["query_source"],
                    "qan": results["qan_observed"],
                    "inventory_nodes": results["inventory"]["nodes_covered"],
                    "rolled": rolled, "healthy": healthy}
        run.finalize("complete" if healthy and rolled else "warning",
                     headline=headline)
        return EXIT_OK if healthy and rolled else EXIT_WARNING
    except KubeError as exc:
        run.event("error", "kubectl error", str(exc)[:300])
        run.finalize("failed", headline={"op": "pmm-enable"}, error=str(exc)[:300])
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — the run must always finalize
        run.event("error", "unexpected error", str(exc)[:300])
        run.finalize("failed", headline={"op": "pmm-enable"}, error=str(exc)[:200])
        return EXIT_FAILED


def run_pmm_status(spec: OpsSpec, results_dir: Path) -> int:
    """Validation/report only — zero mutations."""
    t = spec.target
    cfg = _cfg(dict(spec.params))
    secret_name = cfg["secret_name"] or f"{t.cr_name}-pmm-secret"
    run = OpsRun(results_dir, "pmm-status", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=dict(spec.params))
    kube = Kube(context=t.context, namespace=t.namespace)
    try:
        token = _token(run, required=False)   # only needed for the inventory call
        results, healthy = _validation(kube, run, t, cfg, secret_name, token,
                                       wait_for_qan=False)
        run.finalize("complete" if healthy else "warning",
                     headline={"op": "pmm-status", "qan": results["qan_observed"],
                               "healthy": healthy,
                               "inventory_nodes":
                                   results["inventory"]["nodes_covered"]})
        return EXIT_OK if healthy else EXIT_WARNING
    except KubeError as exc:
        run.event("error", "kubectl error", str(exc)[:300])
        run.finalize("failed", headline={"op": "pmm-status"}, error=str(exc)[:300])
        return EXIT_FAILED


def run_pmm_disable(spec: OpsSpec, results_dir: Path) -> int:
    """Rollback: restore the CR backed up by a pmm-enable run, delete the secret."""
    t = spec.target
    params = dict(spec.params)
    cfg = _cfg(params)
    secret_name = cfg["secret_name"] or f"{t.cr_name}-pmm-secret"
    run = OpsRun(results_dir, "pmm-disable", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=params)
    kube = Kube(context=t.context, namespace=t.namespace)
    try:
        src_id = str(params.get("rollback_of") or "")
        src_dir = results_dir / "ops" / src_id
        cr_backup = src_dir / "backup" / f"cr-{t.cr_name}.yaml"
        if not src_id or read_meta(src_dir) is None or not cr_backup.exists():
            run.event("preflight", "ABORT: params.rollback_of must name a "
                      "pmm-enable run with a CR backup",
                      f"looked for {cr_backup}")
            run.finalize("aborted", headline={"op": "pmm-disable",
                                              "reason": "no-backup"})
            return EXIT_FAILED
        if bool(params.get("dry_run")):
            run.event("dry-run", "restore CR",
                      f"kubectl apply -f {cr_backup}")
            run.event("dry-run", "delete secret",
                      f"kubectl delete secret {secret_name}")
            run.finalize("complete", headline={"op": "pmm-disable",
                                               "dry_run": True})
            return EXIT_OK
        kube.run(["apply", "-f", "-"],
                 input_text=cr_backup.read_text(encoding="utf-8"), check=True)
        run.event("restore", f"CR restored from {src_id}/backup/")
        res = kube.run(["delete", "secret", secret_name])
        run.event("secret", f"secret {secret_name} "
                  + ("deleted" if res.ok else "not present"))
        cfg["rollout_timeout_s"] = min(cfg["rollout_timeout_s"], 300)
        run.status_update(phase="waiting for the operator to reconcile")
        run.finalize("complete", headline={"op": "pmm-disable",
                                           "restored_from": src_id})
        return EXIT_OK
    except KubeError as exc:
        run.event("error", "kubectl error", str(exc)[:300])
        run.finalize("failed", headline={"op": "pmm-disable"}, error=str(exc)[:300])
        return EXIT_FAILED
