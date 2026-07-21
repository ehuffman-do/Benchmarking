"""pgbench command builders + output parser — the independent second driver.

Mirrors sysbench.py's builder/parser split so the runner treats both drivers
uniformly: build argv (password never in argv — libpq env), stream output,
parse per-second progress lines into IntervalSample-compatible rows. pgbench's
`--progress` reports *average* latency (not a percentile); the summary's
"latency average" is parsed too and both are labeled as averages downstream.
"""

from __future__ import annotations

import re
import subprocess
from typing import Optional

from pgbench_harness.parser import IntervalSample
from pgbench_harness.spec import Spec
from pgbench_harness.sysbench import SysbenchCommand, _line_buffered  # noqa: F401

# lat/stddev can BOTH be NaN on a zero-transaction interval (a stall — the
# exact sample a contended suite cell must not lose)
PROGRESS_RE = re.compile(
    r"progress:\s+(?P<t>[\d.]+)\s+s,\s+(?P<tps>[\d.]+)\s+tps,\s+"
    r"lat\s+(?P<lat>[\d.]+|-?[Nn]a[Nn])\s+ms\s+stddev\s+"
    r"(?P<std>[\d.]+|-?[Nn]a[Nn])"
    r"(?:,\s+(?P<failed>\d+)\s+failed)?")

SUMMARY_TPS_RE = re.compile(r"^tps = (?P<tps>[\d.]+)", re.MULTILINE)
SUMMARY_LAT_RE = re.compile(r"^latency average = (?P<lat>[\d.]+) ms", re.MULTILINE)
SUMMARY_TXN_RE = re.compile(r"number of transactions actually processed: (?P<n>\d+)",)
SUMMARY_FAILED_RE = re.compile(r"number of failed transactions: (?P<n>\d+)")


def _connection_args(spec: Spec) -> list[str]:
    t = spec.target
    return ["-h", t.host, "-p", str(t.port), "-U", t.user]


def build_pgbench_init(spec: Spec, scale: int) -> SysbenchCommand:
    """`pgbench -i` — creates/loads pgbench_accounts et al (drops its own
    tables only; sbtest tables are untouched)."""
    argv = (["pgbench", "-i", "-s", str(scale), "--foreign-keys=off"]
            + _connection_args(spec) + [spec.target.database])
    return SysbenchCommand(argv=tuple(argv), cwd=None)


def build_pgbench_run(spec: Spec, clients: int, duration_s: int,
                      select_only: bool) -> SysbenchCommand:
    argv = (["pgbench"]
            + _connection_args(spec)
            + ["-c", str(clients), "-j", str(min(clients, 8)),
               "-T", str(duration_s), "-P", "1", "--no-vacuum"]
            + (["-S"] if select_only else [])
            + [spec.target.database])
    return SysbenchCommand(argv=tuple(argv), cwd=None)


def parse_pgbench_progress(line: str) -> Optional[IntervalSample]:
    m = PROGRESS_RE.search(line)
    if not m:
        return None
    tps = float(m.group("tps"))
    lat = 0.0 if "nan" in m.group("lat").lower() else float(m.group("lat"))
    failed = float(m.group("failed") or 0)
    return IntervalSample(
        t_offset=float(m.group("t")), threads=0, tps=tps, qps=tps,
        r=0.0, w=0.0, o=0.0, lat_pct=0, lat_ms=lat, err_s=failed, reconn_s=0.0)


def parse_pgbench_summary(text: str) -> dict:
    out: dict = {"tps": None, "lat_avg_ms": None, "transactions": None, "failed": 0}
    m = SUMMARY_TPS_RE.search(text)
    if m:
        out["tps"] = float(m.group("tps"))
    m = SUMMARY_LAT_RE.search(text)
    if m:
        out["lat_avg_ms"] = float(m.group("lat"))
    m = SUMMARY_TXN_RE.search(text)
    if m:
        out["transactions"] = int(m.group("n"))
    m = SUMMARY_FAILED_RE.search(text)
    if m:
        out["failed"] = int(m.group("n"))
    return out


def pgbench_version() -> str:
    try:
        res = subprocess.run(["pgbench", "--version"], capture_output=True,
                             text=True, timeout=10)
        return (res.stdout or res.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
