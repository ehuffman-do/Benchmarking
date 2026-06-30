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


UPLOT_MAX_POINTS = 2000


def _load_timeseries(run_dir: Path) -> dict[int, dict[str, Any]]:
    path = run_dir / "parsed" / "soak_timeseries.csv"
    if not path.exists():
        return {}

    def _f(row: dict[str, str], key: str) -> Optional[float]:
        v = row.get(key, "")
        try:
            return float(v) if v not in ("", None) else None
        except (TypeError, ValueError):
            return None
    tl: dict[int, dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            # Tolerate a partial trailing line (the live incremental writer appends
            # rows while this is read for an in-flight run) and first-seen-wins dedup
            # so a re-emitted second never overwrites the original sample.
            try:
                t = int(row["t"])
            except (KeyError, ValueError, TypeError):
                continue
            if t < 0 or t in tl:
                continue
            tl[t] = {
                "tps": _f(row, "tps") or 0.0, "qps": _f(row, "qps") or 0.0,
                "lat_p99": _f(row, "lat_p99") or 0.0, "err_s": _f(row, "err_s") or 0.0,
                "reconn_s": _f(row, "reconn_s") or 0.0,
                # extra (B3) columns are absent on old run dirs -> None
                "qps_r": _f(row, "qps_r"), "qps_w": _f(row, "qps_w"), "qps_o": _f(row, "qps_o"),
            }
    return tl


def _read_text_asset(name: str) -> str:
    """Read a vendored template asset (uPlot js/css) for inlining — keeps the
    report a single self-contained, offline file (no CDN)."""
    try:
        return (Path(__file__).parent / "templates" / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_pg_series(run_dir: Path) -> Optional[dict[str, Any]]:
    """Engine-side per-second series (pg_timeseries.csv) for the interactive panel."""
    path = run_dir / "parsed" / "pg_timeseries.csv"
    if not path.exists():
        return {}
    rows: list[dict[str, str]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return {}

    def col(name: str) -> list[Optional[float]]:
        out: list[Optional[float]] = []
        for r in rows:
            v = r.get(name, "")
            try:
                out.append(float(v) if v not in ("", None) else None)
            except (TypeError, ValueError):
                out.append(None)
        return out
    t = [int(float(r["t"])) for r in rows]
    keys = ("active", "cache_hit_pct", "wal_mb_s", "commits_s", "rollbacks_s",
            "blks_read_s", "blks_hit_s", "ckpt_write_ms_s", "ckpt_sync_ms_s",
            "tup_inserted_s", "tup_updated_s", "tup_deleted_s")
    return {"t": t, **{k: col(k) for k in keys}}


def _decimate(xs: list[int], cols: dict[str, list[Any]], maxpts: int
              ) -> tuple[list[int], dict[str, list[Any]]]:
    n = len(xs)
    if n <= maxpts:
        return xs, cols
    stride = n // maxpts + 1
    return xs[::stride], {k: v[::stride] for k, v in cols.items()}


def build_interactive(run_dir: Path, summary: dict[str, Any],
                      tl: dict[int, dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Aligned, decimated per-second arrays + event/detected markers for the inline
    uPlot charts. None when there is no series to plot."""
    if not tl:
        return None
    horizon = int(summary.get("horizon_s") or (max(tl) if tl else 0))
    xs = list(range(horizon + 1))
    keys = ("tps", "qps", "lat_p99", "err_s", "reconn_s", "qps_r", "qps_w", "qps_o")
    cols = {k: [tl[o][k] if o in tl else None for o in xs] for k in keys}
    xs2, cols2 = _decimate(xs, cols, UPLOT_MAX_POINTS)
    markers = ([{"t": e["at_s"], "label": e.get("label") or e["type"], "kind": "event"}
                for e in summary.get("events", [])]
               + [{"t": c["at_s"], "label": c["type"], "kind": "detected"}
                  for c in summary.get("detected", [])])
    pg = _load_pg_series(run_dir)
    if pg and pg.get("t"):
        pxs, pcols = _decimate(pg["t"], {k: v for k, v in pg.items() if k != "t"}, UPLOT_MAX_POINTS)
        pg = {"t": pxs, **pcols}
    return {"t": xs2, **cols2, "markers": markers, "pg": pg or None,
            "baseline_tps": summary.get("baseline", {}).get("tps") or None}


def _soak_start(run_dir: Path):
    """The soak's t=0 anchor (manifest.soak.start_utc) as a datetime, or None."""
    from pgbench_harness.soak import _parse_ts_loose
    try:
        m = read_json(run_dir / "manifest.json")
    except (OSError, ValueError):
        return None
    s = (m.get("soak") or {}).get("start_utc") if isinstance(m, dict) else None
    try:
        return _parse_ts_loose(s) if s else None
    except (ValueError, TypeError):
        return None


def _live_event_markers(run_dir: Path) -> list[dict[str, Any]]:
    """Operator/spec event markers recomputed from events.jsonl against the soak
    start, so a freshly-stamped mark shows on the in-app chart immediately — even
    mid-run, before the next full analysis writes soak_summary.json."""
    from pgbench_harness.soak import ANALYSIS_TYPES, read_events
    start = _soak_start(run_dir)
    if start is None:
        return []
    out = []
    for e in read_events(run_dir):
        if e.get("type") in ANALYSIS_TYPES:
            at = max(0, int(round((e["_dt"] - start).total_seconds())))
            out.append({"at_s": at, "type": e["type"], "label": e.get("label", "")})
    return out


def interactive_payload(run_dir: Path) -> Optional[dict[str, Any]]:
    """uPlot-ready decimated arrays + event/detected markers for the IN-APP soak
    report. Works for a live or finished soak: the series come from the (possibly
    incrementally written) soak_timeseries.csv, markers are recomputed live from
    events.jsonl, and ``detected``/``baseline`` are lifted from soak_summary.json
    when the finalize analysis has produced it. None when there's nothing to plot."""
    tl = _load_timeseries(run_dir)
    if not tl:
        return None
    summary: dict[str, Any] = {}
    sp = run_dir / "parsed" / "soak_summary.json"
    if sp.exists():
        try:
            loaded = read_json(sp)
            summary = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            summary = {}
    # Always recompute markers from events.jsonl (the source of truth) rather than
    # trust the snapshot in soak_summary.json, which is stale between analyses.
    summary = dict(summary)
    summary["events"] = _live_event_markers(run_dir)
    summary["horizon_s"] = int(summary.get("horizon_s") or max(tl))
    payload = build_interactive(run_dir, summary, tl)
    if payload is not None:
        payload["horizon_s"] = summary["horizon_s"]
    return payload


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
        v = m.get(k)
        if v is not None and v > 0:   # 0 (instant recovery) is not a span to shade
            return v
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
    if summary["baseline"].get("ok", True):
        bw = summary["baseline"]["window_s"]
        ax.axvspan(bw[0], bw[1], color="#2e8b57", alpha=0.06, label="baseline window")
    # Internal supervisor relaunch markers (faint), only if not overwhelming.
    markers = summary.get("restart_markers", [])
    if markers and len(markers) <= 40:
        for i, rm in enumerate(markers):
            ax.axvline(rm["at_s"], color="#b0b8c4", linewidth=0.8, linestyle=":",
                       alpha=0.7, label="load-gen relaunch" if i == 0 else None)
    _style_ax(ax, "Throughput over the full run (events marked)", "elapsed time (h:mm:ss)", "TPS")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: _hms(v)))
    ax.set_xlim(0, horizon)
    ax.set_ylim(bottom=0)                       # set limits BEFORE annotating
    for ev in summary["events"]:                # events as vlines + axes-fraction labels
        at = ev["at_s"]
        ax.axvline(at, color=EVENT_COLOR, linewidth=1.4)
        span = _disruption_span(ev)
        if span:
            ax.axvspan(at, at + span, color=EVENT_COLOR, alpha=0.10)
        ax.annotate(ev["label"] or ev["type"], xy=(at, 0.98),
                    xycoords=("data", "axes fraction"), xytext=(3, 0),
                    textcoords="offset points", fontsize=11, color=EVENT_COLOR,
                    rotation=90, va="top")
    # Auto-detected (unconfirmed) anomalies: dashed amber, visually distinct from
    # the solid-red confirmed events so the operator can tell them apart.
    for i, c in enumerate(summary.get("detected", [])):
        ax.axvline(c["at_s"], color="#e8a33d", linewidth=1.1, linestyle="--", alpha=0.85,
                   label="detected (unconfirmed)" if i == 0 else None)
        end = c.get("end_s")
        if end and end > c["at_s"]:
            ax.axvspan(c["at_s"], end, color="#e8a33d", alpha=0.07)
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
    # Gaps (no sample) render as NaN so the line breaks — visually distinct from a
    # present "0 TPS" second (load gen up, but no successful work).
    tps = [tl[o]["tps"] if o in tl else float("nan") for o in xs]
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
    """(Re)generate soak_report.html for a soak run directory.

    Re-runs the analysis from the raw logs + events.jsonl (raw logs are the
    source of truth), so events `mark`ed after the run are picked up on a plain
    `report --run-dir`. Falls back to a stored summary if the spec/manifest are
    unavailable.
    """
    from pgbench_harness import soak
    from pgbench_harness.manifest import Manifest
    from pgbench_harness.spec import load_spec

    run_dir = run_dir.resolve()
    spec_path = run_dir / "spec.yaml"
    summary_path = run_dir / "parsed" / "soak_summary.json"
    if spec_path.exists() and (run_dir / "manifest.json").exists():
        manifest = Manifest.load(run_dir)
        if manifest.soak:                       # refresh from raw logs + events.jsonl
            soak.analyze(run_dir, load_spec(spec_path), manifest.soak)
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
    interactive = build_interactive(run_dir, summary, tl)
    html = _jinja_env().get_template("soak_report.html.j2").render(
        summary=summary,
        charts=charts,
        interactive=interactive,
        uplot_js=_read_text_asset("uplot.min.js") if interactive else "",
        uplot_css=_read_text_asset("uplot.min.css") if interactive else "",
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
