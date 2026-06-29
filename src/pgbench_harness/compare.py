"""Cross-run comparison report.

Overlaid throughput/latency charts, a latency-vs-throughput efficiency view, a
relative-to-baseline view, per-run KPIs with a winner callout, a side-by-side
headline table, and a pg_settings diff (key settings first). Designed for
"Standard vs Advanced" and "tuned vs default" write-ups, and to merge runs
copied from multiple load generators into one results/ folder.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from pgbench_harness.capture import KEY_SETTINGS, harness_version  # noqa: E402
from pgbench_harness.errors import ReportError  # noqa: E402
from pgbench_harness.manifest import STATUS_OK  # noqa: E402
from pgbench_harness.report import (  # noqa: E402
    FIGSIZE, _jinja_env, _log_x, _style_ax, fig_to_base64, load_pg_settings,
)
from pgbench_harness.util import atomic_write_text, read_json, utc_now_iso  # noqa: E402

# A palette large enough that runs rarely collide; cycles if exceeded.
RUN_COLORS = [
    "#0061eb", "#c0392b", "#1e8449", "#7d3c98", "#b9770e", "#117a8b",
    "#2c3e50", "#d81b60", "#5e8c00", "#00838f",
]


def load_run(run_dir: Path) -> dict[str, Any]:
    """Load one run's summary.json + pg_settings for comparison."""
    summary_path = run_dir / "parsed" / "summary.json"
    if not summary_path.exists():
        raise ReportError(
            f"{run_dir} has no parsed/summary.json",
            hint=f"run `pgbench-harness report --run-dir {run_dir}` first to (re)build it.",
        )
    summary = read_json(summary_path)
    summary["_settings"] = {r["name"]: r["setting"] for r in load_pg_settings(run_dir / "env")}
    summary["_dir"] = str(run_dir)
    return summary


def _disambiguate(runs: list[dict[str, Any]]) -> None:
    """Give each run a unique `display` label (labels can repeat across runs)."""
    seen: dict[str, int] = {}
    for r in runs:
        seen[r["label"]] = seen.get(r["label"], 0) + 1
    for r in runs:
        if seen[r["label"]] > 1:
            suffix = r["run_id"].rsplit("-", 1)[-1]
            r["display"] = f"{r['label']} ({suffix})"
        else:
            r["display"] = r["label"]


def _mean_by_threads(summary: dict[str, Any], metric: str) -> dict[int, float]:
    by_threads: dict[int, list[float]] = {}
    for l in summary["levels"]:
        if l["status"] == STATUS_OK and l.get(metric) is not None:
            by_threads.setdefault(l["threads"], []).append(l[metric])
    return {t: statistics.fmean(vs) for t, vs in sorted(by_threads.items())}


def _color(i: int) -> str:
    return RUN_COLORS[i % len(RUN_COLORS)]


def chart_overlay(runs: list[dict[str, Any]], metric: str, title: str, ylabel: str) -> Optional[str]:
    """Overlaid metric-vs-threads chart, one color per run, legend = run label."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    all_threads: set[int] = set()
    peak = 0.0
    plotted = False
    for i, run in enumerate(runs):
        pts = _mean_by_threads(run, metric)
        if not pts:
            continue
        ax.plot(list(pts), list(pts.values()), marker="o", linewidth=2.2,
                color=_color(i), label=run["display"])
        all_threads.update(pts)
        peak = max(peak, max(pts.values()))
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, title, "client threads (log scale)", ylabel)
    _log_x(ax, sorted(all_threads))
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0, top=peak * 1.18 if peak > 0 else None)
    return fig_to_base64(fig)


def chart_efficiency(runs: list[dict[str, Any]]) -> Optional[str]:
    """Latency-vs-throughput frontier: x = QPS, y = p99, one line per run.

    Lower-and-to-the-right is better (more throughput at less latency). Reveals
    which edition/config sustains lower tail latency at a given load.
    """
    fig, ax = plt.subplots(figsize=FIGSIZE)
    plotted = False
    for i, run in enumerate(runs):
        qps = _mean_by_threads(run, "qps_avg")
        p99 = _mean_by_threads(run, "lat_p99")
        pts = sorted((qps[t], p99[t]) for t in qps if t in p99)
        if not pts:
            continue
        ax.plot([x for x, _ in pts], [y for _, y in pts], marker="o",
                linewidth=2.2, color=_color(i), label=run["display"])
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, "Latency vs throughput (p99)", "throughput (QPS)", "p99 latency (ms)")
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0)
    ax.set_xlim(left=0)
    return fig_to_base64(fig)


def chart_relative(runs: list[dict[str, Any]]) -> Optional[str]:
    """QPS of each run relative to the first run (baseline = 100%), vs threads."""
    if len(runs) < 2:
        return None
    base = _mean_by_threads(runs[0], "qps_avg")
    if not base:
        return None
    fig, ax = plt.subplots(figsize=FIGSIZE)
    all_threads: set[int] = set()
    for i, run in enumerate(runs):
        cur = _mean_by_threads(run, "qps_avg")
        pts = sorted((t, cur[t] / base[t] * 100) for t in cur if t in base and base[t] > 0)
        if not pts:
            continue
        ax.plot([t for t, _ in pts], [v for _, v in pts], marker="o",
                linewidth=2.2, color=_color(i), label=run["display"])
        all_threads.update(t for t, _ in pts)
    ax.axhline(100, color="#8b95a3", linewidth=1.2, linestyle="--")
    _style_ax(ax, f"QPS relative to baseline ({runs[0]['display']})",
              "client threads (log scale)", "% of baseline QPS")
    _log_x(ax, sorted(all_threads))
    ax.legend(fontsize=11)
    return fig_to_base64(fig)


def _mean_io_by_threads(summary: dict[str, Any], key: str) -> dict[int, float]:
    by_threads: dict[int, list[float]] = {}
    for l in summary["levels"]:
        io = l.get("io") if l["status"] == STATUS_OK else None
        if io and io.get(key) is not None:
            by_threads.setdefault(l["threads"], []).append(io[key])
    return {t: statistics.fmean(vs) for t, vs in sorted(by_threads.items())}


def chart_io_overlay(runs: list[dict[str, Any]], key: str, title: str, ylabel: str) -> Optional[str]:
    """Overlay one engine-side I/O metric (e.g. write_ops_s) across runs."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    all_threads: set[int] = set()
    peak = 0.0
    plotted = False
    for i, run in enumerate(runs):
        pts = _mean_io_by_threads(run, key)
        if not pts:
            continue
        ax.plot(list(pts), list(pts.values()), marker="o", linewidth=2.2,
                color=_color(i), label=run["display"])
        all_threads.update(pts)
        peak = max(peak, max(pts.values()))
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, title, "client threads (log scale)", ylabel)
    _log_x(ax, sorted(all_threads))
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0, top=peak * 1.18 if peak > 0 else None)
    return fig_to_base64(fig)


def build_run_kpis(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-run headline KPIs: peak QPS, the thread level at peak, p99 there."""
    kpis = []
    for run in runs:
        qps = _mean_by_threads(run, "qps_avg")
        p99 = _mean_by_threads(run, "lat_p99")
        if qps:
            peak_t = max(qps, key=lambda t: qps[t])
            kpis.append({"display": run["display"], "peak_qps": qps[peak_t],
                         "peak_threads": peak_t, "p99_at_peak": p99.get(peak_t),
                         "status": run["status"]})
        else:
            kpis.append({"display": run["display"], "peak_qps": None,
                         "peak_threads": None, "p99_at_peak": None,
                         "status": run["status"]})
    return kpis


def build_winner(kpis: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Identify the highest-peak-QPS run and its margin over the runner-up."""
    scored = sorted((k for k in kpis if k["peak_qps"] is not None),
                    key=lambda k: k["peak_qps"], reverse=True)
    if not scored:
        return None
    best = scored[0]
    margin = None
    if len(scored) > 1 and scored[1]["peak_qps"] > 0:
        margin = (best["peak_qps"] - scored[1]["peak_qps"]) / scored[1]["peak_qps"] * 100
    return {"display": best["display"], "peak_qps": best["peak_qps"], "margin_pct": margin}


def build_table(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Side-by-side headline table over the union of thread ladders (gaps allowed).

    For exactly two runs, a Δ QPS % column (second vs first) is included.
    """
    threads = sorted({t for r in runs for t in _mean_by_threads(r, "qps_avg")})
    qps_maps = [_mean_by_threads(r, "qps_avg") for r in runs]
    p99_maps = [_mean_by_threads(r, "lat_p99") for r in runs]
    rows = []
    for t in threads:
        cells = [{"qps": qps_maps[i].get(t), "p99": p99_maps[i].get(t)}
                 for i in range(len(runs))]
        delta = None
        if len(runs) == 2 and cells[0]["qps"] and cells[1]["qps"] is not None:
            delta = (cells[1]["qps"] - cells[0]["qps"]) / cells[0]["qps"] * 100
        rows.append({"threads": t, "cells": cells, "delta_pct": delta})
    return {"threads": threads, "rows": rows, "with_delta": len(runs) == 2}


def settings_diff(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """pg_settings rows that differ between runs, key settings first.

    Each entry: name, per-run values, and whether it's a "key" setting (so the
    template can emphasise the settings our benchmarking rounds focus on).
    """
    all_names = {n for r in runs for n in r["_settings"]}
    key_first = [n for n in KEY_SETTINGS if n in all_names]
    rest = sorted(all_names - set(KEY_SETTINGS))
    diff = []
    for name in key_first + rest:
        values = [r["_settings"].get(name, "—") for r in runs]
        if len(set(values)) > 1:
            diff.append({"name": name, "vals": values, "is_key": name in KEY_SETTINGS})
    return diff


def _run_mode(run_dir: Path) -> str:
    """sweep | soak, decided by the parsed artifact present (then the manifest)."""
    if (run_dir / "parsed" / "soak_summary.json").exists():
        return "soak"
    if (run_dir / "parsed" / "summary.json").exists():
        return "sweep"
    man = run_dir / "manifest.json"
    if man.exists():
        try:
            return str(read_json(man).get("mode", "sweep"))
        except (ValueError, OSError):
            pass
    return "sweep"


def compare_runs(run_dirs: list[Path], out_path: Path) -> Path:
    """Top-level compare: enforce same run type, then dispatch sweep vs soak.

    Comparing a sweep against a soak is meaningless (one is throughput-vs-threads,
    the other fixed-concurrency-vs-time), so mixed selections are refused with a
    clear message instead of a broken or misleading report.
    """
    if len(run_dirs) < 2:
        raise ReportError("compare needs at least two runs",
                          hint="select two or more runs.")
    modes = {_run_mode(d) for d in run_dirs}
    if len(modes) > 1:
        raise ReportError(
            "cannot compare runs of different types (" + ", ".join(sorted(modes)) + ")",
            hint="select runs of the SAME type — all sweeps, or all soaks.")
    if "soak" in modes:
        return generate_soak_compare(run_dirs, out_path)
    return generate_compare(run_dirs, out_path)


def generate_compare(run_dirs: list[Path], out_path: Path) -> Path:
    """Render the comparison report for N sweep run directories."""
    if len(run_dirs) < 2:
        raise ReportError("compare needs at least two run directories",
                          hint="pass two or more --runs.")
    runs = [load_run(d) for d in run_dirs]
    _disambiguate(runs)
    kpis = build_run_kpis(runs)
    html = _jinja_env().get_template("compare.html.j2").render(
        runs=runs,
        kpis=kpis,
        winner=build_winner(kpis),
        charts={
            "qps": chart_overlay(runs, "qps_avg", "QPS vs client threads", "QPS"),
            "tps": chart_overlay(runs, "tps_avg", "TPS vs client threads", "TPS"),
            "p99": chart_overlay(runs, "lat_p99", "p99 latency vs client threads", "p99 latency (ms)"),
            "efficiency": chart_efficiency(runs),
            "relative": chart_relative(runs),
            "io_write": chart_io_overlay(runs, "write_ops_s", "Write ops/s vs client threads", "write ops / second"),
            "io_read": chart_io_overlay(runs, "read_ops_s", "Read ops/s vs client threads", "read ops / second"),
        },
        table=build_table(runs),
        diff=settings_diff(runs),
        any_settings=any(r["_settings"] for r in runs),
        generated_utc=utc_now_iso(),
        harness=harness_version(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, html)
    return out_path


# ── soak-vs-soak comparison ──────────────────────────────────────────────────

def load_soak_run(run_dir: Path) -> dict[str, Any]:
    """Load one soak run's soak_summary.json + pg_settings for comparison."""
    p = run_dir / "parsed" / "soak_summary.json"
    if not p.exists():
        raise ReportError(
            f"{run_dir} has no parsed/soak_summary.json",
            hint=f"run `pgbench-harness report --run-dir {run_dir}` first to (re)build it.")
    s = read_json(p)
    s["_settings"] = {r["name"]: r["setting"] for r in load_pg_settings(run_dir / "env")}
    s["_dir"] = str(run_dir)
    return s


def _soak_tps_series(run_dir: str) -> tuple[list[int], list[float]]:
    path = Path(run_dir) / "parsed" / "soak_timeseries.csv"
    if not path.exists():
        return [], []
    ts: list[int] = []
    tps: list[float] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts.append(int(row["t"]))
                tps.append(float(row["tps"]))
            except (KeyError, ValueError):
                continue
    return ts, tps


def chart_soak_throughput(runs: list[dict[str, Any]]) -> Optional[str]:
    """Overlaid per-second TPS-over-time, one line per soak (decimated)."""
    fig, ax = plt.subplots(figsize=(FIGSIZE[0], 4.6))
    plotted = False
    for i, run in enumerate(runs):
        ts, tps = _soak_tps_series(run["_dir"])
        if not ts:
            continue
        stride = max(1, len(ts) // 1500)
        ax.plot(ts[::stride], tps[::stride], color=_color(i), linewidth=1.6,
                label=run["display"])
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, "Throughput over time (overlaid)", "elapsed time (s)", "TPS")
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0)
    return fig_to_base64(fig)


def build_soak_kpis(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-soak headline metrics: throughput, tail latency, coverage, downtime."""
    out = []
    for r in runs:
        rp = r.get("run_profile") or {}
        tps = rp.get("tps") or {}
        lat = rp.get("latency_ms") or {}
        out.append({
            "display": r["display"], "status": r.get("status"),
            "threads": (r.get("soak") or {}).get("threads"),
            "median_tps": tps.get("median"), "peak_tps": tps.get("max"),
            "cov_pct": tps.get("cov_pct"),
            "p50": lat.get("p50"), "p95": lat.get("p95"), "p99": lat.get("p99"),
            "coverage_pct": r.get("coverage_pct"),
            "longest_outage_s": rp.get("longest_outage_s"),
            "events": len(r.get("events") or []), "detected": len(r.get("detected") or []),
        })
    return out


def build_soak_winner(kpis: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    scored = sorted((k for k in kpis if k["median_tps"] is not None),
                    key=lambda k: k["median_tps"], reverse=True)
    if not scored:
        return None
    best = scored[0]
    margin = None
    if len(scored) > 1 and scored[1]["median_tps"] > 0:
        margin = (best["median_tps"] - scored[1]["median_tps"]) / scored[1]["median_tps"] * 100
    return {"display": best["display"], "median_tps": best["median_tps"], "margin_pct": margin}


def generate_soak_compare(run_dirs: list[Path], out_path: Path) -> Path:
    """Render the comparison report for N soak run directories."""
    if len(run_dirs) < 2:
        raise ReportError("compare needs at least two run directories",
                          hint="pass two or more --runs.")
    runs = [load_soak_run(d) for d in run_dirs]
    _disambiguate(runs)
    kpis = build_soak_kpis(runs)
    html = _jinja_env().get_template("compare_soak.html.j2").render(
        runs=runs,
        kpis=kpis,
        winner=build_soak_winner(kpis),
        chart=chart_soak_throughput(runs),
        diff=settings_diff(runs),
        any_settings=any(r["_settings"] for r in runs),
        generated_utc=utc_now_iso(),
        harness=harness_version(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, html)
    return out_path
