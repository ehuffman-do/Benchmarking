"""Automatic event / anomaly detection over a soak's per-second timeline.

A soak with no marked event is still full of signal. This scans the dense 1 Hz
series and proposes *candidate* events — throughput collapses (downtime), sharp
drops that recover (failover-like), gradual capacity shifts (scale up/down),
latency spikes, and error/reconnect bursts — so the report tells the story first
and the operator confirms/labels them retroactively (via ``mark``).

Everything here is deliberately honest: candidates are ``detected_unconfirmed``
with an explicit confidence, and every threshold is config-driven
(``spec.report``) and echoed into the report for auditability. Detection never
invents an event from a single noisy second: each detector requires a minimum
duration and uses separate enter/exit thresholds (hysteresis).
"""

from __future__ import annotations

import statistics
from typing import Any

EPS = 1e-9  # tps at/below this is "no successful work" (a gap counts as 0)


def _tps(tl: dict[int, dict[str, Any]], o: int) -> float:
    row = tl.get(o)
    return row["tps"] if row else 0.0


def _merge(cands: list[dict[str, Any]], gap: int) -> list[dict[str, Any]]:
    """Merge adjacent same-type candidates whose windows are within *gap* seconds."""
    out: list[dict[str, Any]] = []
    for c in sorted(cands, key=lambda x: (x["type"], x["at_s"])):
        if out and out[-1]["type"] == c["type"] and c["at_s"] - out[-1]["end_s"] <= gap:
            prev = out[-1]
            prev["end_s"] = max(prev["end_s"], c["end_s"])
            prev["confidence"] = max(prev["confidence"], c["confidence"])
        else:
            out.append(dict(c))
    return out


def _disruptions(tl: dict[int, dict[str, Any]], lo: int, hi: int,
                 baseline: float, cfg: Any) -> list[dict[str, Any]]:
    """Contiguous windows where TPS falls below ``detect_drop_pct`` of baseline.

    Each window is classified: mostly-zero -> 'downtime'; recovers sharply ->
    'failover' (failover-like); otherwise a partial 'dip'.
    """
    drop = cfg.detect_drop_pct / 100.0 * baseline
    recover = cfg.detect_recover_pct / 100.0 * baseline
    out: list[dict[str, Any]] = []
    o = lo
    while o <= hi:
        if _tps(tl, o) >= drop:
            o += 1
            continue
        start = o
        while o <= hi and _tps(tl, o) < drop:
            o += 1
        end = o - 1
        dur = end - start + 1
        if dur < cfg.detect_min_event_s:
            continue
        window = [_tps(tl, x) for x in range(start, end + 1)]
        zero_s = sum(1 for x in range(start, end + 1) if x not in tl or tl[x]["tps"] <= EPS)
        min_tps = min(window)
        recovered = end + 1 <= hi and _tps(tl, end + 1) >= recover
        if zero_s >= dur * 0.6:
            typ, conf = "downtime", 0.92
        elif recovered:
            typ, conf = "failover", 0.7
        else:
            typ, conf = "dip", 0.5
        out.append({
            "type": typ, "at_s": start, "end_s": end, "confidence": conf,
            "status": "detected_unconfirmed",
            "label_suggestion": f"{typ} @ {start}s ({dur}s)",
            "evidence": {"duration_s": dur, "zero_seconds": zero_s,
                         "min_tps": round(min_tps, 1),
                         "drop_pct": round(100.0 * (1 - min_tps / baseline), 1) if baseline else None,
                         "recovered": recovered},
        })
    return out


def _latency_spikes(tl: dict[int, dict[str, Any]], lo: int, hi: int,
                    baseline_lat: float, cfg: Any) -> list[dict[str, Any]]:
    thresh = baseline_lat * cfg.latency_spike_mult
    out: list[dict[str, Any]] = []
    o = lo
    while o <= hi:
        if o not in tl or tl[o]["lat_p99"] <= thresh:
            o += 1
            continue
        start = o
        peak = 0.0
        while o <= hi and o in tl and tl[o]["lat_p99"] > thresh:
            peak = max(peak, tl[o]["lat_p99"])
            o += 1
        end = o - 1
        if end - start + 1 < cfg.detect_min_event_s:
            continue
        out.append({
            "type": "latency_spike", "at_s": start, "end_s": end, "confidence": 0.6,
            "status": "detected_unconfirmed",
            "label_suggestion": f"latency spike @ {start}s",
            "evidence": {"duration_s": end - start + 1, "peak_p99_ms": round(peak, 1),
                         "threshold_ms": round(thresh, 1)},
        })
    return out


def _error_bursts(tl: dict[int, dict[str, Any]], lo: int, hi: int,
                  cfg: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    o = lo
    while o <= hi:
        row = tl.get(o)
        if not row or (row["err_s"] <= 0 and row["reconn_s"] <= 0):
            o += 1
            continue
        start = o
        err_secs = 0
        total_err = 0.0
        total_reconn = 0.0
        # allow short gaps (<=2s) inside a burst
        gap = 0
        while o <= hi and gap <= 2:
            row = tl.get(o)
            if row and (row["err_s"] > 0 or row["reconn_s"] > 0):
                err_secs += 1
                total_err += row["err_s"]
                total_reconn += row["reconn_s"]
                gap = 0
                last = o
            else:
                gap += 1
            o += 1
        end = last
        if err_secs < cfg.detect_err_burst:
            continue
        out.append({
            "type": "error_burst", "at_s": start, "end_s": end, "confidence": 0.75,
            "status": "detected_unconfirmed",
            "label_suggestion": f"error burst @ {start}s",
            "evidence": {"error_seconds": err_secs, "errors": round(total_err, 1),
                         "reconnects": round(total_reconn, 1)},
        })
    return out


def _shifts(tl: dict[int, dict[str, Any]], lo: int, hi: int,
            baseline: float, cfg: Any) -> list[dict[str, Any]]:
    """Sustained level shift (scale up/down) without a sharp drop: compare the
    median of a leading vs a trailing window around each candidate boundary."""
    w = cfg.detect_shift_window_s
    if hi - lo < 2 * w:
        return []
    out: list[dict[str, Any]] = []
    shift_abs = cfg.detect_shift_pct / 100.0 * baseline
    o = lo + w
    while o <= hi - w:
        before = statistics.median([_tps(tl, x) for x in range(o - w, o)])
        after = statistics.median([_tps(tl, x) for x in range(o, o + w)])
        delta = after - before
        if abs(delta) >= shift_abs and before > EPS:
            typ = "scale_up" if delta > 0 else "scale_down"
            out.append({
                "type": typ, "at_s": o, "end_s": o + w, "confidence": 0.45,
                "status": "detected_unconfirmed",
                "label_suggestion": f"{typ} @ {o}s",
                "evidence": {"before_tps": round(before, 1), "after_tps": round(after, 1),
                             "change_pct": round(100.0 * delta / before, 1)},
            })
            o += w  # don't re-report the same transition every second
        else:
            o += max(1, w // 4)
    return out


def detect_anomalies(tl: dict[int, dict[str, Any]], baseline_tps: float,
                     baseline_lat: float, horizon: int, cfg: Any) -> list[dict[str, Any]]:
    """Scan the per-second timeline for candidate events; newest detectors first.

    Detectors that need a baseline (drop/shift/latency) are skipped when there is
    no trustworthy baseline (baseline_*==0), leaving only the baseline-free
    downtime and error-burst detectors.
    """
    present = sorted(tl)
    if not present:
        return []
    lo, hi = present[0], horizon
    cands: list[dict[str, Any]] = []
    if baseline_tps > 0:
        cands += _disruptions(tl, lo, hi, baseline_tps, cfg)
        cands += _shifts(tl, lo, hi, baseline_tps, cfg)
    else:
        # no baseline: still surface hard zero-runs (downtime is baseline-free)
        cands += _disruptions(tl, lo, hi, 1.0, cfg)
    if baseline_lat > 0:
        cands += _latency_spikes(tl, lo, hi, baseline_lat, cfg)
    cands += _error_bursts(tl, lo, hi, cfg)
    cands = _merge(cands, gap=5)
    cands.sort(key=lambda c: (c["at_s"], -c["confidence"]))
    return cands[: cfg.detect_max_events]
