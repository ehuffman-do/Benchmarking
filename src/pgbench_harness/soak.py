"""Resilience/soak analysis: build the absolute-time per-second timeline from the
soak raw logs and compute a precise, defensible disruption profile per event.

Every metric definition below is intentionally explicit — these numbers go in
front of leadership, so a reader must know exactly what each one means.

Timeline model
--------------
Soak runs are keyed on **absolute wall-clock UTC**, not sysbench's relative
``[ Ns ]`` offset. Each raw line is ``<ISO-UTC>\\t<sysbench line>`` (stamped at
read time by the load generator). We convert each interval sample to an integer
second offset from ``soak_start`` and build a dense series 0..T. A second with
no sample (sysbench silent / between supervisor relaunches) is a **gap** and is
treated as "no successful transactions observed" — i.e. downtime.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.parser import parse_interval_line
from pgbench_harness.spec import Spec
from pgbench_harness.util import atomic_write_json, atomic_write_text, fmt_duration

EPS = 1e-9  # tps at/below this counts as "no successful transactions"
MIN_BASELINE_SAMPLES = 5  # need at least this many clean pre-event samples to trust a baseline
ANALYSIS_TYPES = ("failover", "scale_up", "scale_down", "note")  # loadgen_restart is internal


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _parse_ts_loose(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unrecognized timestamp: {s!r}")


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    """Read events.jsonl (spec-seeded + `mark`-appended), sorted by time."""
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            ev["_dt"] = _parse_ts_loose(ev["ts_utc"])
            events.append(ev)
        except (ValueError, KeyError):
            continue
    events.sort(key=lambda e: e["_dt"])
    return events


def build_timeline(run_dir: Path, soak_start: datetime) -> dict[int, dict[str, Any]]:
    """Parse all raw/soak_seg*.log into {second_offset: sample row}.

    Offset = round((read_time_utc - soak_start) seconds). On the rare collision
    (overlapping relaunch) the first-seen sample wins.
    """
    timeline: dict[int, dict[str, Any]] = {}
    for log in sorted((run_dir / "raw").glob("soak_seg*.log")):
        seg = log.stem
        for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
            ts_str, _, rest = raw.partition("\t")
            if not rest:
                continue
            try:
                ts = _parse_ts(ts_str)
            except ValueError:
                continue
            sample = parse_interval_line(rest)
            if sample is None:
                continue
            off = int(round((ts - soak_start).total_seconds()))
            if off < 0 or off in timeline:
                continue
            timeline[off] = {
                "t": off, "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "tps": sample.tps, "qps": sample.qps, "lat_p99": sample.lat_ms,
                "err_s": sample.err_s, "reconn_s": sample.reconn_s,
                "threads": sample.threads, "seg": seg,
            }
    return timeline


def _present(tl: dict[int, dict[str, Any]], o: int) -> bool:
    return o in tl


def _tps(tl: dict[int, dict[str, Any]], o: int) -> float:
    """Observed successful TPS at second o; a gap means 0 served."""
    row = tl.get(o)
    return row["tps"] if row else 0.0


def resolve_baseline_window(
    tl: dict[int, dict[str, Any]], total_s: int, events: list[dict[str, Any]],
    cfg_window: Optional[tuple[int, int]],
) -> tuple[int, int]:
    """Choose the pre-event baseline span.

    Explicit config wins. Otherwise: a clean window ending shortly before the
    first event, or — with no events — the middle 60% of the run.
    """
    if cfg_window is not None:
        return cfg_window
    if events:
        first = min(e["_offset"] for e in events)
        end = first - 5                    # strictly BEFORE the event (5s guard)
        if end <= 1:                        # event too early — no clean pre-event span
            return (0, 0)                   # degenerate; flagged downstream
        start = max(0, end - 600)           # up to ~10 min of clean pre-event baseline
        return start, end
    return int(total_s * 0.2), int(total_s * 0.8)


def _median_over(tl: dict[int, dict[str, Any]], a: int, b: int, key: str) -> Optional[float]:
    vals = [tl[o][key] for o in range(a, b + 1) if o in tl]
    return statistics.median(vals) if vals else None


def disruption_metrics(
    tl: dict[int, dict[str, Any]], event_off: int, win_end: int,
    baseline_tps: float, baseline_lat: float, cfg: Any,
) -> dict[str, Any]:
    """Compute the disruption profile for one event over [event_off, win_end].

    Definitions (all seconds are 1 Hz samples; a gap = no successful txns):
      hard_downtime_s        consecutive seconds from the first 'down' second
                             (gap or tps<=0) after the event until throughput
                             resumes (>0).
      time_to_first_success  event -> first second with tps>0 after downtime.
      error_window_s         span [first..last] second with err/s or reconn/s >0.
      error_seconds          count of seconds with any error/reconnect.
      reconnect_count        sum of reconn/s over the window.
      ttr_s                  event -> first second where tps >= threshold% of
                             baseline AND holds for recovery_hold_s seconds.
      full_recovery_s        same, at full_recovery% of baseline (cache re-warm
                             tail; always >= ttr_s).
      peak_p99_ms            max per-second p99 latency in the window.
      seconds_p99_above      seconds with p99 > latency_spike_mult x baseline p99
                             (None when there is no trustworthy baseline).
      txns_failed            sum of err/s over the window — sysbench-reported, so
                             observable ONLY while the load generator is alive
                             (present seconds). During a gap-based outage sysbench
                             isn't running, so this is ~0; gap downtime is captured
                             by hard_downtime_s and missed_vs_baseline instead.
      missed_vs_baseline     sum of max(0, baseline_tps - observed_tps) over the
                             window (gaps count as 0 served) — estimated
                             transactions NOT served vs baseline. None when there
                             is no trustworthy baseline.
    """
    thresh = baseline_tps * cfg.recovery_threshold_pct / 100.0
    full = baseline_tps * cfg.full_recovery_pct / 100.0
    hold = cfg.recovery_hold_s

    def is_down(o: int) -> bool:
        return (not _present(tl, o)) or _tps(tl, o) <= EPS

    # hard downtime: first contiguous down-run at/after the event
    downtime_s = 0
    downtime_start: Optional[int] = None
    time_to_first_success: Optional[int] = None
    o = event_off
    while o <= win_end and not is_down(o):
        o += 1
    if o <= win_end:
        downtime_start = o
        while o <= win_end and is_down(o):
            o += 1
        downtime_s = o - downtime_start
        if o <= win_end:
            time_to_first_success = o - event_off

    # error / reconnect window
    err_offs = [x for x in range(event_off, win_end + 1)
                if x in tl and (tl[x]["err_s"] > 0 or tl[x]["reconn_s"] > 0)]
    error_window_s = (err_offs[-1] - err_offs[0] + 1) if err_offs else 0
    error_seconds = len(err_offs)
    reconnect_count = round(sum(tl[x]["reconn_s"] for x in range(event_off, win_end + 1)
                               if x in tl), 1)

    def recovered_at(target: float) -> Optional[int]:
        """First offset (from event) where TPS holds >= target.

        The sustain requirement is clamped to the seconds actually available
        before win_end, so a healthy tail shorter than recovery_hold_s near the
        end of the run/window still counts as recovered (it must not be reported
        as 'never recovered' simply because the window ended). A gap inside the
        sustain window fails the check, so recovery is detected from the first
        gap-free, at-or-above-target run — consistent with downtime semantics.
        """
        for s in range(event_off, win_end + 1):
            eff = min(hold, win_end - s + 1)
            if eff <= 0:
                break
            if all((s + k) in tl and tl[s + k]["tps"] >= target for k in range(eff)):
                return s - event_off
        return None

    have_baseline = baseline_tps > 0
    ttr = recovered_at(thresh) if have_baseline else None
    full_recovery = recovered_at(full) if have_baseline else None

    lat_vals = [(x, tl[x]["lat_p99"]) for x in range(event_off, win_end + 1) if x in tl]
    peak_p99 = max((v for _, v in lat_vals), default=None)
    peak_at = max(lat_vals, key=lambda p: p[1])[0] - event_off if lat_vals else None
    spike_threshold = baseline_lat * cfg.latency_spike_mult if baseline_lat > 0 else None
    seconds_p99_above = (sum(1 for _, v in lat_vals if v > spike_threshold)
                         if spike_threshold is not None else None)

    txns_failed = round(sum(tl[x]["err_s"] for x in range(event_off, win_end + 1)
                           if x in tl), 1)
    # missed work needs a baseline to compare against; without one it's undefined.
    missed = (round(sum(max(0.0, baseline_tps - _tps(tl, x))
                       for x in range(event_off, win_end + 1)), 0)
              if have_baseline else None)

    return {
        "hard_downtime_s": downtime_s,
        "downtime_start_offset": (downtime_start - event_off) if downtime_start is not None else None,
        "time_to_first_success_s": time_to_first_success,
        "error_window_s": error_window_s,
        "error_seconds": error_seconds,
        "reconnect_count": reconnect_count,
        "ttr_s": ttr,
        "full_recovery_s": full_recovery,
        "peak_p99_ms": round(peak_p99, 1) if peak_p99 is not None else None,
        "peak_p99_at_s": peak_at,
        "seconds_p99_above": seconds_p99_above,
        "txns_failed": txns_failed,
        "missed_vs_baseline": missed,
        "window_end_offset": win_end - event_off,
    }


def _verdict(ev: dict[str, Any], m: dict[str, Any], cfg: Any) -> str:
    """One-paragraph plain-language summary — the sentence for a status update."""
    name = ev.get("label") or ev["type"].replace("_", " ")
    parts: list[str] = []
    if m["hard_downtime_s"] > 0:
        parts.append(f"{m['hard_downtime_s']}s hard downtime")
        if m["time_to_first_success_s"] is not None:
            parts.append(f"clients reconnected/served by {fmt_duration(m['time_to_first_success_s'])}")
    else:
        parts.append("no hard downtime")
    if m["missed_vs_baseline"] is None:        # no trustworthy baseline
        parts.append("recovery not assessed (no clean baseline; set "
                     "report.baseline_window_s)")
        tail = f" {int(m['txns_failed']):,} sysbench errors observed."
        return f"{name}: " + "; ".join(parts) + "." + tail
    if m["ttr_s"] is not None:
        parts.append(f"throughput recovered to {cfg.recovery_threshold_pct:.0f}% of baseline "
                     f"at {fmt_duration(m['ttr_s'])}")
    else:
        parts.append(f"did NOT reach {cfg.recovery_threshold_pct:.0f}% of baseline within the window")
    if m["full_recovery_s"] is not None:
        parts.append(f"full re-warm to baseline at {fmt_duration(m['full_recovery_s'])}")
    elif m["ttr_s"] is not None:
        parts.append("but never fully re-warmed to baseline in-window")
    tail = (f" ~{int(m['missed_vs_baseline']):,} transactions not served vs baseline"
            f"; {int(m['txns_failed']):,} sysbench errors.")
    return f"{name}: " + "; ".join(parts) + "." + tail


def analyze(run_dir: Path, spec: Spec, manifest_soak: dict[str, Any]) -> dict[str, Any]:
    """Build timeline + summary, write soak_timeseries.csv and soak_summary.json."""
    assert spec.soak is not None
    soak_start = _parse_ts_loose(manifest_soak["start_utc"])
    total_s = int(manifest_soak.get("target_duration_s") or spec.soak.duration_s)
    tl = build_timeline(run_dir, soak_start)
    present = sorted(tl)
    observed_end = present[-1] if present else 0
    horizon = max(total_s, observed_end)

    events = read_events(run_dir)
    for e in events:
        e["_offset"] = max(0, int(round((e["_dt"] - soak_start).total_seconds())))

    warnings: list[str] = []
    bw = resolve_baseline_window(tl, horizon, events, spec.report.baseline_window_s)
    baseline_samples = sum(1 for o in range(bw[0], bw[1] + 1) if o in tl)
    baseline_tps = _median_over(tl, bw[0], bw[1], "tps") or 0.0
    baseline_lat = _median_over(tl, bw[0], bw[1], "lat_p99") or 0.0
    baseline_ok = baseline_samples >= MIN_BASELINE_SAMPLES and baseline_tps > 0
    if not baseline_ok:
        baseline_tps = baseline_lat = 0.0   # force baseline-dependent metrics to None
        warnings.append(
            "no trustworthy pre-event baseline (event too early, or the baseline "
            "window fell in a gap). Recovery, latency-spike and missed-vs-baseline "
            "metrics are not computed — set report.baseline_window_s to a clean span.")

    # loadgen_restart markers are internal (supervisor relaunches), not user events:
    # they annotate the overview but get no disruption metrics or zoom.
    analysis = [e for e in events if e["type"] in ANALYSIS_TYPES]
    restart_markers = [{"at_s": e["_offset"], "ts_utc": e["ts_utc"], "label": e.get("label", "")}
                       for e in events if e["type"] == "loadgen_restart"]

    # Per-event window = [event, next analysis event or end of timeline].
    ordered = sorted(analysis, key=lambda e: e["_offset"])
    out_events = []
    for i, ev in enumerate(ordered):
        nxt = ordered[i + 1]["_offset"] - 1 if i + 1 < len(ordered) else horizon
        win_end = max(ev["_offset"], min(nxt, horizon))
        metrics = disruption_metrics(tl, ev["_offset"], win_end, baseline_tps,
                                     baseline_lat, spec.report)
        entry = {
            "type": ev["type"], "label": ev.get("label", ""), "note": ev.get("note", ""),
            "source": ev.get("source", "spec"), "ts_utc": ev["ts_utc"],
            "at_s": ev["_offset"], "metrics": metrics,
        }
        entry["verdict"] = _verdict(entry, metrics, spec.report)
        out_events.append(entry)

    span = horizon + 1                       # dense timeline is 0..horizon inclusive
    coverage = min(1.0, len(present) / span) if span else 0.0
    if not present:
        status = "failed"
    elif coverage < 0.9 or manifest_soak.get("relaunches", 0) > 0 or not baseline_ok:
        status = "partial"
    else:
        status = "complete"

    _write_timeseries(run_dir, tl, horizon)
    summary = {
        "run_id": manifest_soak.get("run_id"),
        "label": spec.run.label, "edition": spec.run.edition,
        "tshirt_size": spec.run.tshirt_size, "notes": spec.run.notes,
        "mode": "soak", "status": status,
        "workload": dict(spec.raw.get("workload", {})),
        "soak": dict(spec.raw.get("soak", {})),
        "soak_start_utc": manifest_soak["start_utc"],
        "soak_finish_utc": manifest_soak.get("finish_utc", ""),
        "target_duration_s": total_s,
        "observed_seconds": len(present),
        "horizon_s": horizon,
        "coverage_pct": round(coverage * 100, 1),
        "gaps_s": span - len(present),
        "relaunches": manifest_soak.get("relaunches", 0),
        "segments": manifest_soak.get("segments", []),
        "warnings": warnings,
        "baseline": {
            "window_s": [bw[0], bw[1]], "tps": round(baseline_tps, 1),
            "lat_p99_ms": round(baseline_lat, 1),
            "samples": baseline_samples, "ok": baseline_ok,
        },
        "restart_markers": restart_markers,
        "thresholds": {
            "recovery_threshold_pct": spec.report.recovery_threshold_pct,
            "full_recovery_pct": spec.report.full_recovery_pct,
            "recovery_hold_s": spec.report.recovery_hold_s,
            "latency_spike_mult": spec.report.latency_spike_mult,
        },
        "events": out_events,
    }
    atomic_write_json(run_dir / "parsed" / "soak_summary.json", summary)
    return summary


def _write_timeseries(run_dir: Path, tl: dict[int, dict[str, Any]], horizon: int) -> None:
    """Full-resolution per-second series (present rows only) for overlay/export."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["t", "ts_utc", "tps", "qps", "lat_p99", "err_s", "reconn_s", "threads", "seg"])
    for o in sorted(tl):
        r = tl[o]
        w.writerow([o, r["ts_utc"], r["tps"], r["qps"], r["lat_p99"],
                    r["err_s"], r["reconn_s"], r["threads"], r["seg"]])
    atomic_write_text(run_dir / "parsed" / "soak_timeseries.csv", buf.getvalue())
