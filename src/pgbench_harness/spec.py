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
WORKLOAD_TYPES = ("tpcc", "oltp_read_only", "oltp_read_write", "oltp_write_only",
                  "oltp_point_select", "io_stress")
# The storage team's evidentiary matrix (suite mode) — order is the report order.
SUITE_WORKLOADS = ("oltp_point_select", "oltp_read_only", "oltp_read_write",
                   "oltp_write_only")
PGBENCH_MODES = ("tpcb", "select_only")
RAND_TYPES = ("uniform", "special", "gaussian", "pareto", "zipfian")
IO_STRESS_MIXES = {"read": "oltp_read_only", "write": "oltp_write_only",
                   "mixed": "oltp_read_write"}
# Empirical sbtest footprint (heap + PK + k index) used to turn dataset_gb
# into a row count; the post-prepare size verification is the real check.
SBTEST_BYTES_PER_ROW = 250


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
    # io_stress knobs: dataset_gb is the primary size control (table_size is
    # derived); mix picks the stock lua; rand_type steers key distribution.
    dataset_gb: float = 0.0
    mix: str = ""
    rand_type: str = ""


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
    pg_stat_monitor: str = "auto"  # "auto" | "true" | "false"
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
    # Rate-stepped mode (knee finder): offered load climbs through rate_steps
    # (sysbench --rate, txn/s; 0 = unthrottled) holding each for
    # step_duration_s; every step start is stamped as an event.
    rate_steps: tuple[int, ...] = ()
    step_duration_s: int = 0


@dataclass(frozen=True)
class Cluster:
    """Kube coordinates that make a run cluster-aware: storage identity is
    captured and the pgdata block device is sampled during the load. The
    kubeconfig is NEVER here — it reaches the process as $KUBECONFIG only."""

    cr_name: str
    namespace: str = "percona"
    cr_kind: str = "perconapgcluster"
    context: str = ""


@dataclass(frozen=True)
class Limits:
    """Reference IOPS limits the verdict is judged against (recorded with the
    evidence, never hardcoded in analysis)."""

    standard_iops: int = 10000
    burst_iops: int = 15000
    target_iops: int = 40000
    tolerance_pct: float = 10.0


@dataclass(frozen=True)
class SuiteCfg:
    """The storage team's full evidentiary matrix in one run: four stock
    sysbench OLTP workloads plus pgbench TPC-B / SELECT-only, each across the
    thread ladder, as sequential segments in one consolidated bundle."""

    duration_s: int
    threads: tuple[int, ...] = (1, 2, 4, 8, 16, 32)
    warmup_s: int = 10
    cooldown_s: int = 30
    workloads: tuple[str, ...] = SUITE_WORKLOADS
    pgbench: bool = True
    pgbench_scale: int = 1000


@dataclass(frozen=True)
class DeviceProbe:
    """sysbench fileio against the pgdata volume from a pod pinned to the
    primary's node (Percona's methodology) — the definitive ceiling test.
    Destructive-adjacent: refuses to run without allow_device_probe."""

    allow_device_probe: bool = False
    file_num: int = 128
    file_total_size_gb: float = 100.0
    io_mode: str = "async"              # async | sync
    async_backlog: int = 128
    test_mode: str = "rndrw"            # rndrw | rndrd | rndwr
    fsync_freq: int = 0                 # 0 = fsync only at end (device truth)
    threads: int = 16
    duration_s: int = 300
    image: str = "perconalab/sysbench:latest"
    block_size_kb: int = 16             # Postgres-ish random IO block
    keep_files: bool = False            # reuse test files across probe runs


@dataclass(frozen=True)
class Pmm:
    """PMM linkage for a run: where the observation layer lives. Only the
    server address — the report deep-links into PMM scoped to the run's time
    window. No token here (tokens are env-only, ops-side)."""

    server_host: str
    service_name: str = ""            # optional: pre-scope QAN to one service


@dataclass(frozen=True)
class Spec:
    run: RunMeta
    target: Target
    workload: Workload
    sweep: Optional[Sweep]
    capture: Capture
    report: ReportCfg
    soak: Optional[Soak] = None
    pmm: Optional[Pmm] = None
    suite: Optional[SuiteCfg] = None
    cluster: Optional[Cluster] = None
    limits: Limits = field(default_factory=Limits)
    device_probe: Optional[DeviceProbe] = None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_soak(self) -> bool:
        return self.soak is not None

    @property
    def is_suite(self) -> bool:
        return self.suite is not None

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
    _check_keys(sec, "workload", {"type", "tables"},
                {"tpcc_path", "scale", "table_size", "extra_args",
                 "dataset_gb", "mix", "rand_type"})
    wtype = _typed(sec, "workload", "type", str)
    if wtype not in WORKLOAD_TYPES:
        raise SpecError(f"'workload.type' must be one of {WORKLOAD_TYPES}, got '{wtype}'")
    extra = sec.get("extra_args", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise SpecError("'workload.extra_args' must be a list of strings")
    rand_type = _typed(sec, "workload", "rand_type", str, "")
    if rand_type and rand_type not in RAND_TYPES:
        raise SpecError(f"'workload.rand_type' must be one of {RAND_TYPES}, got '{rand_type}'")
    dataset_gb = _typed(sec, "workload", "dataset_gb", float, 0.0)
    mix = _typed(sec, "workload", "mix", str, "")
    table_size = _typed(sec, "workload", "table_size", int, 0)
    tables = _typed(sec, "workload", "tables", int)
    if wtype == "io_stress":
        if dataset_gb <= 0:
            raise SpecError("'workload.dataset_gb' (positive number) is required for "
                            "io_stress — it sizes the dataset to defeat caches")
        if mix not in IO_STRESS_MIXES:
            raise SpecError(f"'workload.mix' must be one of {tuple(IO_STRESS_MIXES)} "
                            f"for io_stress, got '{mix or '<missing>'}'")
        if table_size:
            raise SpecError("'workload.table_size' is derived from dataset_gb for "
                            "io_stress — remove it")
        if tables < 1:
            raise SpecError("'workload.tables' must be >= 1")
        table_size = max(1, int(dataset_gb * (1 << 30) / (tables * SBTEST_BYTES_PER_ROW)))
        rand_type = rand_type or "uniform"
    elif dataset_gb or mix:
        raise SpecError("'workload.dataset_gb'/'workload.mix' are io_stress-only knobs")
    wl = Workload(
        type=wtype,
        tables=tables,
        tpcc_path=_typed(sec, "workload", "tpcc_path", str, ""),
        scale=_typed(sec, "workload", "scale", int, 0),
        table_size=table_size,
        extra_args=tuple(extra),
        dataset_gb=dataset_gb,
        mix=mix,
        rand_type=rand_type,
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
                {"pg_settings", "pg_stat_monitor", "bgwriter_stats", "io_stats", "histogram",
                 "live_pg", "live_pg_interval_s"})
    psm = sec.get("pg_stat_monitor", "auto")
    if isinstance(psm, bool):
        psm = "true" if psm else "false"
    if psm not in ("auto", "true", "false"):
        raise SpecError("'capture.pg_stat_monitor' must be true, false or 'auto'")
    return Capture(
        pg_settings=_typed(sec, "capture", "pg_settings", bool, True),
        pg_stat_monitor=psm,
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
    _check_keys(sec, "soak", {"threads"},
                {"duration_s", "tolerate_errors", "report_interval_s", "max_relaunches",
                 "fast_fail_segments", "hard_ceiling_grace_s", "segment_kill_grace_s",
                 "rate_steps", "step_duration_s"})
    raw_steps = sec.get("rate_steps", [])
    if not isinstance(raw_steps, list) or not all(
            isinstance(x, int) and not isinstance(x, bool) and x >= 0
            for x in raw_steps):
        raise SpecError("'soak.rate_steps' must be a list of integers >= 0 "
                        "(txn/s offered per step; 0 = unthrottled)")
    rate_steps = tuple(raw_steps)
    step_s = _typed(sec, "soak", "step_duration_s", int, 0)
    if bool(rate_steps) != bool(step_s):
        raise SpecError("'soak.rate_steps' and 'soak.step_duration_s' go together — "
                        "set both (rate-stepped mode) or neither")
    threads = _typed(sec, "soak", "threads", int)
    if rate_steps:
        derived = len(rate_steps) * step_s
        duration = _typed(sec, "soak", "duration_s", int, derived)
        if duration != derived:
            raise SpecError(f"'soak.duration_s' ({duration}) conflicts with "
                            f"rate_steps x step_duration_s ({derived}) — omit it")
    else:
        if "duration_s" not in sec:
            raise SpecError("soak: missing required key 'duration_s'")
        duration = _typed(sec, "soak", "duration_s", int)
    if threads < 1:
        raise SpecError("'soak.threads' must be >= 1")
    if duration < 1:
        raise SpecError("'soak.duration_s' must be >= 1")
    interval = _typed(sec, "soak", "report_interval_s", int, 1)
    if interval != 1:
        # the resilience analysis is strictly per-second dense: absent
        # seconds ARE the outage signal, so a coarser interval would score
        # ~(1 - 1/interval) of a flawless run as downtime
        raise SpecError("'soak.report_interval_s' must be exactly 1 — the "
                        "downtime/TTR analysis models a dense per-second "
                        "timeline (missing seconds are counted as outage)")
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
        rate_steps=rate_steps,
        step_duration_s=step_s,
    )


def _parse_suite(sec: dict[str, Any]) -> SuiteCfg:
    _check_keys(sec, "suite", {"duration_s"},
                {"threads", "warmup_s", "cooldown_s", "workloads",
                 "pgbench", "pgbench_scale"})
    workloads = tuple(_str_list(sec, "suite", "workloads")) or SUITE_WORKLOADS
    for w in workloads:
        if w not in SUITE_WORKLOADS:
            raise SpecError(f"'suite.workloads' entries must be from {SUITE_WORKLOADS}, "
                            f"got '{w}'")
    cfg = SuiteCfg(
        duration_s=_typed(sec, "suite", "duration_s", int),
        threads=_int_list(sec, "suite", "threads", (1, 2, 4, 8, 16, 32)),
        warmup_s=_typed(sec, "suite", "warmup_s", int, 10),
        cooldown_s=_typed(sec, "suite", "cooldown_s", int, 30),
        workloads=workloads,
        pgbench=_typed(sec, "suite", "pgbench", bool, True),
        pgbench_scale=_typed(sec, "suite", "pgbench_scale", int, 1000),
    )
    if cfg.duration_s <= cfg.warmup_s:
        raise SpecError("'suite.duration_s' must exceed 'suite.warmup_s'")
    if cfg.pgbench and cfg.pgbench_scale < 1:
        raise SpecError("'suite.pgbench_scale' must be >= 1")
    return cfg


def _parse_cluster(sec: dict[str, Any]) -> Cluster:
    for bad in ("kubeconfig", "kubeconfig_content", "kubeconfig_path", "token"):
        if bad in sec:
            raise SpecError(f"'cluster.{bad}' must never be in a spec — the kubeconfig "
                            "travels as the KUBECONFIG environment variable only")
    _check_keys(sec, "cluster", {"cr_name"}, {"namespace", "cr_kind", "context"})
    return Cluster(
        cr_name=_typed(sec, "cluster", "cr_name", str),
        namespace=_typed(sec, "cluster", "namespace", str, "percona"),
        cr_kind=_typed(sec, "cluster", "cr_kind", str, "perconapgcluster"),
        context=_typed(sec, "cluster", "context", str, ""),
    )


def _parse_limits(sec: dict[str, Any]) -> Limits:
    _check_keys(sec, "limits", set(),
                {"standard_iops", "burst_iops", "target_iops", "tolerance_pct"})
    lim = Limits(
        standard_iops=_typed(sec, "limits", "standard_iops", int, 10000),
        burst_iops=_typed(sec, "limits", "burst_iops", int, 15000),
        target_iops=_typed(sec, "limits", "target_iops", int, 40000),
        tolerance_pct=_typed(sec, "limits", "tolerance_pct", float, 10.0),
    )
    if not 0 < lim.standard_iops <= lim.burst_iops:
        raise SpecError("'limits': need 0 < standard_iops <= burst_iops")
    if not 0 < lim.tolerance_pct < 100:
        raise SpecError("'limits.tolerance_pct' must be in (0, 100)")
    return lim


def _parse_device_probe(sec: dict[str, Any]) -> DeviceProbe:
    _check_keys(sec, "device_probe", set(),
                {"allow_device_probe", "file_num", "file_total_size_gb", "io_mode",
                 "async_backlog", "test_mode", "fsync_freq", "threads", "duration_s",
                 "image", "block_size_kb", "keep_files"})
    dp = DeviceProbe(
        allow_device_probe=_typed(sec, "device_probe", "allow_device_probe", bool, False),
        file_num=_typed(sec, "device_probe", "file_num", int, 128),
        file_total_size_gb=_typed(sec, "device_probe", "file_total_size_gb", float, 100.0),
        io_mode=_typed(sec, "device_probe", "io_mode", str, "async"),
        async_backlog=_typed(sec, "device_probe", "async_backlog", int, 128),
        test_mode=_typed(sec, "device_probe", "test_mode", str, "rndrw"),
        fsync_freq=_typed(sec, "device_probe", "fsync_freq", int, 0),
        threads=_typed(sec, "device_probe", "threads", int, 16),
        duration_s=_typed(sec, "device_probe", "duration_s", int, 300),
        image=_typed(sec, "device_probe", "image", str, "perconalab/sysbench:latest"),
        block_size_kb=_typed(sec, "device_probe", "block_size_kb", int, 16),
        keep_files=_typed(sec, "device_probe", "keep_files", bool, False),
    )
    if dp.io_mode not in ("async", "sync"):
        raise SpecError("'device_probe.io_mode' must be async|sync")
    if dp.test_mode not in ("rndrw", "rndrd", "rndwr"):
        raise SpecError("'device_probe.test_mode' must be rndrw|rndrd|rndwr")
    if dp.file_num < 1 or dp.file_total_size_gb <= 0 or dp.threads < 1             or dp.duration_s < 1 or dp.async_backlog < 1 or dp.block_size_kb < 1:
        raise SpecError("'device_probe' numeric knobs must be positive")
    return dp


def parse_spec(doc: Any, source: str = "<spec>") -> Spec:
    """Validate a parsed YAML document and return a typed :class:`Spec`."""
    if not isinstance(doc, dict):
        raise SpecError(f"{source}: top level of the spec must be a mapping")
    known = {"run", "target", "workload", "sweep", "capture", "report", "soak", "pmm",
             "suite", "cluster", "limits", "device_probe"}
    unknown = set(doc) - known
    if unknown:
        hint = ""
        if "events" in unknown:
            # Spec-declared events were removed: timeline events now come ONLY from
            # auto-detection or operator marks (live cockpit / report stamping).
            hint = (" — the 'events' section was removed; mark events live via the "
                    "console or `pgbench-harness mark`, or rely on auto-detection")
        raise SpecError(f"unknown top-level section(s): {', '.join(sorted(unknown))}{hint}")
    modes = [m for m in ("sweep", "soak", "suite") if m in doc]
    if len(modes) > 1:
        raise SpecError(f"spec has {' and '.join(modes)}; they are mutually exclusive "
                        "(sweep = thread sweep, soak = resilience run, "
                        "suite = full evidentiary matrix)")
    if not modes and "device_probe" not in doc:
        raise SpecError("spec must contain a 'sweep' (steady-state), 'soak' "
                        "(resilience), 'suite' (evidence matrix), or 'device_probe' "
                        "section")
    sweep = _parse_sweep(_section(doc, "sweep")) if "sweep" in doc else None
    soak = _parse_soak(_section(doc, "soak")) if "soak" in doc else None
    suite = _parse_suite(_section(doc, "suite")) if "suite" in doc else None
    cluster = _parse_cluster(_section(doc, "cluster")) if "cluster" in doc else None
    limits = _parse_limits(_section(doc, "limits")) if "limits" in doc else Limits()
    device_probe = (_parse_device_probe(_section(doc, "device_probe"))
                    if "device_probe" in doc else None)
    if device_probe is not None and cluster is None:
        raise SpecError("'device_probe' needs a 'cluster' section — the probe pod "
                        "mounts the cluster's pgdata PVC")
    pmm = None
    if "pmm" in doc:
        psec = _section(doc, "pmm")
        _check_keys(psec, "pmm", {"server_host"}, {"service_name"})
        host = _typed(psec, "pmm", "server_host", str)
        if not host.strip():
            raise SpecError("'pmm.server_host' must be a non-empty string")
        pmm = Pmm(server_host=host.strip(),
                  service_name=_typed(psec, "pmm", "service_name", str, ""))
    return Spec(
        run=_parse_run(_section(doc, "run")),
        target=_parse_target(_section(doc, "target")),
        workload=_parse_workload(_section(doc, "workload")),
        sweep=sweep,
        capture=_parse_capture(doc.get("capture") or {}),
        report=_parse_report(doc.get("report") or {}, sweep),
        soak=soak,
        pmm=pmm,
        suite=suite,
        cluster=cluster,
        limits=limits,
        device_probe=device_probe,
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
