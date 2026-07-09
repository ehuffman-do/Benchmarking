"""Patroni topology parsing — pure Python, unit-tested against real output.

``patronictl list -f json`` is the authoritative source for leader identity,
member state, timeline, and lag. Parsing happens here on the JSON document —
never with grep/awk over the table form (that approach corrupted captures in
the field).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from pgbench_harness.ops.kube import Kube, KubeError

LEADER_ROLES = ("leader", "standby leader")


@dataclass
class Member:
    name: str
    host: str = ""
    role: str = ""
    state: str = ""
    timeline: Optional[int] = None
    lag_mb: Optional[float] = None

    @property
    def is_leader(self) -> bool:
        return self.role.lower() in LEADER_ROLES


@dataclass
class PatroniView:
    members: list[Member] = field(default_factory=list)
    raw: str = ""

    @property
    def leader(self) -> Optional[Member]:
        return next((m for m in self.members if m.is_leader), None)

    @property
    def leader_name(self) -> str:
        m = self.leader
        return m.name if m else ""

    @property
    def timeline(self) -> Optional[int]:
        m = self.leader
        return m.timeline if m else None

    def to_dict(self) -> dict[str, Any]:
        return {"leader": self.leader_name, "timeline": self.timeline,
                "members": [{"name": m.name, "host": m.host, "role": m.role,
                             "state": m.state, "timeline": m.timeline,
                             "lag_mb": m.lag_mb} for m in self.members]}


def parse_patronictl_list(text: str) -> PatroniView:
    """Parse ``patronictl list -f json`` output (Patroni 3.x / PGO 2.x).

    Tolerates key-name drift across versions: role/state keys are matched
    case-insensitively and lag may be ``Lag in MB`` or missing entirely for
    the leader.
    """
    doc = json.loads(text)
    if not isinstance(doc, list):
        raise ValueError("patronictl list: expected a JSON array of members")
    members: list[Member] = []
    for entry in doc:
        if not isinstance(entry, dict):
            continue
        low = {str(k).strip().lower(): v for k, v in entry.items()}
        tl = low.get("tl")
        lag = low.get("lag in mb")
        members.append(Member(
            name=str(low.get("member", "")),
            host=str(low.get("host", "")),
            role=str(low.get("role", "")),
            state=str(low.get("state", "")),
            timeline=int(tl) if isinstance(tl, (int, float)) or
                     (isinstance(tl, str) and tl.isdigit()) else None,
            lag_mb=float(lag) if isinstance(lag, (int, float)) else None,
        ))
    if not members:
        raise ValueError("patronictl list: no members in output")
    return PatroniView(members=members, raw=text)


def fetch_view(kube: Kube, exec_pod: str, scope: str = "",
               timeout_s: float = 20) -> PatroniView:
    """Run patronictl inside *exec_pod* and parse the member list."""
    argv = ["patronictl", "list"] + ([scope] if scope else []) + ["-f", "json"]
    res = kube.exec(exec_pod, "database", argv, timeout_s=timeout_s)
    if not res.ok:
        raise KubeError(f"patronictl list failed on {exec_pod} (rc={res.returncode}): "
                        f"{(res.stderr or res.stdout).strip()[:300]}")
    try:
        return parse_patronictl_list(res.stdout)
    except ValueError as exc:
        raise KubeError(f"patronictl list on {exec_pod}: {exc}")
