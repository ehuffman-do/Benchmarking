"""Reconcile the filesystem ``results/`` tree (the source of truth) into the
SQLite ``runs`` index, so runs created by the CLI directly also appear in the UI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import yaml

from pgbench_harness.util import atomic_write_json
from pgbench_webapp import queries
from pgbench_webapp.util import utc_now_iso

# A run is in a terminal state once it has finished (or been stopped). These are
# the only statuses the live cockpit treats as "done"; anything else streams as
# "live", so a stuck non-terminal manifest must be converged (see below).
TERMINAL_RUN = ("complete", "partial", "failed", "canceled")


def converge_run_status(results_dir: Path, run_id: str, job_state: str) -> bool:
    """Drive a run's manifest to a terminal status when its owning job has ended.

    The filesystem manifest stays the source of truth; a terminal *job* only
    OVERRIDES a manifest that is stuck non-terminal (e.g. the harness was
    SIGKILLed before it could finalize, or a pre-fix run left 'running' on disk).
    A healthy run finalizes its own manifest, so this is a no-op for it. Returns
    True if the manifest was rewritten.
    """
    if job_state not in ("done", "failed", "canceled"):
        return False
    man = results_dir / run_id / "manifest.json"
    if not man.exists():
        return False
    try:
        m = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(m, dict) or m.get("status") in TERMINAL_RUN:
        return False
    # job done-but-manifest-running is a stuck run -> failed; an explicit cancel -> canceled.
    m["status"] = "canceled" if job_state == "canceled" else "failed"
    if not m.get("finished_utc"):
        m["finished_utc"] = utc_now_iso()
    atomic_write_json(man, m)
    return True


def _peak_qps(run_dir: Path, mode: str) -> Optional[float]:
    if mode == "soak":
        p = run_dir / "parsed" / "soak_summary.json"
        if p.exists():
            try:
                return float(json.loads(p.read_text())["baseline"]["tps"]) or None
            except (ValueError, KeyError, OSError, TypeError):
                return None                 # TypeError: "tps": null — a missing
        return None                        # KPI must not sink the whole row
    p = run_dir / "parsed" / "summary.json"
    if not p.exists():
        return None
    try:
        levels = json.loads(p.read_text())["levels"]
        vals = [l["qps_avg"] for l in levels if l.get("qps_avg") is not None]
        return max(vals) if vals else None
    except (ValueError, KeyError, OSError, TypeError):
        return None


def _run_row(run_dir: Path) -> Optional[dict[str, Any]]:
    man = run_dir / "manifest.json"
    if not man.exists():
        return None
    try:
        m = json.loads(man.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(m, dict):          # valid JSON but not a manifest object
        return None
    run_meta: dict[str, Any] = {}
    spec_path = run_dir / "spec.yaml"
    if spec_path.exists():
        try:
            loaded = (yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}).get("run", {})
            run_meta = loaded if isinstance(loaded, dict) else {}
        except (yaml.YAMLError, AttributeError):
            run_meta = {}
    mode = m.get("mode", "sweep")
    workload = ""
    target_host = ""
    try:
        doc = yaml.safe_load((run_dir / "spec.yaml").read_text()) or {}
        workload = doc.get("workload", {}).get("type", "")
        target_host = doc.get("target", {}).get("host", "")
    except (OSError, yaml.YAMLError, AttributeError):
        pass
    return {
        "run_id": m.get("run_id") or run_dir.name,
        "label": m.get("label", ""), "edition": m.get("edition", ""),
        "tshirt_size": m.get("tshirt_size", ""), "mode": mode,
        "workload_type": workload, "status": m.get("status", ""),
        "tags": ",".join(t for t in (run_meta.get("tags") or [])
                         if isinstance(t, str))
                if isinstance(run_meta.get("tags"), list)
                else str(run_meta.get("tags") or ""),
        "ticket": run_meta.get("ticket", ""), "owner": run_meta.get("owner", ""),
        "environment": run_meta.get("environment", ""),
        "peak_qps": _peak_qps(run_dir, mode),
        "created_utc": m.get("created_utc", ""), "finished_utc": m.get("finished_utc", ""),
        "source": "fs", "target_host": target_host,
    }


def reconcile(conn: sqlite3.Connection, results_dir: Path) -> int:
    """Upsert every run directory under *results_dir* into the index."""
    if not results_dir.exists():
        return 0
    n = 0
    for d in sorted(results_dir.iterdir()):
        if not d.is_dir():
            continue
        try:                      # one bad run dir must not abort indexing of all
            row = _run_row(d)
        except Exception:         # noqa: BLE001
            row = None
        if row is not None:
            # Precedence: a terminal owning job converges a run whose manifest is
            # stuck non-terminal, so no run shows 'live' after its job has ended.
            if row["status"] not in TERMINAL_RUN:
                job = queries.job_for_run(conn, row["run_id"])
                if job is not None and job["state"] in ("done", "failed", "canceled"):
                    if converge_run_status(results_dir, row["run_id"], job["state"]):
                        row = _run_row(d) or row
            queries.upsert_run(conn, row)
            n += 1
    return n
