"""Op run directory plumbing: ``results/ops/<op_run_id>/`` lifecycle.

Same invariant as benchmark runs: raw capture streams under ``raw/`` are the
source of truth; ``meta.json`` is the manifest (with UTC ISO *and* epoch-ms
anchors — the alignment key for correlating with benchmark runs); everything
else (TIMELINE.txt, events.csv, report.html) is derived and regenerable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.util import (atomic_write_json, setup_logging, utc_now,
                                  utc_now_iso)

TERMINAL = ("complete", "warning", "failed", "canceled", "aborted")

# Exit codes the worker maps onto job states: 0/1 -> done, else failed.
EXIT_OK = 0
EXIT_WARNING = 1        # op completed but surfaced a condition (e.g. pending_restart)
EXIT_FAILED = 3
EXIT_ABORTED = 4        # preflight refused to fire (e.g. backup lock held)


def utc_ms() -> int:
    return int(time.time() * 1000)


def make_op_run_id(op: str, label: str) -> str:
    # A short random suffix prevents two runs of the same op+label within one
    # second from colliding on the same directory (id is second-granularity).
    slug = re.sub(r"[^a-z0-9-]+", "-", (label or op).lower()).strip("-") or op
    suffix = os.urandom(2).hex()
    return f"{op}-{slug}-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{suffix}"


class OpsRun:
    """One op run: directory, meta, event feed, and live status snapshot."""

    def __init__(self, results_dir: Path, op: str, label: str,
                 target: dict[str, Any], params: dict[str, Any]) -> None:
        self.op_run_id = make_op_run_id(op, label)
        self.run_dir = results_dir / "ops" / self.op_run_id
        (self.run_dir / "raw").mkdir(parents=True, exist_ok=True)
        self._status_lock = threading.Lock()
        self.meta: dict[str, Any] = {
            "op_run_id": self.op_run_id, "op": op, "label": label,
            "target": target, "params": params,
            "status": "running",
            "created_utc": utc_now_iso(), "created_epoch_ms": utc_ms(),
            "finished_utc": "", "finished_epoch_ms": None,
            "headline": {},
        }
        self.save_meta()
        self.logger = setup_logging(self.run_dir / "ops.log")
        # The worker links job -> op run by parsing this exact line from stdout
        # (mirrors the benchmark runner's "run -> <dir>" contract).
        self.logger.info("ops run -> %s", self.run_dir)

    # ── meta / status ──

    def save_meta(self) -> None:
        atomic_write_json(self.run_dir / "meta.json", self.meta)

    def finalize(self, status: str, headline: Optional[dict[str, Any]] = None,
                 error: str = "") -> None:
        self.meta["status"] = status
        self.meta["finished_utc"] = utc_now_iso()
        self.meta["finished_epoch_ms"] = utc_ms()
        if headline:
            self.meta["headline"].update(headline)
        if error:
            self.meta["error"] = error
        self.save_meta()
        self.logger.info("ops run %s finished: %s", self.op_run_id, status)

    def status_update(self, **fields: Any) -> None:
        """Live status snapshot for the SSE cockpit (atomic, overwrite-only).

        Serialized with a lock: a scenario's background ClusterWatch thread and
        the main thread both call this, and an unlocked read-modify-write would
        lose fields (e.g. the watch thread, holding a pre-'fired' snapshot,
        writing back over the phase the main thread just set)."""
        with self._status_lock:
            path = self.run_dir / "status.json"
            cur: dict[str, Any] = {}
            if path.exists():
                try:
                    cur = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    cur = {}
            cur.update(fields)
            cur["ts_utc"] = utc_now_iso()
            cur["ts_epoch_ms"] = utc_ms()
            atomic_write_json(path, cur)

    # ── event feed ──

    def event(self, etype: str, label: str, note: str = "",
              extra: Optional[dict[str, Any]] = None) -> None:
        """Append to events.jsonl (the live feed + stitcher input) and the log."""
        rec: dict[str, Any] = {"ts_utc": utc_now_iso(), "ts_epoch_ms": utc_ms(),
                               "type": etype, "label": label, "note": note}
        if extra:
            rec.update(extra)
        with open(self.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        self.logger.info("[%s] %s%s", etype, label, f" — {note}" if note else "")

    def raw_path(self, name: str) -> Path:
        return self.run_dir / "raw" / name

    def get_logger(self) -> logging.Logger:
        return self.logger


def read_meta(run_dir: Path) -> Optional[dict[str, Any]]:
    try:
        m = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None
