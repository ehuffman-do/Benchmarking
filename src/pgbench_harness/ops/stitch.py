"""Stitcher: normalize every capture stream to one UTC axis; classify; emit
TIMELINE.txt + events.csv + stitched.json.

Ports stitch.py from the bash harness with its hard-won lessons intact:

* CLASSIFICATION — ``flip? YES (real election)`` vs ``NO (restart in place)``
  is decided by the authoritative Patroni LEADER NAME before vs after, never
  by the probe's answering IP: pod restarts change the pod IP without a
  failover, and the IP heuristic misclassified real runs. The timeline (TL)
  bump is shown alongside as corroborating evidence only.
* T7 / FULL-HA-RECOVERY LATCH — "all members healthy again" must follow an
  OBSERVED DIP in ready members; latching on stale pre-recovery samples
  produced absurd instant-recovery numbers in the field.
* BACKOFF TAIL — pgBouncer's server_login_retry negative-cache rejects even
  fast-retrying clients for ~12-14s after the database is actually back;
  that tail is reported separately from DB downtime.
* PROBE ARTIFACTS — port-forward restarts and probe-side gaps are flagged as
  separate windows, never counted as cluster downtime.

Timestamp model: every probe line carries the app-host clock (one monotonic
axis shared with fire.marker) plus the in-database ``clock_timestamp()`` for
OK ticks; the median (db - local) skew is measured and reported rather than
assumed zero.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.util import atomic_write_json, atomic_write_text


_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_iso_ms(ts: str) -> Optional[int]:
    """ISO-8601 (with or without tz / fractional seconds) -> epoch ms.

    Tolerates kubectl's RFC3339Nano (9-digit fractions — fromisoformat only
    takes 6) and space-separated date/time.
    """
    ts = ts.strip().replace(" ", "T", 1) if " " in ts.strip() else ts.strip()
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        ts = _FRACTION.sub(r"\1", ts)
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


@dataclass
class ProbeTick:
    local_ms: int
    ok: bool
    db_ms: Optional[int] = None
    in_recovery: str = ""
    addr: str = ""
    reason: str = ""


@dataclass
class Stitched:
    fire: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)
    patroni: dict[str, Any] = field(default_factory=dict)
    classification: dict[str, Any] = field(default_factory=dict)
    pgbouncer: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    probe_artifacts: list[dict[str, Any]] = field(default_factory=list)
    clock: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"fire": self.fire, "probe": self.probe, "patroni": self.patroni,
                "classification": self.classification, "pgbouncer": self.pgbouncer,
                "recovery": self.recovery, "probe_artifacts": self.probe_artifacts,
                "clock": self.clock, "timeline": self.timeline}


def parse_probe_log(path: Path) -> list[ProbeTick]:
    """probe.log lines:
    ``OK <local_iso> <db_iso> <in_recovery> <addr>`` |
    ``FAIL <local_iso> <reason...>``"""
    ticks: list[ProbeTick] = []
    if not path.exists():
        return ticks
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        local = parse_iso_ms(parts[1])
        if local is None:
            continue
        if parts[0] == "OK" and len(parts) >= 3:
            ticks.append(ProbeTick(local_ms=local, ok=True,
                                   db_ms=parse_iso_ms(parts[2]),
                                   in_recovery=parts[3] if len(parts) > 3 else "",
                                   addr=parts[4] if len(parts) > 4 else ""))
        elif parts[0] == "FAIL":
            ticks.append(ProbeTick(local_ms=local, ok=False,
                                   reason=" ".join(parts[2:])[:200]))
    return ticks


def parse_patroni_samples(path: Path) -> list[dict[str, Any]]:
    """patroni_samples.jsonl: periodic {ts_epoch_ms, leader, timeline,
    ready, total, members} snapshots."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "ts_epoch_ms" in obj:
                out.append(obj)
        except ValueError:
            continue
    return out


# pgBouncer signal lines (field-observed formats).
_PGB_CRASH = re.compile(r"closing because: server conn crashed|"
                        r"server error|login failed", re.IGNORECASE)
_PGB_RETRY = re.compile(r"server_login_retry|retrying in", re.IGNORECASE)
_TS_PREFIX = re.compile(r"^(?:\S+\s+)?(\d{4}-\d{2}-\d{2}[T ][\d:.,+Z-]+)")


def _line_ts_ms(line: str) -> Optional[int]:
    m = _TS_PREFIX.match(line.strip())
    if not m:
        return None
    return parse_iso_ms(m.group(1).replace(",", "."))


def scan_pgbouncer_logs(raw_dir: Path) -> dict[str, Any]:
    first_crash: Optional[int] = None
    last_retry: Optional[int] = None
    for path in sorted(raw_dir.glob("pgbouncer_*.log")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            ts = _line_ts_ms(line)
            if ts is None:
                continue
            if _PGB_CRASH.search(line) and (first_crash is None or ts < first_crash):
                first_crash = ts
            if _PGB_RETRY.search(line) and (last_retry is None or ts > last_retry):
                last_retry = ts
    return {"first_crash_ms": first_crash, "last_retry_ms": last_retry}


_PATRONI_PROMOTE = re.compile(
    r"promoted self to leader|i am .* the leader with the lock|"
    r"acquired session lock as the leader|"
    r"cleared rewind state|selected new timeline|"
    r"database system is ready to accept connections", re.IGNORECASE)

# Crash-validity: a valid C1/C2 crash means PostgreSQL received NO shutdown
# signal. These lines prove a GRACEFUL shutdown happened instead — their
# presence in the kill window invalidates the run as a crash test
# (DBAAS-8917: DO power-off delivers ACPI; the guest shuts down cleanly).
_GRACEFUL_SHUTDOWN = re.compile(
    r"received fast shutdown request|received smart shutdown request|"
    r"shutdown immediate|aborting any active transactions|"
    r"database system is shut down", re.IGNORECASE)


def scan_crash_validity(raw_dir: Path, fire_ms: Optional[int],
                        window_ms: int = 90_000) -> dict[str, Any]:
    """Grep captured PG logs for graceful-shutdown markers in the kill window.

    A hit on a crash case (pod-delete / node-loss) means the kill was NOT a
    crash — PostgreSQL was signalled and checkpointed. Returns the first such
    marker (if any) so the verdict can stamp the run INVALID-AS-CRASH."""
    lo = (fire_ms - 5_000) if fire_ms is not None else None
    hi = (fire_ms + window_ms) if fire_ms is not None else None
    for path in sorted(raw_dir.glob("patroni_*.log")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not _GRACEFUL_SHUTDOWN.search(line):
                continue
            ts = _line_ts_ms(line)
            if lo is None or (ts is not None and lo <= ts <= hi):
                return {"graceful": True, "marker_ms": ts,
                        "line": line.strip()[:200]}
    return {"graceful": False, "marker_ms": None, "line": ""}


def scan_patroni_logs(raw_dir: Path, fire_ms: int) -> Optional[int]:
    """First post-fire 'promoted/leader with the lock' log timestamp."""
    best: Optional[int] = None
    for path in sorted(raw_dir.glob("patroni_*.log")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not _PATRONI_PROMOTE.search(line):
                continue
            ts = _line_ts_ms(line)
            if ts is not None and ts > fire_ms and (best is None or ts < best):
                best = ts
    return best


def parse_probe_artifacts(path: Path) -> list[dict[str, Any]]:
    """probe_artifacts.log: ``<iso> <label...>`` windows (port-forward restarts)."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(None, 1)
        ts = parse_iso_ms(parts[0]) if parts else None
        if ts is not None:
            out.append({"ts_epoch_ms": ts, "label": parts[1] if len(parts) > 1 else ""})
    return out


def stitch(run_dir: Path) -> Stitched:
    raw = run_dir / "raw"
    s = Stitched()

    # fire marker is the anchor; without it we still stitch what we can.
    fire: dict[str, Any] = {}
    fm = raw / "fire.marker"
    if fm.exists():
        try:
            fire = json.loads(fm.read_text(encoding="utf-8"))
        except ValueError:
            fire = {}
    s.fire = fire
    fire_ms: Optional[int] = fire.get("ts_epoch_ms")

    ticks = parse_probe_log(raw / "probe.log")
    samples = parse_patroni_samples(raw / "patroni_samples.jsonl")
    artifacts = parse_probe_artifacts(raw / "probe_artifacts.log")
    s.probe_artifacts = artifacts

    # ── effective-kill (T0') auto-detection ──
    # The fire marker records T0 = when the instruction was ISSUED. For an
    # out-of-band kill (node panic, droplet action) the old primary keeps
    # serving writes for tens of seconds; anchoring downtime on T0 would
    # charge that delivery delay to the outage. The TRUE kill instant is the
    # last successful write to the old primary — re-baseline on it when the
    # delay is material (>2s). (DBAAS-8917: +42s power-off, +24s sysrq.)
    t0_issued = fire_ms
    effective_ms: Optional[int] = None
    if fire_ms is not None and ticks:
        first_fail_any = min((t.local_ms for t in ticks
                              if not t.ok and t.local_ms >= fire_ms), default=None)
        anchor = first_fail_any if first_fail_any is not None else fire_ms
        last_ok = max((t.local_ms for t in ticks
                       if t.ok and t.local_ms < anchor), default=None)
        if last_ok is not None:
            effective_ms = last_ok
    kill_delay_ms = (effective_ms - t0_issued) \
        if (effective_ms is not None and t0_issued is not None) else None
    rebased = kill_delay_ms is not None and kill_delay_ms > 2000
    if rebased:
        fire_ms = effective_ms
    s.fire = {**fire, "t0_issued_ms": t0_issued, "effective_kill_ms": effective_ms,
              "kill_delay_ms": kill_delay_ms, "rebased_on_effective_kill": rebased}

    # Clock skew: median (db - local) across OK ticks.
    skews = [t.db_ms - t.local_ms for t in ticks
             if t.ok and t.db_ms is not None]
    s.clock = {"db_minus_local_ms_median": int(statistics.median(skews)) if skews else None,
               "ok_ticks": sum(1 for t in ticks if t.ok),
               "fail_ticks": sum(1 for t in ticks if not t.ok)}

    # ── probe window decomposition ──
    artifact_windows: list[tuple[int, int]] = []
    for a in artifacts:
        artifact_windows.append((a["ts_epoch_ms"] - 1000, a["ts_epoch_ms"] + 3000))

    def in_artifact(ms: int) -> bool:
        return any(lo <= ms <= hi for lo, hi in artifact_windows)

    if fire_ms is not None and ticks:
        # The outage of interest is the one the FIRE caused: anchor the first
        # FAIL at/after the fire instant. A transient FAIL during BASELINE (before
        # the fire) is not this outage — counting it would over-report downtime
        # and produce a NEGATIVE detection (first_fail - fire_ms < 0). Field bug.
        raw_fails = [t for t in ticks if not t.ok and t.local_ms >= fire_ms]
        fails = [t for t in raw_fails if not in_artifact(t.local_ms)]
        first_fail = min((t.local_ms for t in fails), default=None)
        last_ok_before = first_ok_after = None
        if first_fail is not None:
            # last OK before the outage began (may be between fire and first_fail)
            last_ok_before = max((t.local_ms for t in ticks
                                  if t.ok and t.local_ms <= first_fail), default=None)
            first_ok_after = min((t.local_ms for t in ticks
                                  if t.ok and t.local_ms > first_fail), default=None)
            downtime_ms: Optional[int] = (
                first_ok_after - last_ok_before
                if first_ok_after is not None and last_ok_before is not None else None)
        elif raw_fails:
            # FAILs occurred but were all probe artifacts (port-forward restarts)
            # -> the real outage window is obscured; report unmeasurable, not 0.
            downtime_ms = None
        else:
            # fire happened, probe ran, NO FAIL after it -> genuinely zero outage
            # (distinct from "unmeasurable" / no ticks, which stays None).
            ok_after = [t for t in ticks if t.ok and t.local_ms >= fire_ms]
            downtime_ms = 0 if ok_after else None
            last_ok_before = max((t.local_ms for t in ticks
                                  if t.ok and t.local_ms <= fire_ms), default=None)
        s.probe = {
            "last_ok_before_ms": last_ok_before, "first_fail_ms": first_fail,
            "first_ok_after_ms": first_ok_after,
            "client_downtime_ms": downtime_ms,
            "detection_ms": (first_fail - fire_ms) if first_fail is not None else None,
            "fail_count": len(fails),
        }
    else:
        s.probe = {"client_downtime_ms": None,
                   "note": "no fire marker or no probe ticks"}

    # ── per-path probe decomposition (dual-path helper: pgbouncer vs ha) ──
    # Field lesson: the pgBouncer path carries a server_login_retry backoff
    # tail the direct -ha path does not; report each path's downtime
    # separately so the delta is visible, not averaged away.
    per_path: dict[str, Any] = {}
    for extra in sorted(raw.glob("probe_*.log")):
        label = extra.stem.replace("probe_", "")
        pt = parse_probe_log(extra)
        if not pt or fire_ms is None:
            continue
        pf = min((t.local_ms for t in pt if not t.ok and t.local_ms >= fire_ms),
                 default=None)
        if pf is None:
            per_path[label] = {"client_downtime_ms": 0}
            continue
        lob = max((t.local_ms for t in pt if t.ok and t.local_ms <= pf),
                  default=None)
        foa = min((t.local_ms for t in pt if t.ok and t.local_ms > pf),
                  default=None)
        per_path[label] = {
            "client_downtime_ms": (foa - lob) if (foa and lob) else None,
            "detection_ms": pf - fire_ms,
            "first_ok_after_ms": foa,
        }
    if per_path:
        s.probe["per_path"] = per_path

    # ── patroni: authoritative leader before/after + promote ──
    leader_before = fire.get("leader_before", "")
    tl_before = fire.get("tl_before")
    pre = [p for p in samples if fire_ms is None or p["ts_epoch_ms"] <= fire_ms]
    post = [p for p in samples if fire_ms is not None and p["ts_epoch_ms"] > fire_ms]
    if not leader_before and pre:
        leader_before = pre[-1].get("leader", "")
    if tl_before is None and pre:
        tl_before = pre[-1].get("timeline")
    leader_after = post[-1].get("leader", "") if post else ""
    tl_after = post[-1].get("timeline") if post else None
    promote_ms = None
    if fire_ms is not None:
        promote_ms = scan_patroni_logs(raw, fire_ms)
        if promote_ms is None and leader_before and leader_after \
                and leader_after != leader_before:
            promote_ms = next((p["ts_epoch_ms"] for p in post
                               if p.get("leader") == leader_after), None)
    s.patroni = {"leader_before": leader_before, "leader_after": leader_after,
                 "tl_before": tl_before, "tl_after": tl_after,
                 "promote_ms": promote_ms}
    if s.probe.get("first_ok_after_ms") is not None and promote_ms is not None:
        s.probe["recovery_after_promote_ms"] = \
            s.probe["first_ok_after_ms"] - promote_ms

    # ── classification: LEADER NAME decides; TL corroborates ──
    # Three-way (stitch-case-c parity): a DIFFERENT member promoted is a true
    # election; the SAME leader returning with a TL bump is an in-place
    # re-promotion, explicitly NOT a failover; same leader, same TL is a plain
    # restart. The flip boolean alone can't tell the second from the third.
    tl_bumped = (tl_before is not None and tl_after is not None
                 and tl_after > tl_before)
    if leader_before and leader_after:
        flip = leader_after != leader_before
        if flip:
            kind = "election"
        elif tl_bumped:
            kind = "restart-in-place-repromote"
        else:
            kind = "restart-in-place"
        s.classification = {
            "flip": flip,
            "kind": kind,
            "basis": "patroni leader name before vs after (authoritative); "
                     "probe IP is deliberately ignored — pod restarts change the "
                     "IP without a failover",
            "tl_change": (tl_before is not None and tl_after is not None
                          and tl_after != tl_before),
            "tl_corroborates": (tl_after > tl_before) == flip
            if tl_before is not None and tl_after is not None else None,
        }
    else:
        s.classification = {"flip": None, "kind": "unknown",
                            "basis": "insufficient patroni data"}

    # ── crash-validity (C1/C2 only): did PostgreSQL get a graceful shutdown? ──
    case = str(fire.get("scenario") or "")
    if case in ("pod-delete", "node-loss"):
        cv = scan_crash_validity(raw, fire_ms)
        s.classification["crash_valid"] = not cv["graceful"]
        if cv["graceful"]:
            s.classification["invalid_as_crash"] = True
            s.classification["graceful_marker"] = cv["line"]

    # ── T7 / full HA recovery: must follow an OBSERVED dip ──
    dip_ms = None
    recover_ms = None
    for p in post:
        ready, total = p.get("ready"), p.get("total")
        healthy = (ready is not None and total is not None and ready >= total
                   and all(str(m.get("state", "")).lower() in
                           ("running", "streaming")
                           for m in p.get("members", [])))
        if not healthy and dip_ms is None:
            dip_ms = p["ts_epoch_ms"]
        if healthy and dip_ms is not None and recover_ms is None:
            recover_ms = p["ts_epoch_ms"]
    s.recovery = {
        "ready_dip_observed_ms": dip_ms,
        "full_ha_recovery_ms": recover_ms,
        "note": ("" if recover_ms is not None else
                 "no post-dip healthy sample observed" if dip_ms is not None else
                 "no ready-count dip observed after fire — full-recovery time "
                 "not latched (stale-sample guard)"),
    }
    if recover_ms is not None and fire_ms is not None:
        s.recovery["full_ha_recovery_s"] = round((recover_ms - fire_ms) / 1000, 1)

    # ── pgBouncer backoff tail ──
    pgb = scan_pgbouncer_logs(raw)
    tail_ms = None
    if pgb.get("last_retry_ms") is not None and \
            s.probe.get("first_ok_after_ms") is not None:
        tail_ms = max(0, pgb["last_retry_ms"] - s.probe["first_ok_after_ms"])
    s.pgbouncer = {**pgb, "backoff_tail_ms": tail_ms,
                   "note": ("server_login_retry negative-cache: even fast-"
                            "retrying clients are rejected for this window "
                            "AFTER the database recovered" if tail_ms else "")}

    # ── merged timeline (only the lines that matter) ──
    tl: list[dict[str, Any]] = []

    def add(ms: Optional[int], source: str, event: str, detail: str = "") -> None:
        if ms is not None:
            tl.append({"ts_epoch_ms": ms, "source": source, "event": event,
                       "detail": detail})

    add(fire_ms, "fire", f"FIRE: {fire.get('scenario', '?')}",
        f"target {fire.get('target_pod', '?')}")
    add(s.probe.get("last_ok_before_ms"), "probe", "last OK write before fire")
    add(s.probe.get("first_fail_ms"), "probe", "first FAILed write")
    add(pgb.get("first_crash_ms"), "pgbouncer", "first server-crash line")
    add(promote_ms, "patroni", "leader promote",
        s.patroni.get("leader_after", ""))
    add(s.probe.get("first_ok_after_ms"), "probe", "first OK write after outage")
    add(pgb.get("last_retry_ms"), "pgbouncer", "last login-retry line",
        "backoff tail end")
    add(dip_ms, "k8s", "ready-count dip observed")
    add(recover_ms, "k8s", "all members healthy (post-dip)")
    for a in artifacts:
        add(a["ts_epoch_ms"], "probe-artifact", a["label"],
            "excluded from downtime")
    # dedupe on (ms, source, event) then sort
    seen = set()
    uniq = []
    for e in sorted(tl, key=lambda e: e["ts_epoch_ms"]):
        key = (e["ts_epoch_ms"], e["source"], e["event"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    s.timeline = uniq
    return s


def _fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) \
        .strftime("%H:%M:%S.%f")[:-3]


def render_timeline_txt(s: Stitched) -> str:
    fire_ms = s.fire.get("ts_epoch_ms")
    lines = []
    lines.append("=" * 72)
    lines.append(f"SCENARIO   : {s.fire.get('scenario', '?')}  "
                 f"target={s.fire.get('target_pod', '?')}")
    lines.append(f"FIRE (UTC) : {s.fire.get('ts_utc', '?')}")
    cls = s.classification
    _KINDLABEL = {
        "election": "YES — real election (a different member promoted)",
        "restart-in-place-repromote": "NO — restart in place (SAME leader "
                                      "re-promoted with a TL bump; not a failover)",
        "restart-in-place": "NO — restart in place",
        "unknown": "unknown",
    }
    lines.append(f"FLIP?      : "
                 + _KINDLABEL.get(cls.get("kind", "unknown"), cls.get("kind", "?"))
                 + f"   (leader {s.patroni.get('leader_before', '?')} -> "
                   f"{s.patroni.get('leader_after', '?')}; "
                   f"TL {s.patroni.get('tl_before', '?')} -> "
                   f"{s.patroni.get('tl_after', '?')})")
    if cls.get("invalid_as_crash"):
        lines.append("!! INVALID AS CRASH — PostgreSQL received a GRACEFUL "
                     "shutdown in the kill window; this run does NOT measure a "
                     "crash. " + (cls.get("graceful_marker", "")[:80]))
    elif cls.get("crash_valid") is True:
        lines.append("CRASH OK   : no graceful-shutdown markers in the kill "
                     "window — valid crash semantics")
    if s.fire.get("rebased_on_effective_kill"):
        lines.append(f"KILL DELAY : old primary served writes until "
                     f"+{s.fire['kill_delay_ms'] / 1000:.1f}s after the "
                     "instruction (out-of-band kill); offsets measured from T0'")
    dt = s.probe.get("client_downtime_ms")
    lines.append(f"DOWNTIME   : "
                 + (f"{dt / 1000:.1f}s client write downtime" if dt is not None
                    else "n/a")
                 + (f"  (detection {s.probe['detection_ms'] / 1000:.1f}s)"
                    if s.probe.get("detection_ms") is not None else ""))
    tail = s.pgbouncer.get("backoff_tail_ms")
    if tail:
        lines.append(f"BACKOFF    : +{tail / 1000:.1f}s pgBouncer "
                     f"server_login_retry tail AFTER recovery")
    rec = s.recovery.get("full_ha_recovery_s")
    lines.append(f"FULL HA    : "
                 + (f"{rec}s to all-members-healthy (post-dip latch)"
                    if rec is not None else s.recovery.get("note", "n/a")))
    skew = s.clock.get("db_minus_local_ms_median")
    if skew is not None:
        lines.append(f"CLOCK SKEW : db-local median {skew:+d} ms")
    lines.append("=" * 72)
    lines.append("")
    for e in s.timeline:
        rel = ""
        if fire_ms is not None:
            rel = f"{(e['ts_epoch_ms'] - fire_ms) / 1000:+8.1f}s"
        lines.append(f"{_fmt_ms(e['ts_epoch_ms'])}  {rel}  "
                     f"[{e['source']:>14}] {e['event']}"
                     + (f" — {e['detail']}" if e.get("detail") else ""))
    return "\n".join(lines) + "\n"


def render_events_csv(s: Stitched) -> str:
    rows = ["ts_utc,ts_epoch_ms,rel_s,source,event,detail"]
    fire_ms = s.fire.get("ts_epoch_ms")
    for e in s.timeline:
        rel = (f"{(e['ts_epoch_ms'] - fire_ms) / 1000:.3f}"
               if fire_ms is not None else "")
        detail = str(e.get("detail", "")).replace(",", ";")
        event = str(e["event"]).replace(",", ";")
        iso = datetime.fromtimestamp(e["ts_epoch_ms"] / 1000, tz=timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        rows.append(f"{iso},{e['ts_epoch_ms']},{rel},{e['source']},{event},{detail}")
    return "\n".join(rows) + "\n"


def stitch_run_dir(run_dir: Path) -> Stitched:
    """Stitch + write the derived artifacts (regenerable any time from raw/)."""
    s = stitch(run_dir)
    atomic_write_json(run_dir / "stitched.json", s.to_dict())
    atomic_write_text(run_dir / "TIMELINE.txt", render_timeline_txt(s))
    atomic_write_text(run_dir / "events.csv", render_events_csv(s))
    return s


def restitch_run_dir(run_dir: Path) -> int:
    """CLI: ``ops stitch --run-dir`` — re-derive from raw captures."""
    if not (run_dir / "raw").is_dir():
        print(f"error: {run_dir} has no raw/ capture directory")
        return 2
    s = stitch_run_dir(run_dir)
    print((run_dir / "TIMELINE.txt").read_text(encoding="utf-8"))
    dt = s.probe.get("client_downtime_ms")
    print(f"stitched: downtime={dt} ms, flip={s.classification.get('flip')}")
    return 0
