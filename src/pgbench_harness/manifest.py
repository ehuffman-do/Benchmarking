"""Run manifest: crash-resumable per-level bookkeeping.

The manifest is rewritten atomically after every level, so a killed run can
be resumed with ``run --resume`` — levels whose status is ``ok`` or
``failed`` are treated as completed and skipped.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.errors import ReportError
from pgbench_harness.util import atomic_write_json, read_json, utc_now_iso

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
COMPLETED_STATUSES = (STATUS_OK, STATUS_FAILED)


@dataclass
class Level:
    """One (repetition, thread-count) cell of the sweep."""

    rep: int
    threads: int
    status: str = STATUS_PENDING
    started_utc: str = ""
    finished_utc: str = ""
    exit_code: Optional[int] = None
    error_excerpt: str = ""
    raw_log: str = ""
    attempts: int = 1
    prior_attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"rep{self.rep}_t{self.threads:03d}"


@dataclass
class Manifest:
    """Top-level run state, persisted as results/<run_id>/manifest.json."""

    run_id: str
    label: str
    edition: str
    tshirt_size: str
    status: str = "created"  # created|running|complete|partial|failed
    created_utc: str = field(default_factory=utc_now_iso)
    finished_utc: str = ""
    wall_time_s: float = 0.0
    preflight: dict[str, Any] = field(default_factory=dict)
    levels: list[Level] = field(default_factory=list)

    def level(self, rep: int, threads: int) -> Level:
        """Find (or raise) the level entry for (rep, threads)."""
        for lvl in self.levels:
            if lvl.rep == rep and lvl.threads == threads:
                return lvl
        raise KeyError(f"no level rep={rep} threads={threads} in manifest")

    def pending_levels(self) -> list[Level]:
        """Levels not yet completed (used by --resume; 'running' means crashed mid-level)."""
        return [l for l in self.levels if l.status not in COMPLETED_STATUSES]

    def finalize_status(self) -> str:
        """Derive the final run status from level outcomes."""
        outcomes = [l.status for l in self.levels]
        if all(s == STATUS_OK for s in outcomes):
            self.status = "complete"
        elif any(s == STATUS_OK for s in outcomes):
            self.status = "partial"
        else:
            self.status = "failed"
        self.finished_utc = utc_now_iso()
        return self.status

    def save(self, run_dir: Path) -> None:
        """Atomically persist the manifest."""
        doc = asdict(self)
        atomic_write_json(run_dir / "manifest.json", doc)

    @classmethod
    def load(cls, run_dir: Path) -> "Manifest":
        """Load a manifest from a run directory."""
        path = run_dir / "manifest.json"
        if not path.exists():
            raise ReportError(
                f"no manifest.json in {run_dir}",
                hint="is this a pgbench-harness run directory?",
            )
        doc = read_json(path)
        levels = [Level(**lvl) for lvl in doc.pop("levels", [])]
        return cls(levels=levels, **doc)


def plan_levels(threads: tuple[int, ...], repetitions: int) -> list[Level]:
    """Build the ordered execution plan: full thread ladder, repeated N times."""
    return [Level(rep=rep, threads=t) for rep in range(1, repetitions + 1) for t in threads]
