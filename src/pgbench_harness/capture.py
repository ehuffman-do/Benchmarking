"""Database/environment capture and preflight checks (via psql).

psql is invoked with the password in ``PGPASSWORD`` and sslmode in
``PGSSLMODE``; neither ever appears on a command line or in a stored file.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pgbench_harness import __version__
from pgbench_harness.errors import PreflightError
from pgbench_harness.spec import Spec
from pgbench_harness.sysbench import child_env, sysbench_version
from pgbench_harness.util import get_redactor

PSQL_TIMEOUT_S = 30
# How long the ceiling probe waits for all holders to establish before
# declaring success. Override (e.g. for tests/slow links) via env var.
PROBE_CONNECT_GRACE_S = float(os.environ.get("PGB_PROBE_GRACE_S", "6.0"))
PROBE_HOLD_SQL = "SELECT pg_sleep(30)"

KEY_SETTINGS = [
    "shared_buffers",
    "effective_cache_size",
    "max_wal_size",
    "checkpoint_timeout",
    "synchronous_commit",
    "max_connections",
    "work_mem",
    "random_page_cost",
    "wal_buffers",
    "huge_pages",
]


@dataclass
class ProbeResult:
    """Outcome of the connection-ceiling probe."""

    requested: int
    succeeded: int
    first_failed_index: Optional[int] = None
    first_error: str = ""

    @property
    def ok(self) -> bool:
        return self.succeeded >= self.requested


@dataclass
class DatasetCheck:
    """Outcome of the dataset conformance check (presence AND size vs spec)."""

    status: str = "error"  # ok | missing | incomplete | mismatch | error
    detail: str = ""
    expected_tables: int = 0
    present_tables: int = 0
    foreign_tables: int = 0
    expected_size: int = 0
    actual_size: Optional[int] = None
    size_unit: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class PreflightResult:
    """Everything preflight learned, recorded into the run manifest/env."""

    sysbench_version: str = ""
    psql_version: str = ""
    tpcc_git_sha: str = ""
    server_version_full: str = ""
    server_version: str = ""
    max_connections: str = ""
    pooler_probe: str = ""
    pg_stat_statements: bool = False
    dataset: Optional[DatasetCheck] = None
    probe: Optional[ProbeResult] = None
    warnings: list[str] = field(default_factory=list)


def _psql_argv(spec: Spec, sql: str) -> list[str]:
    t = spec.target
    return [
        "psql", "-h", t.host, "-p", str(t.port), "-U", t.user, "-d", t.database,
        "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]


def psql_query(spec: Spec, password: str, sql: str, timeout: int = PSQL_TIMEOUT_S) -> str:
    """Run one SQL statement via psql and return trimmed stdout.

    Raises PreflightError with the verbatim (redacted) server error on failure.
    """
    try:
        proc = subprocess.run(
            _psql_argv(spec, sql),
            env=child_env(spec, password),
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PreflightError(
            "psql is not installed or not on PATH",
            hint="apt-get install postgresql-client (see README).",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PreflightError(
            f"psql timed out after {timeout}s connecting to "
            f"{spec.target.host}:{spec.target.port}",
            hint="check host/port, VPC reachability and firewall rules.",
        ) from exc
    if proc.returncode != 0:
        err = get_redactor().redact(proc.stderr.strip())
        raise PreflightError(
            f"psql query failed against {spec.target.host}:{spec.target.port}: {err}",
            hint="verify credentials (password_env), database name and sslmode.",
        )
    return proc.stdout.strip()


def psql_query_soft(spec: Spec, password: str, sql: str) -> tuple[bool, str]:
    """Like psql_query but returns (ok, output_or_error) instead of raising."""
    try:
        return True, psql_query(spec, password, sql)
    except PreflightError as exc:
        return False, str(exc)


def psql_version() -> str:
    """Return `psql --version` output, raising PreflightError if missing."""
    try:
        out = subprocess.run(["psql", "--version"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise PreflightError(
            "psql is not installed or not on PATH",
            hint="apt-get install postgresql-client (see README).",
        ) from exc
    return out.stdout.strip()


def tpcc_git_sha(tpcc_path: str) -> str:
    """Return the git SHA of the sysbench-tpcc checkout (or a marker if unknown)."""
    if not tpcc_path:
        return "n/a (oltp workload)"
    try:
        out = subprocess.run(
            ["git", "-C", tpcc_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown (not a git checkout)"


def harness_version() -> str:
    """Harness package version, plus git SHA when running from a checkout."""
    sha = ""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            sha = " @ " + out.stdout.strip()[:12]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"pgbench-harness {__version__}{sha}"


def host_info() -> str:
    """Load-generator host info: uname, CPU count, memory."""
    uname = platform.uname()
    lines = [
        f"uname: {' '.join(uname)}",
        f"cpu_count: {os.cpu_count()}",
    ]
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith(("MemTotal", "MemAvailable", "SwapTotal")):
                lines.append(line.strip())
    return "\n".join(lines) + "\n"


def connection_ceiling_probe(
    spec: Spec, password: str, count: int, logger: logging.Logger
) -> ProbeResult:
    """Open *count* simultaneous connections (cheap SELECT pg_sleep holders).

    Connections are launched in order with a tiny stagger; after a grace
    period every process still running is counted as an established holder
    and any early exit is a refusal. The first failed launch index
    approximates the connection count at which the target refused, and its
    stderr is captured verbatim.
    """
    logger.info("preflight: connection-ceiling probe with %d simultaneous connections", count)
    env = child_env(spec, password)
    procs: list[subprocess.Popen[str]] = []
    try:
        for _ in range(count):
            procs.append(subprocess.Popen(
                _psql_argv(spec, PROBE_HOLD_SQL),
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            ))
            time.sleep(0.01)
        grace = float(os.environ.get("PGB_PROBE_GRACE_S", str(PROBE_CONNECT_GRACE_S)))
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in procs):
                time.sleep(0.5)  # let the remaining refusals land
                break
            time.sleep(0.2)
        result = ProbeResult(requested=count, succeeded=0)
        for idx, p in enumerate(procs, start=1):
            if p.poll() is None or p.returncode == 0:
                result.succeeded += 1
            elif result.first_failed_index is None:
                result.first_failed_index = idx
                stderr = p.stderr.read().strip() if p.stderr else ""
                result.first_error = get_redactor().redact(stderr)
        return result
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


TPCC_TABLE_BASES = (
    "warehouse", "district", "customer", "history", "orders",
    "new_orders", "order_line", "stock", "item",
)
OLTP_SIZE_TOLERANCE = 0.10  # oltp row counts may drift slightly under R/W load


def expected_table_names(spec: Spec) -> list[str]:
    """The exact benchmark tables the configured workload owns."""
    w = spec.workload
    if w.type == "tpcc":
        return [f"{base}{i}" for i in range(1, w.tables + 1) for base in TPCC_TABLE_BASES]
    return [f"sbtest{i}" for i in range(1, w.tables + 1)]


def benchmark_table_pattern(spec: Spec) -> str:
    """POSIX regex matching this workload's whole table namespace (any suffix).

    Broader than ``expected_table_names`` on purpose: it also catches leftovers
    from a previous run with a different ``tables`` count (e.g. ``sbtest17`` when
    the current spec only configures 16), so ``--clean`` fully resets the cluster.
    """
    if spec.workload.type == "tpcc":
        return f"^({'|'.join(TPCC_TABLE_BASES)})[0-9]+$"
    return "^sbtest[0-9]+$"


def find_benchmark_tables(spec: Spec, password: str) -> list[str]:
    """Public base tables whose names belong to this workload's namespace."""
    ok, out = psql_query_soft(
        spec, password,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' "
        f"AND table_name ~ '{benchmark_table_pattern(spec)}' ORDER BY table_name",
    )
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if ok else []


def drop_benchmark_tables(spec: Spec, password: str, logger: logging.Logger) -> list[str]:
    """``DROP ... CASCADE`` every benchmark-namespace table for this workload.

    Only tables matching the tool's own naming pattern (``sbtest<N>`` or the nine
    tpcc tables ``<base><N>``) are dropped, so unrelated user tables in the same
    database are never touched. CASCADE handles the tpcc foreign keys. Returns
    the names that were dropped.
    """
    names = find_benchmark_tables(spec, password)
    if not names:
        logger.info("clean: no benchmark tables found for this workload; nothing to drop")
        return []
    logger.warning("clean: dropping %d benchmark table(s): %s", len(names), ", ".join(names))
    quoted = ", ".join(f'public."{n}"' for n in names)
    psql_query(spec, password, f"DROP TABLE IF EXISTS {quoted} CASCADE", timeout=120)
    logger.info("clean: dropped %d table(s)", len(names))
    return names


def _count_query(spec: Spec, password: str, sql: str) -> Optional[int]:
    ok, out = psql_query_soft(spec, password, sql)
    return int(out) if ok and out.lstrip("-").isdigit() else None


def database_size_bytes(spec: Spec, password: str) -> Optional[int]:
    """Current size of the target database in bytes (best effort)."""
    return _count_query(
        spec, password, "SELECT pg_database_size(current_database())")


def _check_canary_schema(spec: Spec, password: str) -> tuple[bool, str]:
    """Verify the canary table has the columns the workload's schema defines."""
    cols: tuple[str, ...]
    if spec.workload.type == "tpcc":
        table, cols = "warehouse1", ("w_id", "w_ytd")
    else:
        table, cols = "sbtest1", ("id", "k", "c", "pad")
    quoted = ", ".join(f"'{c}'" for c in cols)
    n = _count_query(
        spec, password,
        f"SELECT count(*) FROM information_schema.columns "
        f"WHERE table_schema='public' AND table_name='{table}' "
        f"AND column_name IN ({quoted})",
    )
    if n != len(cols):
        return False, (
            f"table '{table}' exists but does not have the expected {spec.workload.type} "
            f"columns ({', '.join(cols)}) — it was probably not created by this tool"
        )
    return True, ""


def _check_dataset_size(spec: Spec, password: str, chk: DatasetCheck) -> None:
    """Compare loaded dataset size against the spec; sets mismatch status."""
    w = spec.workload
    if w.type == "tpcc":
        chk.expected_size = w.scale
        chk.size_unit = "warehouses per table-set"
        chk.actual_size = _count_query(
            spec, password, "SELECT count(*) FROM warehouse1")
        bad = chk.actual_size != w.scale
    else:
        chk.expected_size = w.table_size
        chk.size_unit = "rows in sbtest1"
        chk.actual_size = _count_query(spec, password, "SELECT max(id) FROM sbtest1")
        bad = (
            chk.actual_size is None
            or abs(chk.actual_size - w.table_size) > w.table_size * OLTP_SIZE_TOLERANCE
        )
    if bad:
        chk.status = "mismatch"
        chk.detail = (
            f"dataset size does not match the spec: found {chk.actual_size} "
            f"{chk.size_unit}, but the spec configures {chk.expected_size}. "
            "The harness never reloads or tops up on top of an existing dataset."
        )


def check_dataset(spec: Spec, password: str) -> DatasetCheck:
    """Verify the dataset is present AND conforms to the spec's configured size.

    Checks, in order: every expected benchmark table exists (by name, so
    unrelated tables can't satisfy the check), the canary table has the
    expected schema, and the loaded size matches `scale`/`table_size`
    (tpcc: exact warehouse count; oltp: ±10% on max(id), since R/W runs may
    drift row ids slightly). Also counts foreign (non-benchmark) tables so
    callers can warn about shared databases.
    """
    names = expected_table_names(spec)
    quoted = ", ".join(f"'{n}'" for n in names)
    chk = DatasetCheck(expected_tables=len(names))
    present = _count_query(
        spec, password,
        f"SELECT count(*) FROM information_schema.tables "
        f"WHERE table_schema='public' AND table_name IN ({quoted})",
    )
    if present is None:
        chk.detail = "could not query information_schema (connectivity/permissions?)"
        return chk
    chk.present_tables = present
    chk.foreign_tables = _count_query(
        spec, password,
        f"SELECT count(*) FROM information_schema.tables "
        f"WHERE table_schema='public' AND table_type='BASE TABLE' "
        f"AND table_name NOT IN ({quoted})",
    ) or 0
    if present == 0:
        chk.status = "missing"
        chk.detail = f"none of the {len(names)} expected benchmark tables exist"
        return chk
    if present < len(names):
        chk.status = "incomplete"
        chk.detail = (
            f"only {present} of {len(names)} expected benchmark tables exist — "
            "a previous load was interrupted, or the spec's `tables` changed"
        )
        return chk
    schema_ok, schema_err = _check_canary_schema(spec, password)
    if not schema_ok:
        chk.status = "incomplete"
        chk.detail = schema_err
        return chk
    chk.status = "ok"
    _check_dataset_size(spec, password, chk)
    if chk.ok:
        chk.detail = (
            f"all {len(names)} benchmark tables present; "
            f"{chk.actual_size} {chk.size_unit} (matches spec)"
        )
    return chk


def is_writable(spec: Spec, password: str) -> tuple[bool, str]:
    """Whether the target currently accepts writes.

    ``transaction_read_only`` is ``on`` both on a hot standby (so right after a
    managed-PG failover, before promotion completes) and when
    ``default_transaction_read_only`` is set — i.e. exactly the states that make
    every UPDATE fail with "cannot execute ... in a read-only transaction".
    Returns ``(writable, detail)``; an unreachable target counts as not writable.
    """
    ok, out = psql_query_soft(spec, password, "SHOW transaction_read_only")
    if not ok:
        return False, "target unreachable"
    val = out.strip().lower()
    return val == "off", f"transaction_read_only={val or '?'}"


def wait_for_writable(
    spec: Spec, password: str, timeout_s: int, logger: logging.Logger, poll_s: float = 3.0
) -> bool:
    """Poll until the target accepts writes again, up to *timeout_s* seconds.

    Used between level retries so a retry does not get spent connecting to a
    node that is still read-only mid-failover. Returns True as soon as the
    target is writable, False if the timeout elapses first. ``timeout_s == 0``
    performs a single immediate check.
    """
    deadline = time.monotonic() + max(0, timeout_s)
    waited = False
    while True:
        writable, detail = is_writable(spec, password)
        if writable:
            if waited:
                logger.info("    target writable again (%s)", detail)
            return True
        if time.monotonic() >= deadline:
            if timeout_s > 0:
                logger.warning(
                    "    target still not writable after %ds (%s)", timeout_s, detail)
            return False
        waited = True
        logger.info("    waiting for target to become writable (%s) ...", detail)
        time.sleep(min(poll_s, max(0.1, deadline - time.monotonic())))


def detect_pooler(spec: Spec, password: str) -> str:
    """Best-effort pooler detection; records raw behavior, never fails preflight.

    `SHOW pool_mode` is a PgBouncer admin-console command and is expected to
    fail against both PgBouncer app databases and plain PostgreSQL — the
    verbatim response is recorded as metadata either way.
    """
    ok, out = psql_query_soft(spec, password, "SHOW pool_mode")
    if ok:
        return f"pool_mode={out} (pooler admin interface answered)"
    return f"SHOW pool_mode failed (expected against PgBouncer app DBs / plain PG): {out}"


def detect_pg_stat_statements(spec: Spec, password: str) -> bool:
    """True when the pg_stat_statements extension is installed."""
    ok, out = psql_query_soft(
        spec, password,
        "SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'",
    )
    return ok and out.strip() == "1"


def snapshot_bgwriter(spec: Spec, password: str) -> str:
    """One-row JSON snapshot of pg_stat_bgwriter (column-set agnostic)."""
    ok, out = psql_query_soft(
        spec, password, "SELECT row_to_json(t) FROM pg_stat_bgwriter t")
    return out if ok else f'{{"error": "{out[:200]}"}}'


def snapshot_pg_stat_statements(spec: Spec, password: str, limit: int = 50) -> str:
    """Top statements by total time as JSON rows (best effort)."""
    sql = (
        "SELECT coalesce(json_agg(t), '[]'::json) FROM ("
        "SELECT queryid, calls, total_exec_time, mean_exec_time, rows "
        f"FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT {limit}) t"
    )
    ok, out = psql_query_soft(spec, password, sql)
    return out if ok else "[]"


def capture_pg_settings(spec: Spec, password: str) -> str:
    """Full pg_settings dump as CSV text (name,setting,unit,source)."""
    return psql_query(
        spec, password,
        "COPY (SELECT name, setting, unit, source FROM pg_settings ORDER BY name) "
        "TO STDOUT WITH CSV HEADER",
        timeout=60,
    )


def capture_env(run_dir: Path, spec: Spec, password: str, pf: PreflightResult) -> None:
    """Write the env/ capture directory (settings, versions, host info)."""
    env_dir = run_dir / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    if spec.capture.pg_settings:
        (env_dir / "pg_settings.csv").write_text(
            capture_pg_settings(spec, password) + "\n", encoding="utf-8")
    (env_dir / "server_version.txt").write_text(
        pf.server_version_full + "\n", encoding="utf-8")
    (env_dir / "sysbench_version.txt").write_text(pf.sysbench_version + "\n", encoding="utf-8")
    (env_dir / "tpcc_git_sha.txt").write_text(pf.tpcc_git_sha + "\n", encoding="utf-8")
    (env_dir / "harness_git_sha.txt").write_text(harness_version() + "\n", encoding="utf-8")
    (env_dir / "host_info.txt").write_text(host_info(), encoding="utf-8")


def run_preflight(spec: Spec, password: str, logger: logging.Logger) -> PreflightResult:
    """Run all preflight checks; raise PreflightError on any hard failure."""
    pf = PreflightResult()
    pf.sysbench_version = sysbench_version()
    pf.psql_version = psql_version()
    pf.tpcc_git_sha = tpcc_git_sha(spec.workload.tpcc_path)
    logger.info("preflight: %s | %s | tpcc %s", pf.sysbench_version, pf.psql_version, pf.tpcc_git_sha)
    if spec.workload.type == "tpcc" and not Path(spec.workload.tpcc_path, "tpcc.lua").exists():
        raise PreflightError(
            f"tpcc.lua not found in workload.tpcc_path '{spec.workload.tpcc_path}'",
            hint="git clone https://github.com/Percona-Lab/sysbench-tpcc to that path.",
        )
    pf.server_version_full = psql_query(spec, password, "SELECT version()")
    pf.server_version = psql_query(spec, password, "SHOW server_version")
    pf.max_connections = psql_query(spec, password, "SHOW max_connections")
    pf.pooler_probe = detect_pooler(spec, password)
    logger.info("preflight: server %s, max_connections=%s", pf.server_version, pf.max_connections)
    logger.info("preflight: pooler probe: %s", pf.pooler_probe)
    _check_pg_stat_statements(spec, password, pf)
    _check_ceiling(spec, password, pf, logger)
    pf.dataset = check_dataset(spec, password)
    logger.info("preflight: dataset [%s]: %s", pf.dataset.status, pf.dataset.detail)
    _collect_warnings(spec, pf)
    for w in pf.warnings:
        logger.warning("preflight: %s", w)
    return pf


def _collect_warnings(spec: Spec, pf: PreflightResult) -> None:
    """Non-fatal conditions worth surfacing in logs, manifest and report."""
    cpus = os.cpu_count() or 1
    max_threads = max(spec.sweep.threads)
    if max_threads > cpus * 8:
        pf.warnings.append(
            f"sweep peaks at {max_threads} client threads but this load generator "
            f"has only {cpus} CPUs — the loadgen itself may become the bottleneck "
            "at high thread counts (check host_info.txt when interpreting results)"
        )
    if pf.dataset and pf.dataset.foreign_tables:
        pf.warnings.append(
            f"{pf.dataset.foreign_tables} non-benchmark table(s) exist in schema "
            "'public' — this database is shared, so cache/disk contention from "
            "that data may pollute results; a dedicated database is recommended"
        )


def _check_pg_stat_statements(spec: Spec, password: str, pf: PreflightResult) -> None:
    mode = spec.capture.pg_stat_statements
    if mode == "false":
        return
    pf.pg_stat_statements = detect_pg_stat_statements(spec, password)
    if mode == "true" and not pf.pg_stat_statements:
        raise PreflightError(
            "capture.pg_stat_statements is true but the extension is not installed",
            hint="CREATE EXTENSION pg_stat_statements, or set capture.pg_stat_statements to auto/false.",
        )


def _check_ceiling(
    spec: Spec, password: str, pf: PreflightResult, logger: logging.Logger
) -> None:
    count = max(spec.sweep.threads)
    pf.probe = connection_ceiling_probe(spec, password, count, logger)
    if not pf.probe.ok:
        raise PreflightError(
            f"connection-ceiling probe failed: only {pf.probe.succeeded} of "
            f"{pf.probe.requested} simultaneous connections succeeded; connection "
            f"#{pf.probe.first_failed_index} was refused with:\n  {pf.probe.first_error}",
            hint=(
                "the target (likely its pooler, e.g. PgBouncer max_client_conn) refuses "
                f"{count} clients. Raise the pooler/client limit or trim sweep.threads "
                "below the ceiling before launching a long sweep."
            ),
        )
    logger.info(
        "preflight: connection ceiling OK (%d/%d simultaneous connections)",
        pf.probe.succeeded, pf.probe.requested,
    )
