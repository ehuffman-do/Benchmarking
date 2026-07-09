"""Self-contained HTML reports for op runs + the cross-scenario comparison.

Same leadership-grade style as the benchmark reports (shared base.css, inline
uPlot). Everything renders from the run dir's derived artifacts, so a report
is regenerable at any time (``ops report --run-dir``); for backup runs with a
``linked_run_id`` the benchmark run's per-second series is overlaid with the
backup window shaded — the "backup impact under live TPC-C" view.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, PackageLoader, select_autoescape

from pgbench_harness.ops.oprun import read_meta
from pgbench_harness.util import atomic_write_text


def _env() -> Environment:
    return Environment(loader=PackageLoader("pgbench_harness", "templates"),
                       autoescape=select_autoescape(["html"]))


def _asset(name: str) -> str:
    from importlib import resources
    return (resources.files("pgbench_harness") / "templates" / name) \
        .read_text(encoding="utf-8")


def _read_csv_cols(path: Path) -> tuple[list[str], list[list[str]]]:
    if not path.exists():
        return [], []
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    if len(rows) < 2:
        return rows[0] if rows else [], []
    return rows[0], rows[1:]


def _series_from_csv(path: Path, x_col: str, y_col: str) -> list[list[float]]:
    header, rows = _read_csv_cols(path)
    if x_col not in header or y_col not in header:
        return [[], []]
    xi, yi = header.index(x_col), header.index(y_col)
    xs, ys = [], []
    for r in rows:
        try:
            xs.append(float(r[xi]))
            ys.append(float(r[yi]))
        except (ValueError, IndexError):
            continue
    return [xs, ys]


def _iso_to_epoch(iso: str) -> Optional[float]:
    if not iso:
        return None
    try:
        s = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _probe_series(run_dir: Path) -> list[list[float]]:
    """probe.log -> [epoch_s[], up01[]] for the availability strip."""
    from pgbench_harness.ops.stitch import parse_probe_log
    ticks = parse_probe_log(run_dir / "raw" / "probe.log")
    return [[t.local_ms / 1000 for t in ticks], [1.0 if t.ok else 0.0 for t in ticks]]


def _benchmark_overlay(run_dir: Path, meta: dict[str, Any]) -> Optional[dict[str, Any]]:
    """TPS/p99 of the linked benchmark run around the backup window.

    The op run's epoch-ms anchors + the soak's start_utc put both series on
    one wall-clock axis; the report shades the backup window over the TPS
    line with archive-queue depth on a secondary axis.
    """
    linked = ((meta.get("params") or {}).get("linked_run_id")
              or (meta.get("headline") or {}).get("linked_run_id"))
    if not linked:
        return None
    bench_dir = run_dir.parent.parent / str(linked)
    ts_path = bench_dir / "parsed" / "soak_timeseries.csv"
    man_path = bench_dir / "manifest.json"
    if not ts_path.exists() or not man_path.exists():
        return None
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
        start = _iso_to_epoch((man.get("soak") or {}).get("start_utc", "")
                              or man.get("created_utc", ""))
    except (ValueError, OSError):
        return None
    if start is None:
        return None
    header, rows = _read_csv_cols(ts_path)
    t_col = "t" if "t" in header else "t_offset"   # soak vs sweep column name
    if t_col not in header or "tps" not in header:
        return None
    ti, tpsi = header.index(t_col), header.index("tps")
    p99i = header.index("lat_p99") if "lat_p99" in header else None
    b_start = (meta.get("backup_start_epoch_ms") or 0) / 1000
    b_end = (meta.get("backup_end_epoch_ms") or 0) / 1000
    pad = max(120.0, (b_end - b_start))
    xs, tps, p99 = [], [], []
    for r in rows:
        try:
            x = start + float(r[ti])
        except (ValueError, IndexError):
            continue
        if b_start and (x < b_start - pad or x > b_end + pad):
            continue
        xs.append(x)
        try:
            tps.append(float(r[tpsi]))
        except (ValueError, IndexError):
            tps.append(float("nan"))
        if p99i is not None:
            try:
                p99.append(float(r[p99i]))
            except (ValueError, IndexError):
                p99.append(float("nan"))
    if not xs:
        return None

    def _window_stats(lo: float, hi: float) -> dict[str, Any]:
        vals = [v for x, v in zip(xs, tps) if lo <= x <= hi and v == v]
        lats = [v for x, v in zip(xs, p99) if lo <= x <= hi and v == v] \
            if p99 else []
        med = (sorted(vals)[len(vals) // 2] if vals else None)
        lat = (sorted(lats)[len(lats) // 2] if lats else None)
        return {"tps_median": med, "p99_median": lat, "samples": len(vals)}

    return {
        "linked_run_id": linked,
        "series": {"x": xs, "tps": tps, "p99": p99},
        "window": {"start_s": b_start, "end_s": b_end},
        "kpi": {"before": _window_stats(xs[0], b_start),
                "during": _window_stats(b_start, b_end),
                "after": _window_stats(b_end, xs[-1])},
    }


def comparison_payload(run_dir: Path) -> dict[str, Any]:
    """One row of the cross-scenario Case A/B/C table."""
    meta = read_meta(run_dir) or {}
    h = meta.get("headline") or {}
    return {
        "op_run_id": meta.get("op_run_id", run_dir.name),
        "kind": meta.get("op", ""), "label": meta.get("label", ""),
        "target": (meta.get("target") or {}).get("name", ""),
        "created_utc": meta.get("created_utc", ""),
        "case": h.get("case") or h.get("type", ""),
        "downtime_ms": h.get("downtime_ms"),
        "detection_ms": h.get("detection_ms"),
        "flip": h.get("flip"), "classification": h.get("kind", ""),
        "tl_change": (f"{h.get('tl_before')} → {h.get('tl_after')}"
                      if h.get("tl_before") is not None else ""),
        "new_primary": (h.get("leader_after")
                        if h.get("leader_after") != h.get("leader_before") else "—"),
        "backoff_tail_ms": h.get("backoff_tail_ms"),
        "full_ha_recovery_s": h.get("full_ha_recovery_s"),
        "status": meta.get("status", ""),
    }


def generate_ops_report(run_dir: Path) -> Path:
    meta = read_meta(run_dir)
    if meta is None:
        raise ValueError(f"{run_dir} has no meta.json")
    op = meta.get("op", "")
    stitched: dict[str, Any] = {}
    sp = run_dir / "stitched.json"
    if sp.exists():
        try:
            stitched = json.loads(sp.read_text(encoding="utf-8"))
        except ValueError:
            stitched = {}
    timeline_txt = ""
    tp = run_dir / "TIMELINE.txt"
    if tp.exists():
        timeline_txt = tp.read_text(encoding="utf-8")
    events: list[dict[str, Any]] = []
    ep = run_dir / "events.jsonl"
    if ep.exists():
        for line in ep.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    diff: dict[str, Any] = {}
    dp = run_dir / "diff.json"
    if dp.exists():
        try:
            diff = json.loads(dp.read_text(encoding="utf-8"))
        except ValueError:
            diff = {}
    verify: dict[str, Any] = {}
    vp = run_dir / "verify.json"
    if vp.exists():
        try:
            verify = json.loads(vp.read_text(encoding="utf-8"))
        except ValueError:
            verify = {}

    charts: dict[str, Any] = {}
    if op == "backup":
        charts["queue"] = _series_from_csv(run_dir / "parsed" / "queue_depth.csv",
                                           "epoch_s", "ready_files")
        charts["archived"] = _series_from_csv(run_dir / "parsed" / "archiver.csv",
                                              "epoch_s", "archived_count")
        charts["overlay"] = _benchmark_overlay(run_dir, meta)
    if op == "scenario":
        charts["probe"] = _probe_series(run_dir)

    html = _env().get_template("ops_report.html.j2").render(
        meta=meta, op=op, headline=meta.get("headline") or {},
        stitched=stitched, timeline_txt=timeline_txt, events=events,
        diff=diff, verify=verify,
        charts_json=json.dumps(charts),
        base_css=_env().get_template("base.css.j2").render(),
        uplot_js=_asset("uplot.min.js"), uplot_css=_asset("uplot.min.css"),
        generated_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    out = run_dir / "report.html"
    atomic_write_text(out, html)
    return out
