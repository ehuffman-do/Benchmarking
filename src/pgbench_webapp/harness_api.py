"""Thin adapter onto the harness library — the web tier calls these instead of
duplicating any parsing/validation/report logic. Pure functions, no I/O state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from pgbench_harness import report as _report
from pgbench_harness import report_soak as _report_soak
from pgbench_harness import runner, sysbench
from pgbench_harness.compare import compare_runs
from pgbench_harness.errors import HarnessError, SpecError
from pgbench_harness.spec import Spec, parse_spec


def validate_yaml(spec_yaml: str) -> dict[str, Any]:
    """Validate a spec string via the harness validator (the single source of rules)."""
    try:
        doc = yaml.safe_load(spec_yaml)
    except yaml.YAMLError as exc:
        return {"ok": False, "error": f"invalid YAML: {exc}"}
    try:
        spec = parse_spec(doc)
    except SpecError as exc:
        return {"ok": False, "error": str(exc), "hint": getattr(exc, "hint", "")}
    return {"ok": True, "mode": "soak" if spec.is_soak else "sweep",
            "label": spec.run.label, "workload": spec.workload.type}


def _spec_from_yaml(spec_yaml: str) -> Spec:
    return parse_spec(yaml.safe_load(spec_yaml))


def dry_run(spec_yaml: str) -> dict[str, Any]:
    """Exact planned sysbench commands + wall-clock budget (mirrors `--dry-run`)."""
    spec = _spec_from_yaml(spec_yaml)
    if spec.is_soak:
        assert spec.soak is not None
        cmd = sysbench.build_soak_command(spec, spec.soak.threads, spec.soak.duration_s)
        return {"mode": "soak", "budget_s": spec.soak.duration_s,
                "commands": [cmd.display()]}
    assert spec.sweep is not None
    cmds = []
    for rep in range(1, spec.sweep.repetitions + 1):
        for threads in spec.sweep.threads:
            cmds.append(f"[rep {rep}, {threads} threads] "
                        + sysbench.build_run_command(spec, threads).display())
    return {"mode": "sweep", "budget_s": int(runner.planned_budget_s(spec)), "commands": cmds}


def generate_report(run_dir: Path) -> Path:
    """(Re)generate the report for a run dir (mode-aware), returning the file path."""
    if (run_dir / "parsed" / "soak_summary.json").exists() or \
            (run_dir / "manifest.json").exists() and _is_soak(run_dir):
        return _report_soak.generate_soak_report(run_dir)
    return _report.generate_report(run_dir)


def _is_soak(run_dir: Path) -> bool:
    import json
    try:
        return json.loads((run_dir / "manifest.json").read_text()).get("mode") == "soak"
    except (OSError, ValueError):
        return False


def report_filename(run_dir: Path) -> str:
    return "soak_report.html" if _is_soak(run_dir) else "report.html"


def compare(run_dirs: list[Path], out_path: Path) -> Path:
    """Dispatch to the sweep or soak comparison; refuses mixed run types."""
    return compare_runs(run_dirs, out_path)


def mark_event(run_dir: Path, etype: str, label: str, note: str,
               at_s: Optional[float] = None) -> None:
    """Stamp a timeline event. ``at_s`` (offset seconds from the soak start) lets an
    operator annotate a specific point on the chart; without it the event is stamped
    at 'now' (the live-cockpit behaviour)."""
    runner.cmd_mark(run_dir, etype, label, note, at_s=at_s)


def interactive_timeseries(run_dir: Path) -> Optional[dict[str, Any]]:
    """uPlot-ready decimated soak series + markers for the in-app report (live or
    finished). None when the run has no soak timeline to plot."""
    return _report_soak.interactive_payload(run_dir)


def prepare_stats(spec_yaml: str, results_dir: Path) -> Optional[dict[str, Any]]:
    """Load-metrics a prepare job recorded (wall time, DB size, MB/s), or None.

    prepare writes one ``prepare_<host>-<db>.json`` per (host, database) under
    results/; resolve it from the job's spec. Best-effort.
    """
    import json
    try:
        spec = _spec_from_yaml(spec_yaml)
        p = runner.prepare_stats_path(spec, results_dir)
        if p.exists():
            return dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return None
    return None


__all__ = ["validate_yaml", "dry_run", "generate_report", "report_filename", "compare",
           "mark_event", "HarnessError"]
