"""Parameter-value intelligence for CR applies: normalization, validation,
and hazard guardrails.

Three field lessons drive this module (huge_pages incident, 2026-07-23):

* ``pg_settings.setting`` is always the normalized base-unit number, so a CR
  value written as ``1GB`` (or ``on`` vs ``true``, ``0.90`` vs ``0.9``) never
  string-matches the live value — verify must compare NORMALIZED values or it
  reports false failures.
* The web tier validates parameter names/types/ranges from the cached catalog,
  but the raw API path did not — an unknown GUC or an out-of-range value
  patched into the CR takes the whole cluster down (postmaster refuses to
  start). The worker re-validates against the LIVE pg_settings catalog before
  patching.
* Some parameters are only safe when the host environment cooperates
  (``huge_pages=on`` needs hugepages-2Mi pod resources; shared memory must fit
  the container limit; ``max_wal_size`` must fit the PVC). A small extensible
  table of hazard checks runs in the validate phase and blocks or warns
  BEFORE the operator rolls pods.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# ── unit tables (PostgreSQL GUC units, guc.c memory_unit_conversion_table) ──

_MEM_BYTES = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
              "TB": 1024 ** 4}
_TIME_MS = {"us": 0.001, "ms": 1.0, "s": 1000.0, "min": 60_000.0,
            "h": 3_600_000.0, "d": 86_400_000.0}

_NUM_UNIT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]+)?\s*$")

_BOOL_TRUE = ("on", "true", "yes", "1")
_BOOL_FALSE = ("off", "false", "no", "0")


def _base_unit_factor(unit: str) -> tuple[Optional[float], str]:
    """(multiplier to bytes-or-ms, kind) for a pg_settings ``unit`` string.

    Units can carry a block-size multiple ("8kB", "16MB"). kind is "mem",
    "time", or "" when the unit is absent/unrecognized.
    """
    u = (unit or "").strip()
    if not u:
        return None, ""
    m = re.match(r"^(\d+)?\s*([A-Za-z]+)$", u)
    if not m:
        return None, ""
    mult = float(m.group(1) or 1)
    suffix = m.group(2)
    if suffix in _MEM_BYTES:
        return mult * _MEM_BYTES[suffix], "mem"
    if suffix in _TIME_MS:
        return mult * _TIME_MS[suffix], "time"
    return None, ""


def parse_value_with_unit(value: Any, catalog_unit: str) -> Optional[float]:
    """A GUC value (possibly carrying its own unit suffix, e.g. "1GB",
    "300s") expressed in multiples of *catalog_unit* (the pg_settings base
    unit for the parameter). None when unparseable."""
    base, kind = _base_unit_factor(catalog_unit)
    m = _NUM_UNIT_RE.match(str(value))
    if not m:
        return None
    num = float(m.group(1))
    suffix = m.group(2) or ""
    if not suffix:
        return num                      # already in base units
    vfac, vkind = _base_unit_factor(suffix)
    if vfac is None or base is None or vkind != kind:
        return None
    return num * vfac / base


def normalize_value(value: Any, unit: Optional[str], vartype: Optional[str]) -> str:
    """Canonical comparison form of a GUC value, per the catalog's type/unit.

    * bool  -> "on" / "off" (all PG-accepted spellings, incl. unique prefixes)
    * integer with a memory/time unit -> base-unit integer ("1GB" -> "131072"
      when the unit is 8kB)
    * real  -> repr of float ("0.90" -> "0.9")
    * enum/string -> stripped verbatim
    """
    sv = str(value).strip()
    vt = (vartype or "").lower()
    if vt == "bool":
        low = sv.lower()
        if not low:
            return sv
        # PostgreSQL rejects a prefix that is ambiguous between on/off — most
        # notably a bare "o", which is a prefix of BOTH "on" and "off". Only
        # accept a prefix that matches exactly one side, matching PG's parser
        # (so the guardrail never passes a value PG will reject).
        true_m = [s for s in _BOOL_TRUE if s.startswith(low)]
        false_m = [s for s in _BOOL_FALSE if s.startswith(low)]
        if true_m and not false_m:
            return "on"
        if false_m and not true_m:
            return "off"
        return sv
    if vt in ("integer", "int64", "real"):
        parsed = parse_value_with_unit(sv, unit or "")
        if parsed is None:
            return sv
        if vt == "real":
            return repr(parsed)
        # pg rounds unit conversions to integer settings
        return str(int(round(parsed)))
    return sv


def values_equal(a: Any, b: Any, unit: Optional[str] = None,
                 vartype: Optional[str] = None) -> bool:
    """Compare two GUC values under normalization. Falls back to raw string
    equality when neither side normalizes."""
    if str(a) == str(b):
        return True
    return normalize_value(a, unit, vartype) == normalize_value(b, unit, vartype)


# ── catalog validation (the server-side twin of the UI's checks) ──

def validate_against_catalog(rows: dict[str, dict[str, Any]],
                             proposed: dict[str, Any],
                             locked: dict[str, str]) -> tuple[list[str], list[str]]:
    """Validate proposed parameter values against live pg_settings rows.

    *rows*: name -> pg_settings row (setting/unit/vartype/min_val/max_val/
    enumvals/context). *locked*: name -> channel for names that must never be
    applied via the CR (patroni-locked / operator-managed).

    Returns (blockers, warnings). An empty catalog row set validates nothing —
    the caller decides whether to proceed (e.g. cluster down, catalog
    unavailable: a recovery apply must not be blocked by its own outage).
    """
    blockers: list[str] = []
    warnings: list[str] = []
    for name, value in proposed.items():
        if value is None:
            continue                    # removal — nothing to validate
        if name in locked:
            blockers.append(
                f"{name}: channel is '{locked[name]}' — this parameter is "
                "owned by Patroni/the operator and a CR value would be "
                "ignored or reverted (or break replication config)")
            continue
        row = rows.get(name)
        if row is None:
            if "." in name:
                warnings.append(
                    f"{name}: extension parameter not in the live catalog — "
                    "cannot validate; it only takes effect if the extension "
                    "is loaded")
                continue
            blockers.append(
                f"{name}: unknown parameter (not in pg_settings on the "
                "leader) — patching it would stop the postmaster from "
                "starting: 'unrecognized configuration parameter'")
            continue
        vt = str(row.get("vartype") or "")
        unit = row.get("unit")
        sv = str(value).strip()
        if vt == "bool":
            if normalize_value(sv, None, "bool") not in ("on", "off"):
                blockers.append(f"{name}: '{sv}' is not a valid boolean")
            continue
        if vt in ("integer", "int64", "real"):
            parsed = parse_value_with_unit(sv, str(unit or ""))
            if parsed is None:
                blockers.append(
                    f"{name}: '{sv}' is not a valid {vt}"
                    + (f" (unit {unit})" if unit else ""))
                continue
            try:
                mn = float(row["min_val"]) if row.get("min_val") not in (None, "") else None
                mx = float(row["max_val"]) if row.get("max_val") not in (None, "") else None
            except (TypeError, ValueError):
                mn = mx = None
            if mn is not None and parsed < mn or mx is not None and parsed > mx:
                blockers.append(
                    f"{name}: {sv} is outside the server's allowed range "
                    f"[{row.get('min_val')}, {row.get('max_val')}]"
                    + (f" (in {unit})" if unit else ""))
            continue
        if vt == "enum":
            enums = [str(e) for e in (row.get("enumvals") or [])]
            if enums and sv.lower() not in [e.lower() for e in enums]:
                blockers.append(
                    f"{name}: '{sv}' is not one of {', '.join(enums)}")
            continue
        # string: nothing structural to check
    return blockers, warnings


# ── hazard guardrails (params with hard host dependencies) ──

def _mem_to_bytes(v: Any) -> Optional[float]:
    """Kubernetes quantity or PG memory string -> bytes."""
    s = str(v or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$", s)
    if not m:
        return None
    num = float(m.group(1))
    suffix = m.group(2)
    k8s = {"": 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
           "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4,
           "k": 1000}
    if suffix in k8s:
        return num * k8s[suffix]
    if suffix in _MEM_BYTES:
        return num * _MEM_BYTES[suffix]
    return None


def _instances(cr: dict[str, Any]) -> list[dict[str, Any]]:
    spec = cr.get("spec") or {}
    inst = spec.get("instances")
    return [i for i in inst if isinstance(i, dict)] if isinstance(inst, list) else []


def _setting_bytes(name: str, proposed: dict[str, Any],
                   rows: dict[str, dict[str, Any]]) -> Optional[float]:
    """Effective value of *name* in bytes: proposed if staged, else live."""
    row = rows.get(name) or {}
    unit = str(row.get("unit") or "")
    if name in proposed and proposed[name] is not None:
        val = proposed[name]
    elif row.get("setting") is not None:
        val = row["setting"]
    else:
        return None
    in_base = parse_value_with_unit(val, unit)
    base, kind = _base_unit_factor(unit)
    if in_base is None or base is None or kind != "mem":
        return None
    return in_base * base


def _setting_num(name: str, proposed: dict[str, Any],
                 rows: dict[str, dict[str, Any]]) -> Optional[float]:
    row = rows.get(name) or {}
    if name in proposed and proposed[name] is not None:
        return parse_value_with_unit(proposed[name], str(row.get("unit") or ""))
    if row.get("setting") is not None:
        try:
            return float(row["setting"])
        except (TypeError, ValueError):
            return None
    return None


def hazard_findings(proposed: dict[str, Any], cr: dict[str, Any],
                    rows: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Precondition checks for parameters with hard host dependencies.

    Returns (blockers, warnings). Each check is independent and degrades to
    silence when the information it needs is unavailable — a hazard check must
    never invent a blocker out of missing data.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    instances = _instances(cr)

    # huge_pages=on requires hugepages-2Mi pod resources (and node-level
    # vm.nr_hugepages). Without them every member fails with
    # 'FATAL: could not map anonymous shared memory' — a total outage.
    hp = str(proposed.get("huge_pages") or "").strip().lower()
    if hp == "on":
        has_hp = any(
            ("hugepages-2Mi" in ((i.get("resources") or {}).get(sec) or {})
             or "hugepages-1Gi" in ((i.get("resources") or {}).get(sec) or {}))
            for i in instances for sec in ("limits", "requests"))
        if not has_hp:
            blockers.append(
                "huge_pages=on but no instance declares hugepages-2Mi/1Gi pod "
                "resources — every member would fail to start ('FATAL: could "
                "not map anonymous shared memory'). Use huge_pages=try, or add "
                "hugepages resources to spec.instances[].resources first (the "
                "nodes also need vm.nr_hugepages provisioned).")
        else:
            warnings.append(
                "huge_pages=on: pod hugepages resources found — also confirm "
                "the NODES have vm.nr_hugepages provisioned, or startup will "
                "still fail.")
    elif hp == "try":
        warnings.append(
            "huge_pages=try falls back to regular pages silently when huge "
            "pages are unavailable — verify with SHOW huge_pages_status after "
            "the restart if you are benchmarking the difference.")

    # Shared-memory arithmetic vs the container memory limit.
    shared = _setting_bytes("shared_buffers", proposed, rows)
    maxconn = _setting_num("max_connections", proposed, rows)
    if shared is not None and ("shared_buffers" in proposed
                               or "max_connections" in proposed):
        est = shared * 1.05 + (maxconn or 100) * 120_000 + 48 * 1024 ** 2
        limits = [b for i in instances
                  for b in [_mem_to_bytes(((i.get("resources") or {})
                                           .get("limits") or {}).get("memory"))]
                  if b]
        if limits:
            lim = min(limits)
            if shared >= lim:
                blockers.append(
                    f"shared_buffers ({shared / 1024 ** 3:.1f} GiB) is at or above "
                    f"the container memory limit ({lim / 1024 ** 3:.1f} GiB) — "
                    "the postmaster cannot start.")
            elif est > lim * 0.9:
                warnings.append(
                    f"estimated shared-memory request (~{est / 1024 ** 3:.1f} GiB "
                    "from shared_buffers + connection overhead) is over 90% of "
                    f"the container memory limit ({lim / 1024 ** 3:.1f} GiB) — "
                    "expect allocation failures or OOM kills under load.")

    # max_wal_size vs the data PVC: WAL that can outgrow the volume takes
    # every member down with a full disk (same signature as huge_pages).
    mws = _setting_bytes("max_wal_size", proposed, rows) \
        if "max_wal_size" in proposed else None
    if mws is not None:
        pvcs = [b for i in instances
                for b in [_mem_to_bytes((((i.get("dataVolumeClaimSpec") or {})
                                          .get("resources") or {})
                                         .get("requests") or {}).get("storage"))]
                if b]
        if pvcs and mws > min(pvcs) * 0.5:
            warnings.append(
                f"max_wal_size ({mws / 1024 ** 3:.0f} GiB) is more than half the "
                f"data PVC ({min(pvcs) / 1024 ** 3:.0f} GiB) — a checkpoint stall "
                "or archiving outage could fill the volume and PANIC every "
                "member. Size it to the PVC, not the workload.")

    return blockers, warnings


def pgbackrest_hazards(proposed: dict[str, Any]) -> list[str]:
    """Warnings for pgBackRest global options with known failure couplings."""
    warnings: list[str] = []
    spool = str(proposed.get("spool-path") or "")
    async_on = str(proposed.get("archive-async") or "").lower() in ("y", "yes", "true", "1", "on")
    if spool and (spool == "/pgdata" or spool.startswith("/pgdata/")):
        warnings.append(
            "spool-path is on the data volume (/pgdata): with archive-async, "
            "an archiving outage backlogs WAL and spool onto the SAME disk — "
            "the volume fills twice as fast. Prefer a dedicated path/volume."
            if async_on or "archive-async" not in proposed else
            "spool-path is on the data volume (/pgdata) — if archive-async is "
            "enabled later, an archive outage will fill the data disk faster.")
    return warnings
