"""Evidence-grade report for suite and device-probe runs.

Structure mirrors the storage team's benchmark report: executive summary and
verdict, config + storage-identity tables, per-workload plain-language SQL
descriptions, peak summary, full per-concurrency tables (TPS + latency),
scaling charts, the device-IOPS timeline with reference-limit lines and event
marks, and an auto-populated caveats section.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pgbench_harness import evidence as evidence_mod
from pgbench_harness.errors import ReportError
from pgbench_harness.manifest import Manifest
from pgbench_harness.report import _jinja_env, fig_to_base64, load_pg_settings
from pgbench_harness.spec import load_spec
from pgbench_harness.util import atomic_write_text, read_json


def _chart_scaling(segs: dict[str, list[dict]], metric: str, title: str,
                   ylabel: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7.5, 4))
    for seg, rows in segs.items():
        pts = [(r["threads"], r.get(metric)) for r in rows
               if r.get(metric) is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts],
                    marker="o", label=seg)
    ax.set_xscale("log", base=2)
    ax.set_xlabel("clients / threads")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    return fig_to_base64(fig)


def chart_device_timeline(rows: list[dict], limits: dict,
                          events: list[dict], t0_ms: Optional[int]) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9.5, 4.2))
    base = rows[0]["t_epoch_ms"] if rows else 0
    xs = [(r["t_epoch_ms"] - base) / 1000.0 for r in rows]
    ax.plot(xs, [r["iops"] for r in rows], lw=0.9, label="device IOPS (r+w)")
    ax.plot(xs, [r["reads_s"] for r in rows], lw=0.7, alpha=0.6, label="reads/s")
    ax.plot(xs, [r["writes_s"] for r in rows], lw=0.7, alpha=0.6, label="writes/s")
    for key, style, lbl in (("standard_iops", "--", "standard limit"),
                            ("burst_iops", ":", "burst limit"),
                            ("target_iops", "-.", "target (high-perf)")):
        if limits.get(key):
            ax.axhline(limits[key], ls=style, lw=1.1, color="crimson"
                       if key == "standard_iops" else "gray",
                       label=f"{lbl} {limits[key]:,}")
    for ev in events or []:
        if ev.get("_off_s") is not None:
            ax.axvline(ev["_off_s"], color="black", alpha=0.25, lw=0.8)
            ax.annotate(ev.get("label", "")[:28], (ev["_off_s"], ax.get_ylim()[1]),
                        rotation=90, fontsize=6, va="top", alpha=0.7)
    ax.set_xlabel("seconds since first device sample")
    ax.set_ylabel("IO operations / s")
    ax.set_title("Block device backing /pgdata — measured IOPS vs reference limits")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, loc="upper right")
    return fig_to_base64(fig)


def _offset_events(run_dir: Path, rows: list[dict]) -> list[dict]:
    from datetime import datetime, timezone
    if not rows:
        return []
    base_ms = rows[0]["t_epoch_ms"]
    out = []
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
            ts = ev.get("ts_utc", "")
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in ts else "%Y-%m-%dT%H:%M:%SZ"
            ms = datetime.strptime(ts, fmt).replace(
                tzinfo=timezone.utc).timestamp() * 1000
            ev["_off_s"] = round((ms - base_ms) / 1000.0, 1)
            if ev.get("type") != "loadgen_restart":
                out.append(ev)
        except (ValueError, KeyError):
            continue
    return out


def generate_evidence_report(run_dir: Path) -> Path:
    run_dir = run_dir.resolve()
    spec = load_spec(run_dir / "spec.yaml")
    manifest = Manifest.load(run_dir)
    ev_path = run_dir / "evidence.json"
    if not ev_path.exists():
        from pgbench_harness import deviceio
        rows = deviceio.derive_device_series(run_dir)
        verdict = deviceio.compute_verdict(rows, spec.limits) if spec.cluster else None
        evidence_mod.build_evidence(run_dir, spec, verdict)
    try:
        ev = read_json(ev_path)
    except (OSError, ValueError):
        raise ReportError(f"no readable evidence.json in {run_dir}")

    summary = ev.get("summary") or {}
    levels = summary.get("levels") or []
    segs: dict[str, list[dict]] = {}
    for lv in levels:
        segs.setdefault(lv.get("seg") or "workload", []).append(lv)
    peak = []
    for seg, lrows in segs.items():
        ok = [r for r in lrows if r.get("tps_avg")]
        if ok:
            best = max(ok, key=lambda r: r["tps_avg"])
            peak.append({"seg": seg, "threads": best["threads"],
                         "tps": best["tps_avg"], "qps": best.get("qps_avg"),
                         "lat_p95": best.get("lat_p95"),
                         "lat_avg": best.get("lat_avg")})

    from pgbench_harness.deviceio import derive_device_series
    dev_rows = derive_device_series(run_dir)
    events = _offset_events(run_dir, dev_rows)
    charts: dict[str, Any] = {}
    if segs and any(r.get("tps_avg") for rows_ in segs.values() for r in rows_):
        charts["tps"] = _chart_scaling(segs, "tps_avg",
                                       "Throughput scaling by workload", "TPS")
        charts["lat"] = _chart_scaling(segs, "lat_p95",
                                       "P95 latency by concurrency", "ms (p95)")
    if dev_rows:
        charts["device"] = chart_device_timeline(
            dev_rows, ev.get("limits") or {}, events, None)

    env_dir = run_dir / "env"

    def _env(name: str) -> str:
        try:
            return (env_dir / name).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    from pgbench_harness.capture import KEY_SETTINGS
    settings = load_pg_settings(env_dir)
    settings_map = {s["name"]: s for s in settings}
    html = _jinja_env().get_template("evidence_report.html.j2").render(
        key_settings=[settings_map.get(k, {"name": k, "setting": "n/a",
                                           "unit": "", "source": ""})
                      for k in KEY_SETTINGS],
        all_settings=settings,
        ev=ev, manifest=manifest, spec=spec, summary=summary, segs=segs,
        peak=peak, charts=charts, fileio=ev.get("fileio"),
        storage=ev.get("storage_identity") or {},
        verdict=ev.get("verdict"),
        events=events,
        device_samples=len(dev_rows),
        env={"server_version": _env("server_version.txt"),
             "sysbench_version": _env("sysbench_version.txt"),
             "harness_version": _env("harness_git_sha.txt"),
             "host_info": _env("host_info.txt")},
    )
    out = run_dir / "report.html"
    atomic_write_text(out, html)
    return out
