"""Ops job spec: the YAML contract between the web tier and the ops runner.

Like benchmark specs, an ops spec NEVER contains a secret: the kubeconfig
reaches the runner as the ``KUBECONFIG`` environment variable (set by the
worker at exec time) and the DB password is read from the cluster's pguser
Secret by the runner itself. The spec carries only names, parameters, and the
non-secret target coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pgbench_harness.errors import SpecError

OPS_KINDS = ("validate", "discover", "cr-apply", "backup", "scenario", "monitor",
             "pg-params", "diag", "health", "operate",
             "pmm-enable", "pmm-status", "pmm-disable")

OPERATE_OPERATIONS = ("restart", "switchover", "failover", "scale", "resize",
                      "schedules")

# CR kinds we know how to drive, in fallback order (Percona first, then the
# upstream Crunchy kind it is built on).
CR_KINDS = ("perconapgcluster", "postgrescluster")

SCENARIO_CASES = ("switchover", "pgkill", "pod-delete", "node-loss")


@dataclass(frozen=True)
class OpsTarget:
    """Non-secret coordinates of a Kube Target (mirrors the webapp registry)."""

    name: str
    context: str = ""                 # empty = kubeconfig current-context
    namespace: str = "percona"
    cr_kind: str = "perconapgcluster"
    cr_name: str = ""
    pguser_secret: str = ""           # default derived: <cr_name>-pguser-<db_user>
    pguser_secret_key: str = "password"
    db_user: str = "doadmin"
    db_name: str = "defaultdb"

    @property
    def pguser_secret_name(self) -> str:
        return self.pguser_secret or f"{self.cr_name}-pguser-{self.db_user}"

    @property
    def patroni_scope(self) -> str:
        """Patroni cluster name as patronictl knows it (PGO uses <cr>-ha)."""
        return f"{self.cr_name}-ha"


@dataclass(frozen=True)
class OpsSpec:
    op: str
    target: OpsTarget
    label: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _require(doc: dict[str, Any], key: str, why: str) -> Any:
    if key not in doc or doc[key] in (None, ""):
        raise SpecError(f"ops spec: missing required field '{key}' ({why})")
    return doc[key]


def parse_ops_spec(doc: Any) -> OpsSpec:
    """Validate and normalize an ops spec document."""
    if not isinstance(doc, dict):
        raise SpecError("ops spec: top level must be a mapping")
    op = str(_require(doc, "op", "which operation to run"))
    if op not in OPS_KINDS:
        raise SpecError(f"ops spec: unknown op '{op}' (expected one of {', '.join(OPS_KINDS)})")
    tdoc = doc.get("target") or {}
    if not isinstance(tdoc, dict):
        raise SpecError("ops spec: 'target' must be a mapping")
    name = str(_require(tdoc, "name", "the Kube Target name"))
    cr_kind = str(tdoc.get("cr_kind") or CR_KINDS[0]).lower()
    if cr_kind not in CR_KINDS:
        raise SpecError(f"ops spec: cr_kind '{cr_kind}' not supported "
                        f"(expected {', '.join(CR_KINDS)})")
    # validate/discover may run before the CR name is known; every other op
    # acts on a specific cluster and must name it.
    cr_name = str(tdoc.get("cr_name") or "")
    if not cr_name and op not in ("validate", "discover"):
        raise SpecError(f"ops spec: target.cr_name is required for op '{op}'")
    target = OpsTarget(
        name=name,
        context=str(tdoc.get("context") or ""),
        namespace=str(tdoc.get("namespace") or "percona"),
        cr_kind=cr_kind,
        cr_name=cr_name,
        pguser_secret=str(tdoc.get("pguser_secret") or ""),
        pguser_secret_key=str(tdoc.get("pguser_secret_key") or "password"),
        db_user=str(tdoc.get("db_user") or "doadmin"),
        db_name=str(tdoc.get("db_name") or "defaultdb"),
    )
    params = doc.get("params") or {}
    if not isinstance(params, dict):
        raise SpecError("ops spec: 'params' must be a mapping")
    if op == "scenario":
        case = str(params.get("case") or "")
        if case not in SCENARIO_CASES:
            raise SpecError(f"ops spec: scenario params.case must be one of "
                            f"{', '.join(SCENARIO_CASES)} (got '{case or '<missing>'}')")
    if op == "backup":
        btype = str(params.get("type") or "incr")
        if btype not in ("full", "diff", "incr"):
            raise SpecError(f"ops spec: backup params.type must be full|diff|incr (got '{btype}')")
    if op == "operate":
        operation = str(params.get("operation") or "")
        if operation not in OPERATE_OPERATIONS:
            raise SpecError(f"ops spec: operate params.operation must be one of "
                            f"{', '.join(OPERATE_OPERATIONS)} (got '{operation or '<missing>'}')")
        if operation == "scale":
            try:
                n = int(params.get("replicas"))
            except (TypeError, ValueError):
                raise SpecError("ops spec: scale needs integer params.replicas")
            if not 1 <= n <= 16:
                raise SpecError(f"ops spec: scale replicas must be 1..16 (got {n}) — "
                                "use pause for a full stop")
        if operation == "resize":
            res = params.get("resources")
            if not isinstance(res, dict) or not any(
                    (res.get(sec) or {}) for sec in ("requests", "limits")):
                raise SpecError("ops spec: resize needs non-empty params.resources "
                                "{requests/limits: {cpu, memory}} — an empty patch "
                                "would strip the pods' existing resources")
    if op in ("pmm-enable", "pmm-status"):
        if not str(params.get("server_host") or "").strip():
            raise SpecError(f"ops spec: {op} needs params.server_host "
                            "(the PMM server address)")
        qs = str(params.get("query_source") or "pgstatmonitor")
        if qs not in ("pgstatmonitor", "pgstatements"):
            raise SpecError("ops spec: pmm query_source must be "
                            f"pgstatmonitor|pgstatements (got '{qs}')")
        ext = str(params.get("extension") or "pg_stat_monitor")
        if ext not in ("pg_stat_monitor", "pg_stat_statements"):
            raise SpecError("ops spec: pmm extension must be "
                            f"pg_stat_monitor|pg_stat_statements (got '{ext}')")
    if op == "pmm-disable" and not str(params.get("rollback_of") or "").strip():
        raise SpecError("ops spec: pmm-disable needs params.rollback_of "
                        "(the pmm-enable run id whose backup to restore)")
    label = str(doc.get("label") or f"{op}-{target.name}")
    return OpsSpec(op=op, target=target, label=label, params=dict(params), raw=dict(doc))
