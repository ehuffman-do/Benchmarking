"""Cross-run comparison report: overlaid charts, side-by-side table, settings diff."""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from pgbench_harness.capture import harness_version  # noqa: E402
from pgbench_harness.errors import ReportError  # noqa: E402
from pgbench_harness.manifest import STATUS_OK  # noqa: E402
from pgbench_harness.report import (  # noqa: E402
    FIGSIZE, _jinja_env, _log_x, _style_ax, fig_to_base64, load_pg_settings,
)
from pgbench_harness.util import read_json, utc_now_iso  # noqa: E402

RUN_COLORS = ["#1f4e79", "#c0392b", "#1e8449", "#7d3c98", "#b9770e", "#117a8b"]


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


def _mean_by_threads(summary: dict[str, Any], metric: str) -> dict[int, float]:
    by_threads: dict[int, list[float]] = {}
    for l in summary["levels"]:
        if l["status"] == STATUS_OK and l.get(metric) is not None:
            by_threads.setdefault(l["threads"], []).append(l[metric])
    return {t: statistics.fmean(vs) for t, vs in sorted(by_threads.items())}


def chart_overlay(runs: list[dict[str, Any]], metric: str, title: str, ylabel: str) -> Optional[str]:
    """Overlaid metric-vs-threads chart, one color per run, legend = run label."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    all_threads: set[int] = set()
    plotted = False
    for i, run in enumerate(runs):
        pts = _mean_by_threads(run, metric)
        if not pts:
            continue
        ax.plot(list(pts), list(pts.values()), marker="o", linewidth=2.2,
                color=RUN_COLORS[i % len(RUN_COLORS)], label=run["label"])
        all_threads.update(pts)
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, title, "client threads (log scale)", ylabel)
    _log_x(ax, sorted(all_threads))
    ax.legend(fontsize=12)
    ax.set_ylim(bottom=0)
    return fig_to_base64(fig)


def build_table(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Side-by-side headline table over the union of thread ladders (gaps allowed)."""
    threads = sorted({t for r in runs for t in _mean_by_threads(r, "qps_avg")})
    rows = []
    for t in threads:
        cells = []
        for r in runs:
            qps = _mean_by_threads(r, "qps_avg").get(t)
            p99 = _mean_by_threads(r, "lat_p99").get(t)
            cells.append({"qps": qps, "p99": p99})
        rows.append({"threads": t, "cells": cells})
    return {"threads": threads, "rows": rows}


def settings_diff(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """pg_settings rows whose values differ between at least two runs."""
    names = sorted({n for r in runs for n in r["_settings"]})
    diff = []
    for name in names:
        values = [r["_settings"].get(name, "—") for r in runs]
        if len(set(values)) > 1:
            diff.append({"name": name, "vals": values})
    return diff


def generate_compare(run_dirs: list[Path], out_path: Path) -> Path:
    """Render the comparison report for N run directories."""
    if len(run_dirs) < 2:
        raise ReportError("compare needs at least two run directories")
    runs = [load_run(d) for d in run_dirs]
    html = _jinja_env().get_template("compare.html.j2").render(
        runs=runs,
        charts={
            "qps": chart_overlay(runs, "qps_avg", "QPS vs client threads", "QPS"),
            "p99": chart_overlay(runs, "lat_p99", "p99 latency vs client threads", "p99 latency (ms)"),
        },
        table=build_table(runs),
        diff=settings_diff(runs),
        any_settings=any(r["_settings"] for r in runs),
        generated_utc=utc_now_iso(),
        harness=harness_version(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
