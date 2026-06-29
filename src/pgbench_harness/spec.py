"""Run specification: YAML loading, strict validation, typed access.

The schema is exactly the one documented in the README. Unknown keys and
missing required keys fail fast with a message naming the offending key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from pgbench_harness.errors import SpecError

EDITIONS = ("standard", "advanced")
WORKLOAD_TYPES = ("tpcc", "oltp_read_only", "oltp_read_write", "oltp_write_only")


@dataclass(frozen=True)
class RunMeta:
    label: str
    edition: str
    tshirt_size: str
    notes: str = ""
    tags: tuple[str, ...] = ()       # free-form labels for history filtering/grouping
    environment: str = ""            # e.g. staging / prod-like
    ticket: str = ""                 # e.g. DBAAS-1234
    owner: str = ""                  # who launched / owns this run


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    database: str
    user: str
    password_env: str
    sslmode: str = "require"


@dataclass(frozen=True)
class Workload:
    type: str
    tables: int
    tpcc_path: str = ""
    scale: int = 0
    table_size: int = 0
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sweep:
    threads: tuple[int, ...]
    duration_s: int
    warmup_s: int = 0
    cooldown_s: int = 0
    repetitions: int = 1


@dataclass(frozen=True)
class Capture:
    pg_settings: bool = True
    pg_stat_statements: str = "auto"  # "auto" | "true" | "false"
    bgwriter_stats: bool = True
    io_stats: bool = True
    histogram: bool = True
    live_pg: bool = True               # background engine-side metrics during the run
    live_pg_interval_s: int = 5


@dataclass(frozen=True)
class ReportCfg:
    percentiles: tuple[int, ...] = (50, 95, 99)
    timeseries_levels: tuple[int, ...] = ()
    variance_warn_pct: float = 10.0
    # Resilience/soak analysis knobs (used only by soak mode).
    baseline_window_s: Optional[tuple[int, int]] = None
    recovery_threshold_pct: float = 95.0
    full_recovery_pct: float = 100.0
    recovery_hold_s: int = 10
    latency_spike_mult: float = 2.0


SOAK_EVENT_TYPES = ("failover", "scale_up", "scale_down", "note", "loadgen_restart")
SOAK_TRIGGERS = ("manual", "do_api", "aiven_api")


@dataclass(frozen=True)
class Event:
    """A timeline annotation (declared in the spec or appended live via `mark`)."""

    type: str
    at_s: Optional[int] = None          # offset from soak start (spec-declared events)
    trigger: str = "manual"
    note: str = ""
    label: str = ""


@dataclass(frozen=True)
class Soak:
    """Fixed-concurrency, long-duration resilience run (failover/scale capture)."""

    threads: int
    duration_s: int
    tolerate_errors: bool = True        # keep the load generator alive through outages
    report_interval_s: int = 1
    max_relaunches: int = 50            # supervisor safety cap
    # Supervisor safety bounds (see runner._soak_supervisor). A run that cannot
    # produce samples must fail fast with a clear reason, never burn the window.
    fast_fail_segments: int = 3         # abort after N consecutive zero-sample launches
    hard_ceiling_grace_s: int = 15      # supervisor wall-clock may exceed duration_s by at most this
    segment_kill_grace_s: int = 10      # SIGTERM->SIGKILL grace when a segment overruns/hangs


@dataclass(frozen=True)
class Spec:
    run: RunMeta
    target: Target
    workload: Workload
    sweep: Optional[Sweep]
    capture: Capture
    report: ReportCfg
    soak: Optional[Soak] = None
    events: tuple[Event, ...] = ()
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_soak(self) -> bool:
        return self.soak is not None

    def password(self) -> str:
        """Resolve the target password from the configured environment variable."""
        value = os.environ.get(self.target.password_env)
        if not value:
            raise SpecError(
                f"environment variable '{self.target.password_env}' "
                "(target.password_env) is not set or empty",
                hint=f"export {self.target.password_env}=<password> before running.",
            )
        return value


def _section(doc: dict[str, Any], name: str) -> dict[str, Any]:
    if name not in doc:
        raise SpecError(f"spec is missing required section '{name}'")
    sec = doc[name]
    if not isinstance(sec, dict):
        raise SpecError(f"spec section '{name}' must be a mapping")
    return sec


def _check_keys(sec: dict[str, Any], where: str, required: set[str], optional: set[str]) -> None:
    unknown = set(sec) - required - optional
    if unknown:
        raise SpecError(f"unknown key(s) in '{where}': {', '.join(sorted(unknown))}")
    missing = required - set(sec)
    if missing:
        raise SpecError(f"missing required key(s) in '{where}': {', '.join(sorted(missing))}")


def _typed(sec: dict[str, Any], where: str, key: str, typ: type, default: Any = None) -> Any:
    if key not in sec:
        return default
    val = sec[key]
    if typ is int and isinstance(val, bool):  # bool is an int subclass; reject it
        raise SpecError(f"'{where}.{key}' must be an integer, got boolean")
    if typ is float and isinstance(val, int) and not isinstance(val, bool):
        val = float(val)
    if not isinstance(val, typ):
        raise SpecError(f"'{where}.{key}' must be of type {typ.__name__}, got {type(val).__name__}")
    return val


def _int_list(
    sec: dict[str, Any], where: str, key: str, default: tuple[int, ...],
    allow_empty: bool = False,
) -> tuple[int, ...]:
    if key not in sec:
        return default
    val = sec[key]
    if not isinstance(val, list) or (not val and not allow_empty) or not all(
        isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in val
    ):
        kind = "list" if allow_empty else "non-empty list"
        raise SpecError(f"'{where}.{key}' must be a {kind} of positive integers")
    return tuple(val)


def _str_list(sec: dict[str, Any], where: str, key: str) -> tuple[str, ...]:
    if key not in sec:
        return ()
    val = sec[key]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise SpecError(f"'{where}.{key}' must be a list of strings")
    return tuple(v.strip() for v in val if v.strip())


def _parse_run(sec: dict[str, Any]) -> RunMeta:
    _check_keys(sec, "run", {"label", "edition", "tshirt_size"},
                {"notes", "tags", "environment", "ticket", "owner"})
    edition = _typed(sec, "run", "edition", str)
    if edition not in EDITIONS:
        raise SpecError(f"'run.edition' must be one of {EDITIONS}, got '{edition}'")
    label = _typed(sec, "run", "label", str)
    if not label.strip():
        raise SpecError("'run.label' must be a non-empty string")
    return RunMeta(
        label=label,
        edition=edition,
        tshirt_size=_typed(sec, "run", "tshirt_size", str),
        notes=_typed(sec, "run", "notes", str, ""),
        tags=_str_list(sec, "run", "tags"),
        environment=_typed(sec, "run", "environment", str, ""),
        ticket=_typed(sec, "run", "ticket", str, ""),
        owner=_typed(sec, "run", "owner", str, ""),
    )


def _parse_target(sec: dict[str, Any]) -> Target:
    for forbidden in ("password", "pass", "pwd"):
        if forbidden in sec:
            raise SpecError(
                f"'target.{forbidden}' is not allowed: specs must never contain a password",
                hint="use target.password_env to name an environment variable instead.",
            )
    _check_keys(sec, "target", {"host", "port", "database", "user", "password_env"}, {"sslmode"})
    return Target(
        host=_typed(sec, "target", "host", str),
        port=_typed(sec, "target", "port", int),
        database=_typed(sec, "target", "database", str),
        user=_typed(sec, "target", "user", str),
        password_env=_typed(sec, "target", "password_env", str),
        sslmode=_typed(sec, "target", "sslmode", str, "require"),
    )


def _parse_workload(sec: dict[str, Any]) -> Workload:
    _check_keys(sec, "workload", {"type", "tables"}, {"tpcc_path", "scale", "table_size", "extra_args"})
    wtype = _typed(sec, "workload", "type", str)
    if wtype not in WORKLOAD_TYPES:
        raise SpecError(f"'workload.type' must be one of {WORKLOAD_TYPES}, got '{wtype}'")
    extra = sec.get("extra_args", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise SpecError("'workload.extra_args' must be a list of strings")
    wl = Workload(
        type=wtype,
        tables=_typed(sec, "workload", "tables", int),
        tpcc_path=_typed(sec, "workload", "tpcc_path", str, ""),
        scale=_typed(sec, "workload", "scale", int, 0),
        table_size=_typed(sec, "workload", "table_size", int, 0),
        extra_args=tuple(extra),
    )
    if wl.type == "tpcc":
        if not wl.tpcc_path:
            raise SpecError("'workload.tpcc_path' is required when workload.type is 'tpcc'")
        if wl.scale <= 0:
            raise SpecError("'workload.scale' (positive integer) is required for tpcc workloads")
    elif wl.table_size <= 0:
        raise SpecError(f"'workload.table_size' (positive integer) is required for {wl.type} workloads")
    return wl


def _parse_sweep(sec: dict[str, Any]) -> Sweep:
    _check_keys(sec, "sweep", {"threads", "duration_s"}, {"warmup_s", "cooldown_s", "repetitions"})
    sweep = Sweep(
        threads=_int_list(sec, "sweep", "threads", ()),
        duration_s=_typed(sec, "sweep", "duration_s", int),
        warmup_s=_typed(sec, "sweep", "warmup_s", int, 0),
        cooldown_s=_typed(sec, "sweep", "cooldown_s", int, 0),
        repetitions=_typed(sec, "sweep", "repetitions", int, 1),
    )
    if sweep.duration_s <= 0:
        raise SpecError("'sweep.duration_s' must be a positive integer")
    if sweep.warmup_s < 0 or sweep.cooldown_s < 0:
        raise SpecError("'sweep.warmup_s' and 'sweep.cooldown_s' must be >= 0")
    if sweep.warmup_s >= sweep.duration_s:
        raise SpecError(
            f"'sweep.warmup_s' ({sweep.warmup_s}) must be smaller than "
            f"'sweep.duration_s' ({sweep.duration_s}): the steady-state window would be empty"
        )
    if sweep.repetitions < 1:
        raise SpecError("'sweep.repetitions' must be >= 1")
    return sweep


def _parse_capture(sec: dict[str, Any]) -> Capture:
    _check_keys(sec, "capture", set(),
                {"pg_settings", "pg_stat_statements", "bgwriter_stats", "io_stats", "histogram",
                 "live_pg", "live_pg_interval_s"})
    pss = sec.get("pg_stat_statements", "auto")
    if isinstance(pss, bool):
        pss = "true" if pss else "false"
    if pss not in ("auto", "true", "false"):
        raise SpecError("'capture.pg_stat_statements' must be true, false or 'auto'")
    return Capture(
        pg_settings=_typed(sec, "capture", "pg_settings", bool, True),
        pg_stat_statements=pss,
        bgwriter_stats=_typed(sec, "capture", "bgwriter_stats", bool, True),
        io_stats=_typed(sec, "capture", "io_stats", bool, True),
        histogram=_typed(sec, "capture", "histogram", bool, True),
        live_pg=_typed(sec, "capture", "live_pg", bool, True),
        live_pg_interval_s=_typed(sec, "capture", "live_pg_interval_s", int, 5),
    )


def _parse_report(sec: dict[str, Any], sweep: Optional[Sweep]) -> ReportCfg:
    _check_keys(sec, "report", set(),
                {"percentiles", "timeseries_levels", "variance_warn_pct",
                 "baseline_window_s", "recovery_threshold_pct", "full_recovery_pct",
                 "recovery_hold_s", "latency_spike_mult"})
    pcts = _int_list(sec, "report", "percentiles", (50, 95, 99))
    if any(p < 1 or p > 100 for p in pcts):
        raise SpecError("'report.percentiles' entries must be between 1 and 100")
    levels = _int_list(sec, "report", "timeseries_levels", (), allow_empty=True)
    if sweep is not None:
        for lvl in levels:
            if lvl not in sweep.threads:
                raise SpecError(
                    f"'report.timeseries_levels' contains {lvl}, which is not in "
                    f"sweep.threads {list(sweep.threads)}"
                )
    baseline = None
    if "baseline_window_s" in sec:
        bw = sec["baseline_window_s"]
        if (not isinstance(bw, list) or len(bw) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in bw)
                or bw[0] >= bw[1]):
            raise SpecError("'report.baseline_window_s' must be [start, end] seconds with start < end")
        baseline = (bw[0], bw[1])
    pct = _typed(sec, "report", "recovery_threshold_pct", float, 95.0)
    full = _typed(sec, "report", "full_recovery_pct", float, 100.0)
    hold = _typed(sec, "report", "recovery_hold_s", int, 10)
    mult = _typed(sec, "report", "latency_spike_mult", float, 2.0)
    if not 0 < pct <= 100 or not 0 < full <= 100:
        raise SpecError("'report.recovery_threshold_pct'/'full_recovery_pct' must be in (0, 100]")
    if hold < 1:
        raise SpecError("'report.recovery_hold_s' must be >= 1")
    return ReportCfg(
        percentiles=pcts,
        timeseries_levels=levels,
        variance_warn_pct=_typed(sec, "report", "variance_warn_pct", float, 10.0),
        baseline_window_s=baseline,
        recovery_threshold_pct=pct,
        full_recovery_pct=full,
        recovery_hold_s=hold,
        latency_spike_mult=mult,
    )


def _parse_soak(sec: dict[str, Any]) -> Soak:
    _check_keys(sec, "soak", {"threads", "duration_s"},
                {"tolerate_errors", "report_interval_s", "max_relaunches",
                 "fast_fail_segments", "hard_ceiling_grace_s", "segment_kill_grace_s"})
    threads = _typed(sec, "soak", "threads", int)
    duration = _typed(sec, "soak", "duration_s", int)
    if threads < 1:
        raise SpecError("'soak.threads' must be >= 1")
    if duration < 1:
        raise SpecError("'soak.duration_s' must be >= 1")
    interval = _typed(sec, "soak", "report_interval_s", int, 1)
    if interval < 1:
        raise SpecError("'soak.report_interval_s' must be >= 1")
    fast_fail = _typed(sec, "soak", "fast_fail_segments", int, 3)
    if fast_fail < 1:
        raise SpecError("'soak.fast_fail_segments' must be >= 1")
    ceiling_grace = _typed(sec, "soak", "hard_ceiling_grace_s", int, 15)
    kill_grace = _typed(sec, "soak", "segment_kill_grace_s", int, 10)
    if ceiling_grace < 0 or kill_grace < 0:
        raise SpecError("'soak.hard_ceiling_grace_s' / 'segment_kill_grace_s' must be >= 0")
    return Soak(
        threads=threads,
        duration_s=duration,
        tolerate_errors=_typed(sec, "soak", "tolerate_errors", bool, True),
        report_interval_s=interval,
        max_relaunches=_typed(sec, "soak", "max_relaunches", int, 50),
        fast_fail_segments=fast_fail,
        hard_ceiling_grace_s=ceiling_grace,
        segment_kill_grace_s=kill_grace,
    )


def _parse_events(doc: Any) -> tuple[Event, ...]:
    if "events" not in doc:
        return ()
    raw = doc["events"]
    if not isinstance(raw, list):
        raise SpecError("'events' must be a list")
    out: list[Event] = []
    for i, ev in enumerate(raw):
        if not isinstance(ev, dict):
            raise SpecError(f"events[{i}] must be a mapping")
        unknown = set(ev) - {"at_s", "type", "trigger", "note", "label"}
        if unknown:
            raise SpecError(f"unknown key(s) in events[{i}]: {', '.join(sorted(unknown))}")
        etype = ev.get("type")
        if etype not in SOAK_EVENT_TYPES:
            raise SpecError(f"events[{i}].type must be one of {SOAK_EVENT_TYPES}, got {etype!r}")
        trigger = ev.get("trigger", "manual")
        if trigger not in SOAK_TRIGGERS:
            raise SpecError(f"events[{i}].trigger must be one of {SOAK_TRIGGERS}")
        if trigger != "manual":
            raise SpecError(
                f"events[{i}].trigger={trigger!r} is not supported yet (Phase 1 is manual only)")
        at_s = ev.get("at_s")
        if at_s is not None and (not isinstance(at_s, int) or isinstance(at_s, bool) or at_s < 0):
            raise SpecError(f"events[{i}].at_s must be a non-negative integer (seconds from soak start)")
        out.append(Event(type=etype, at_s=at_s, trigger=trigger,
                         note=str(ev.get("note", "")), label=str(ev.get("label", ""))))
    return tuple(out)


def parse_spec(doc: Any, source: str = "<spec>") -> Spec:
    """Validate a parsed YAML document and return a typed :class:`Spec`."""
    if not isinstance(doc, dict):
        raise SpecError(f"{source}: top level of the spec must be a mapping")
    known = {"run", "target", "workload", "sweep", "capture", "report", "soak", "events"}
    unknown = set(doc) - known
    if unknown:
        raise SpecError(f"unknown top-level section(s): {', '.join(sorted(unknown))}")
    has_soak, has_sweep = "soak" in doc, "sweep" in doc
    if has_soak and has_sweep:
        raise SpecError("spec has both 'soak' and 'sweep'; they are mutually exclusive "
                        "(soak = fixed-concurrency resilience run, sweep = thread sweep)")
    if not has_soak and not has_sweep:
        raise SpecError("spec must contain either a 'sweep' (steady-state) or a 'soak' "
                        "(resilience) section")
    sweep = _parse_sweep(_section(doc, "sweep")) if has_sweep else None
    soak = _parse_soak(_section(doc, "soak")) if has_soak else None
    events = _parse_events(doc)
    if events and not has_soak:
        raise SpecError("'events' are only valid with a 'soak' section")
    return Spec(
        run=_parse_run(_section(doc, "run")),
        target=_parse_target(_section(doc, "target")),
        workload=_parse_workload(_section(doc, "workload")),
        sweep=sweep,
        capture=_parse_capture(doc.get("capture") or {}),
        report=_parse_report(doc.get("report") or {}, sweep),
        soak=soak,
        events=events,
        raw=doc,
    )


def load_spec(path: Path) -> Spec:
    """Load and validate a run spec YAML file."""
    if not path.exists():
        raise SpecError(f"spec file not found: {path}", hint="check the --spec path.")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError(f"spec file {path} is not valid YAML: {exc}") from exc
    return parse_spec(doc, source=str(path))


def dump_spec_copy(spec: Spec, dest: Path) -> None:
    """Write a verbatim copy of the spec (it contains only the password_env *name*)."""
    atomic = yaml.safe_dump(spec.raw, sort_keys=False, default_flow_style=False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(atomic, encoding="utf-8")
