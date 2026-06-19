"""Resilience (soak) report: full-run overview + per-event zoom charts.

Long runs stay legible: the whole-run overview is decimated to <=~1500 plotted
points (bucket mean as the line, bucket *min* shaded so brief dips/outages are
never averaged away; p99 uses bucket max so spikes survive). Per-event charts
are full 1 Hz resolution but bounded to the event window, and every table is
per-event — never per-second. So an 8-hour run renders as a handful of crisp
charts, not 28,800 points of noise.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Optional

import matplotlib.pyplot as plt

from pgbench_harness.errors import ReportError
from pgbench_harness.report import FIGSIZE, MEAN_COLOR, _jinja_env, _style_ax, fig_to_base64
from pgbench_harness.util import atomic_write_text, fmt_duration, read_json

EVENT_COLOR = "#c0392b"
RECOVER_COLOR = "#1e8449"
FULL_COLOR = "#0061eb"
OVERVIEW_TARGET = 1500
ZOOM_PRE_S = 30
ZOOM_MAX_S = 1800


def _load_timeseries(run_dir: Path) -> dict[int, dict[str, Any]]:
    path = run_dir / "parsed" / "soak_timeseries.csv"
    if not path.exists():
        return {}
    tl: dict[int, dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            tl[int(row["t"])] = {
                "tps": float(row["tps"]), "qps": float(row["qps"]),
                "lat_p99": float(row["lat_p99"]), "err_s": float(row["err_s"]),
                "reconn_s": float(row["reconn_s"]),
            }
    return tl


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _bucket(values: list[float], target: int, agg: str) -> tuple[list[int], list[float]]:
    """Decimate a dense 0..N series into <=target buckets (mean/min/max-aware)."""
    n = len(values)
    if n <= target:
        return list(range(n)), values
    size = math.ceil(n / target)
    xs, ys = [], []
    for i in range(0, n, size):
        chunk = [v for v in values[i:i + size] if not math.isnan(v)]
        xs.append(i)
        if not chunk:
            ys.append(float("nan"))
        elif agg == "min":
            ys.append(min(chunk))
        elif agg == "max":
            ys.append(max(chunk))
        else:
            ys.append(sum(chunk) / len(chunk))
    return xs, ys


def _dense(tl: dict[int, dict[str, Any]], horizon: int, key: str, gap: float) -> list[float]:
    return [tl[o][key] if o in tl else gap for o in range(horizon + 1)]


def _disruption_span(ev: dict[str, Any]) -> Optional[int]:
    m = ev["metrics"]
    for k in ("full_recovery_s", "ttr_s", "hard_downtime_s"):
        if m.get(k):
            return m[k]
    return None


def chart_overview(summary: dict[str, Any], tl: dict[int, dict[str, Any]]) -> Optional[str]:
    """Whole-run throughput (decimated) with event markers and disruption shading."""
    horizon = summary["horizon_s"]
    if horizon <= 0 or not tl:
        return None
    tps_dense = _dense(tl, horizon, "tps", 0.0)   # gaps -> 0 (no throughput observed)
    xs_mean, mean = _bucket(tps_dense, OVERVIEW_TARGET, "mean")
    _, mn = _bucket(tps_dense, OVERVIEW_TARGET, "min")
    fig, ax = plt.subplots(figsize=(FIGSIZE[0], 5.0))
    ax.fill_between(xs_mean, mn, mean, color=MEAN_COLOR, alpha=0.18, linewidth=0,
                    label="per-bucket min–mean (dips preserved)")
    ax.plot(xs_mean, mean, color=MEAN_COLOR, linewidth=1.6, label="throughput (TPS)")
    base = summary["baseline"]["tps"]
    if base:
        ax.axhline(base, color="#5b6573", linewidth=1.0, linestyle="--", label=f"baseline {base:,.0f}")
    bw = summary["baseline"]["window_s"]
    ax.axvspan(bw[0], bw[1], color="#2e8b57", alpha=0.06, label="baseline window")
    for ev in summary["events"]:
        at = ev["at_s"]
        ax.axvline(at, color=EVENT_COLOR, linewidth=1.4)
        span = _disruption_span(ev)
        if span:
            ax.axvspan(at, at + span, color=EVENT_COLOR, alpha=0.10)
        ax.annotate(ev["label"] or ev["type"], xy=(at, ax.get_ylim()[1]),
                    xytext=(3, -12), textcoords="offset points", fontsize=11,
                    color=EVENT_COLOR, rotation=90, va="top")
    _style_ax(ax, "Throughput over the full run (events marked)", "elapsed time (h:mm:ss)", "TPS")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: _hms(v)))
    ax.set_xlim(0, horizon)
    ax.set_ylim(bottom=0)
    ax.legend(fontsize=10, loc="upper right")
    return fig_to_base64(fig)


def chart_event_zoom(summary: dict[str, Any], tl: dict[int, dict[str, Any]],
                     ev: dict[str, Any]) -> Optional[str]:
    """Full-resolution view of one event window: TPS + p99, with recovery markers."""
    at = ev["at_s"]
    m = ev["metrics"]
    span = _disruption_span(ev) or 60
    lo = max(0, at - ZOOM_PRE_S)
    hi = min(summary["horizon_s"], at + min(span + 60, ZOOM_MAX_S))
    xs = list(range(lo, hi + 1))
    if len(xs) < 2:
        return None
    tps = [tl[o]["tps"] if o in tl else 0.0 for o in xs]
    lat = [tl[o]["lat_p99"] if o in tl else float("nan") for o in xs]
    fig, ax = plt.subplots(figsize=(FIGSIZE[0], 4.6))
    ax.plot(xs, tps, color=MEAN_COLOR, linewidth=1.8, label="TPS")
    base = summary["baseline"]["tps"]
    if base:
        ax.axhline(base, color="#5b6573", linewidth=1.0, linestyle="--", label=f"baseline {base:,.0f}")
    ax.axvline(at, color=EVENT_COLOR, linewidth=2.0, label=f"event: {ev['label'] or ev['type']}")
    if m.get("hard_downtime_s"):
        d0 = at + (m.get("downtime_start_offset") or 0)
        ax.axvspan(d0, d0 + m["hard_downtime_s"], color=EVENT_COLOR, alpha=0.12,
                   label=f"downtime {m['hard_downtime_s']}s")
    if m.get("ttr_s") is not None:
        ax.axvline(at + m["ttr_s"], color=RECOVER_COLOR, linewidth=1.6, linestyle=":",
                   label=f"{summary['thresholds']['recovery_threshold_pct']:.0f}% @ {_hms(m['ttr_s'])}")
    if m.get("full_recovery_s") is not None:
        ax.axvline(at + m["full_recovery_s"], color=FULL_COLOR, linewidth=1.6, linestyle=":",
                   label=f"full re-warm @ {_hms(m['full_recovery_s'])}")
    _style_ax(ax, f"Event detail — {ev['label'] or ev['type']}", "elapsed time (h:mm:ss)", "TPS")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: _hms(v)))
    ax.set_ylim(bottom=0)
    ax2 = ax.twinx()
    ax2.plot(xs, lat, color="#e8a33d", linewidth=1.2, alpha=0.9, label="p99 latency (ms)")
    ax2.set_ylabel("p99 latency (ms)", fontsize=14, color="#b9770e")
    ax2.tick_params(labelsize=12, colors="#b9770e")
    ax2.set_ylim(bottom=0)
    lines, labels = ax.get_legend_handles_labels()
    l2, lab2 = ax2.get_legend_handles_labels()
    ax.legend(lines + l2, labels + lab2, fontsize=10, loc="lower right")
    return fig_to_base64(fig)


def _read_env(env_dir: Path, name: str) -> str:
    p = env_dir / name
    return p.read_text(encoding="utf-8").strip() if p.exists() else "n/a"


def generate_soak_report(run_dir: Path) -> Path:
    """(Re)generate soak_report.html for a soak run directory."""
    run_dir = run_dir.resolve()
    summary_path = run_dir / "parsed" / "soak_summary.json"
    if not summary_path.exists():
        raise ReportError(f"no parsed/soak_summary.json in {run_dir}",
                          hint="is this a soak run directory?")
    summary = read_json(summary_path)
    tl = _load_timeseries(run_dir)
    env_dir = run_dir / "env"
    charts = {
        "overview": chart_overview(summary, tl),
        "events": [{"ev": ev, "img": chart_event_zoom(summary, tl, ev)}
                   for ev in summary["events"]],
    }
    html = _jinja_env().get_template("soak_report.html.j2").render(
        summary=summary,
        charts=charts,
        fmt_duration=fmt_duration,
        env={
            "server_version": _read_env(env_dir, "server_version.txt"),
            "sysbench_version": _read_env(env_dir, "sysbench_version.txt"),
            "harness_version": _read_env(env_dir, "harness_git_sha.txt"),
            "host_info": _read_env(env_dir, "host_info.txt"),
        },
    )
    out = run_dir / "soak_report.html"
    atomic_write_text(out, html)
    return out
