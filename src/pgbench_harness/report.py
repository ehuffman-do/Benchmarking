"""Per-run HTML report generation: self-contained, charts embedded as base64 PNG."""

from __future__ import annotations

import base64
import csv
import io
import statistics
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from jinja2 import Environment, PackageLoader, select_autoescape  # noqa: E402
from matplotlib.ticker import ScalarFormatter  # noqa: E402

from pgbench_harness.capture import KEY_SETTINGS  # noqa: E402
from pgbench_harness.errors import ReportError  # noqa: E402
from pgbench_harness.manifest import STATUS_OK, Manifest  # noqa: E402
from pgbench_harness.spec import Spec, load_spec  # noqa: E402
from pgbench_harness.summarize import write_parsed  # noqa: E402
from pgbench_harness.util import atomic_write_text, fmt_duration  # noqa: E402

REP_COLORS = ["#7fb2f0", "#f0a37f", "#8fd6a5", "#c79fe0"]
MEAN_COLOR = "#0061eb"
PCT_COLORS = {"p50": "#2eb67d", "p95": "#e8a33d", "p99": "#d6453d"}
FIGSIZE = (11.0, 4.8)
DPI = 120

matplotlib.rcParams.update({
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.edgecolor": "#aab4c0",
    "axes.labelcolor": "#2b3440",
    "axes.titlecolor": "#16202b",
    "axes.titleweight": "bold",
    "grid.color": "#dfe5ec",
    "grid.linewidth": 0.8,
    "xtick.color": "#5b6573",
    "ytick.color": "#5b6573",
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Helvetica", "Arial"],
    "figure.facecolor": "white",
})


def _jinja_env() -> Environment:
    return Environment(
        loader=PackageLoader("pgbench_harness", "templates"),
        autoescape=select_autoescape(["html"]),
    )


def fig_to_base64(fig: "plt.Figure") -> str:
    """Render a figure to a base64-encoded PNG data URI."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _style_ax(ax: "plt.Axes", title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, fontsize=16, pad=12)
    ax.set_xlabel(xlabel, fontsize=14)
    ax.set_ylabel(ylabel, fontsize=14)
    ax.tick_params(labelsize=12)
    ax.grid(True, alpha=0.3)
    ax.set_facecolor("white")


def _log_x(ax: "plt.Axes", levels: list[int]) -> None:
    ax.set_xscale("log", base=2)
    ax.set_xticks(levels)
    ax.xaxis.set_major_formatter(ScalarFormatter())
    ax.minorticks_off()


def _ok_levels(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [l for l in summary["levels"] if l["status"] == STATUS_OK and l.get("qps_avg") is not None]


def chart_metric_vs_threads(summary: dict[str, Any], metric: str, title: str, ylabel: str) -> Optional[str]:
    """QPS/TPS vs threads (log-x): one line per repetition plus the mean."""
    ok = _ok_levels(summary)
    if not ok:
        return None
    reps = sorted({l["rep"] for l in ok})
    fig, ax = plt.subplots(figsize=FIGSIZE)
    by_threads: dict[int, list[float]] = {}
    for rep_i, rep in enumerate(reps):
        pts = sorted(((l["threads"], l[metric]) for l in ok if l["rep"] == rep))
        for t, v in pts:
            by_threads.setdefault(t, []).append(v)
        if len(reps) > 1:
            ax.plot(*zip(*pts), marker="o", linewidth=1.4, alpha=0.75,
                    color=REP_COLORS[rep_i % len(REP_COLORS)], label=f"rep {rep}")
    mean_pts = sorted((t, statistics.fmean(vs)) for t, vs in by_threads.items())
    ax.plot(*zip(*mean_pts), marker="o", linewidth=2.6, color=MEAN_COLOR,
            label="mean" if len(reps) > 1 else None, zorder=5)
    peak_t, peak_v = max(mean_pts, key=lambda p: p[1])
    if peak_v > 0:  # a level that ran but did zero work would give peak_v == 0
        ax.annotate(f"peak {peak_v:,.0f}", xy=(peak_t, peak_v), xytext=(0, 12),
                    textcoords="offset points", ha="center", fontsize=12,
                    fontweight="bold", color=MEAN_COLOR)
    _style_ax(ax, title, "client threads (log scale)", ylabel)
    _log_x(ax, sorted(by_threads))
    if len(reps) > 1:
        ax.legend(fontsize=12)
    ax.set_ylim(bottom=0, top=peak_v * 1.18 if peak_v > 0 else None)
    return fig_to_base64(fig)


def chart_latency_vs_threads(summary: dict[str, Any]) -> Optional[str]:
    """Latency percentiles vs threads (log-x), mean across repetitions."""
    ok = _ok_levels(summary)
    if not ok:
        return None
    pcts = summary.get("percentiles", [50, 95, 99])
    fig, ax = plt.subplots(figsize=FIGSIZE)
    plotted = False
    threads_all: set[int] = set()
    for p in pcts:
        by_threads: dict[int, list[float]] = {}
        for l in ok:
            if l.get(f"lat_p{p}") is not None:
                by_threads.setdefault(l["threads"], []).append(l[f"lat_p{p}"])
        if not by_threads:
            continue
        pts = sorted((t, statistics.fmean(vs)) for t, vs in by_threads.items())
        ax.plot(*zip(*pts), marker="o", linewidth=2.2, label=f"p{p}",
                color=PCT_COLORS.get(f"p{p}"))
        threads_all.update(by_threads)
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, "Latency vs client threads", "client threads (log scale)", "latency (ms)")
    _log_x(ax, sorted(threads_all))
    ax.legend(fontsize=12)
    ax.set_ylim(bottom=0)
    return fig_to_base64(fig)


def chart_timeseries(
    samples: list[dict[str, Any]], threads: int, spec: Spec
) -> Optional[str]:
    """QPS over time for one thread level: warm-up greyed, steady window shaded."""
    rows = [s for s in samples if s["threads"] == threads]
    if not rows:
        return None
    reps = sorted({s["rep"] for s in rows})
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for rep_i, rep in enumerate(reps):
        pts = sorted((s["t_offset"], s["qps"]) for s in rows if s["rep"] == rep)
        label = f"rep {rep}" if len(reps) > 1 else None
        ax.plot(*zip(*pts), linewidth=1.2,
                color=REP_COLORS[rep_i % len(REP_COLORS)] if len(reps) > 1 else MEAN_COLOR,
                label=label)
    warmup, dur = spec.sweep.warmup_s, spec.sweep.duration_s
    if warmup > 0:
        ax.axvspan(0, warmup, color="#888888", alpha=0.25, label="warm-up (discarded)")
    ax.axvspan(warmup, dur, color="#2e8b57", alpha=0.07, label="steady-state window")
    _style_ax(ax, f"QPS over time — {threads} threads", "elapsed seconds", "QPS")
    ax.legend(fontsize=11, loc="lower right")
    ax.set_ylim(bottom=0)
    ax.set_xlim(left=0)
    return fig_to_base64(fig)


def load_samples_csv(path: Path) -> list[dict[str, Any]]:
    """Read parsed/samples.csv into typed dict rows."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append({
                "rep": int(row["rep"]), "threads": int(row["threads"]),
                "t_offset": int(row["t_offset"]), "tps": float(row["tps"]),
                "qps": float(row["qps"]), "err_s": float(row["err_s"]),
                "reconn_s": float(row["reconn_s"]), "lat_p99": float(row["lat_p99"]),
            })
    return out


def build_headline_rows(summary: dict[str, Any], spec: Spec) -> list[dict[str, Any]]:
    """Aggregate per thread level across repetitions for the headline table."""
    pcts = summary.get("percentiles", [50, 95, 99])
    threads_order = sorted({l["threads"] for l in summary["levels"]})
    rows = []
    for t in threads_order:
        here = [l for l in summary["levels"] if l["threads"] == t]
        ok = [l for l in here if l["status"] == STATUS_OK and l.get("qps_avg") is not None]
        failed = [l for l in here if l["status"] != STATUS_OK]
        row: dict[str, Any] = {"threads": t, "failed_reps": [l["rep"] for l in failed],
                               "error_excerpt": next((l.get("error_excerpt") for l in failed
                                                      if l.get("error_excerpt")), None)}
        if ok:
            qps = [l["qps_avg"] for l in ok]
            row["qps"] = statistics.fmean(qps)
            row["tps"] = statistics.fmean(l["tps_avg"] for l in ok)
            for p in pcts:
                vals = [l[f"lat_p{p}"] for l in ok if l.get(f"lat_p{p}") is not None]
                row[f"lat_p{p}"] = statistics.fmean(vals) if vals else None
            maxes = [l["lat_max"] for l in ok if l.get("lat_max") is not None]
            row["lat_max"] = max(maxes) if maxes else None
            row["err_s"] = statistics.fmean(
                (l["errors"] or 0) / max(1, l["samples_steady"]) for l in ok)
            row["reconn_s"] = statistics.fmean(
                (l["reconnects"] or 0) / max(1, l["samples_steady"]) for l in ok)
            row["variance_pct"] = (
                (max(qps) - min(qps)) / statistics.fmean(qps) * 100 if len(qps) > 1 else None)
            row["variance_warn"] = (
                row["variance_pct"] is not None
                and row["variance_pct"] > spec.report.variance_warn_pct)
        rows.append(row)
    return rows


def build_error_sections(summary: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Collect nonzero err/reconn intervals and failed levels for the report."""
    bad_intervals = [
        s for s in samples if s["err_s"] > 0 or s["reconn_s"] > 0
    ]
    failed = [l for l in summary["levels"] if l["status"] != STATUS_OK]
    return {
        "bad_intervals": bad_intervals[:200],
        "bad_interval_count": len(bad_intervals),
        "failed_levels": failed,
    }


def build_kpis(headline: list[dict[str, Any]], summary: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Headline KPI cards: peak throughput, latency at peak, error totals."""
    ok_rows = [r for r in headline if "qps" in r]
    if not ok_rows:
        return None
    peak = max(ok_rows, key=lambda r: r["qps"])
    failed = sum(1 for l in summary["levels"] if l["status"] != STATUS_OK)
    errors = sum(l.get("errors") or 0 for l in summary["levels"]
                 if l["status"] == STATUS_OK)
    return {
        "peak_qps": peak["qps"],
        "peak_tps": peak["tps"],
        "peak_threads": peak["threads"],
        "p99_at_peak": peak.get("lat_p99"),
        "failed_levels": failed,
        "total_errors": errors,
    }


def load_prepare_stats(env_dir: Path) -> Optional[dict[str, Any]]:
    """Data-load metrics recorded by `prepare`, when attached to this run."""
    path = env_dir / "prepare_stats.json"
    if not path.exists():
        return None
    try:
        import json

        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return None


def load_pg_settings(env_dir: Path) -> list[dict[str, str]]:
    """Read env/pg_settings.csv into rows (empty list when not captured)."""
    path = env_dir / "pg_settings.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def _read_env(env_dir: Path, name: str) -> str:
    p = env_dir / name
    return p.read_text(encoding="utf-8").strip() if p.exists() else "n/a"


def generate_report(run_dir: Path) -> Path:
    """(Re)generate report.html for a run directory; returns the output path."""
    run_dir = run_dir.resolve()
    spec_path = run_dir / "spec.yaml"
    if not spec_path.exists():
        raise ReportError(f"no spec.yaml in {run_dir}",
                          hint="is this a pgbench-harness run directory?")
    spec = load_spec(spec_path)
    manifest = Manifest.load(run_dir)
    summary = write_parsed(run_dir, spec, manifest)  # raw logs are the source of truth
    samples = load_samples_csv(run_dir / "parsed" / "samples.csv")
    env_dir = run_dir / "env"
    settings = load_pg_settings(env_dir)
    settings_map = {r["name"]: r for r in settings}
    charts = {
        "qps": chart_metric_vs_threads(summary, "qps_avg", "QPS vs client threads", "QPS"),
        "tps": chart_metric_vs_threads(summary, "tps_avg", "TPS vs client threads", "TPS"),
        "latency": chart_latency_vs_threads(summary),
        "timeseries": [
            {"threads": t, "img": chart_timeseries(samples, t, spec)}
            for t in spec.report.timeseries_levels
        ],
    }
    headline = build_headline_rows(summary, spec)
    html = _jinja_env().get_template("report.html.j2").render(
        manifest=manifest,
        spec=spec,
        summary=summary,
        percentiles=summary.get("percentiles", [50, 95, 99]),
        headline=headline,
        kpis=build_kpis(headline, summary),
        prepare_stats=load_prepare_stats(env_dir),
        preflight=manifest.preflight,
        dataset=manifest.preflight.get("dataset") or {},
        warnings=manifest.preflight.get("warnings", []),
        errors=build_error_sections(summary, samples),
        charts=charts,
        key_settings=[settings_map.get(k, {"name": k, "setting": "n/a", "unit": "", "source": ""})
                      for k in KEY_SETTINGS],
        all_settings=settings,
        env={
            "server_version": _read_env(env_dir, "server_version.txt"),
            "sysbench_version": _read_env(env_dir, "sysbench_version.txt"),
            "tpcc_git_sha": _read_env(env_dir, "tpcc_git_sha.txt"),
            "harness_version": _read_env(env_dir, "harness_git_sha.txt"),
            "host_info": _read_env(env_dir, "host_info.txt"),
        },
        wall_time=fmt_duration(manifest.wall_time_s) if manifest.wall_time_s else "n/a",
        steady_window=f"{spec.sweep.warmup_s}s – {spec.sweep.duration_s}s",
    )
    out = run_dir / "report.html"
    atomic_write_text(out, html)  # redacts the registered secret as a final safety net
    return out
