"""Run specification: YAML loading, strict validation, typed access.

The schema is exactly the one documented in the README. Unknown keys and
missing required keys fail fast with a message naming the offending key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    retries: int = 0
    retry_wait_s: int = 30


@dataclass(frozen=True)
class Capture:
    pg_settings: bool = True
    pg_stat_statements: str = "auto"  # "auto" | "true" | "false"
    bgwriter_stats: bool = True
    histogram: bool = True


@dataclass(frozen=True)
class ReportCfg:
    percentiles: tuple[int, ...] = (50, 95, 99)
    timeseries_levels: tuple[int, ...] = ()
    variance_warn_pct: float = 10.0


@dataclass(frozen=True)
class Spec:
    run: RunMeta
    target: Target
    workload: Workload
    sweep: Sweep
    capture: Capture
    report: ReportCfg
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

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


def _parse_run(sec: dict[str, Any]) -> RunMeta:
    _check_keys(sec, "run", {"label", "edition", "tshirt_size"}, {"notes"})
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
    _check_keys(
        sec, "sweep", {"threads", "duration_s"},
        {"warmup_s", "cooldown_s", "repetitions", "retries", "retry_wait_s"},
    )
    sweep = Sweep(
        threads=_int_list(sec, "sweep", "threads", ()),
        duration_s=_typed(sec, "sweep", "duration_s", int),
        warmup_s=_typed(sec, "sweep", "warmup_s", int, 0),
        cooldown_s=_typed(sec, "sweep", "cooldown_s", int, 0),
        repetitions=_typed(sec, "sweep", "repetitions", int, 1),
        retries=_typed(sec, "sweep", "retries", int, 0),
        retry_wait_s=_typed(sec, "sweep", "retry_wait_s", int, 30),
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
    if sweep.retries < 0:
        raise SpecError("'sweep.retries' must be >= 0")
    if sweep.retry_wait_s < 0:
        raise SpecError("'sweep.retry_wait_s' must be >= 0")
    return sweep


def _parse_capture(sec: dict[str, Any]) -> Capture:
    _check_keys(sec, "capture", set(), {"pg_settings", "pg_stat_statements", "bgwriter_stats", "histogram"})
    pss = sec.get("pg_stat_statements", "auto")
    if isinstance(pss, bool):
        pss = "true" if pss else "false"
    if pss not in ("auto", "true", "false"):
        raise SpecError("'capture.pg_stat_statements' must be true, false or 'auto'")
    return Capture(
        pg_settings=_typed(sec, "capture", "pg_settings", bool, True),
        pg_stat_statements=pss,
        bgwriter_stats=_typed(sec, "capture", "bgwriter_stats", bool, True),
        histogram=_typed(sec, "capture", "histogram", bool, True),
    )


def _parse_report(sec: dict[str, Any], sweep: Sweep) -> ReportCfg:
    _check_keys(sec, "report", set(), {"percentiles", "timeseries_levels", "variance_warn_pct"})
    pcts = _int_list(sec, "report", "percentiles", (50, 95, 99))
    if any(p < 1 or p > 100 for p in pcts):
        raise SpecError("'report.percentiles' entries must be between 1 and 100")
    levels = _int_list(sec, "report", "timeseries_levels", (), allow_empty=True)
    for lvl in levels:
        if lvl not in sweep.threads:
            raise SpecError(
                f"'report.timeseries_levels' contains {lvl}, which is not in sweep.threads {list(sweep.threads)}"
            )
    return ReportCfg(
        percentiles=pcts,
        timeseries_levels=levels,
        variance_warn_pct=_typed(sec, "report", "variance_warn_pct", float, 10.0),
    )


def parse_spec(doc: Any, source: str = "<spec>") -> Spec:
    """Validate a parsed YAML document and return a typed :class:`Spec`."""
    if not isinstance(doc, dict):
        raise SpecError(f"{source}: top level of the spec must be a mapping")
    known = {"run", "target", "workload", "sweep", "capture", "report"}
    unknown = set(doc) - known
    if unknown:
        raise SpecError(f"unknown top-level section(s): {', '.join(sorted(unknown))}")
    sweep = _parse_sweep(_section(doc, "sweep"))
    return Spec(
        run=_parse_run(_section(doc, "run")),
        target=_parse_target(_section(doc, "target")),
        workload=_parse_workload(_section(doc, "workload")),
        sweep=sweep,
        capture=_parse_capture(doc.get("capture") or {}),
        report=_parse_report(doc.get("report") or {}, sweep),
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
