"""Unit tests for the long-soak resilience features: the run watchdog and the
post-failover read-only poll."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
import yaml

from conftest import make_spec_doc
from pgbench_harness import capture
from pgbench_harness.spec import load_spec
from pgbench_harness.sysbench import WATCHDOG_EXIT_CODE, SysbenchCommand, run_streaming

LOGGER = logging.getLogger("test")


def _spec(tmp_path: Path) -> "object":
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(make_spec_doc()), encoding="utf-8")
    return load_spec(path)


def test_watchdog_kills_hung_process(tmp_path: Path) -> None:
    """A process that produces no output past the hard limit is killed and
    reported as a failed attempt (WATCHDOG_EXIT_CODE), not left to hang."""
    log = tmp_path / "out.log"
    cmd = SysbenchCommand(argv=("sleep", "30"), cwd=None)
    t0 = time.monotonic()
    rc = run_streaming(cmd, {}, log, LOGGER, hard_timeout_s=1, stall_timeout_s=1)
    elapsed = time.monotonic() - t0
    assert rc == WATCHDOG_EXIT_CODE
    assert elapsed < 15  # killed promptly, nowhere near the 30s sleep
    assert "watchdog aborted" in log.read_text()


def test_watchdog_does_not_kill_quick_success(tmp_path: Path) -> None:
    """A fast, well-behaved command finishes normally with the watchdog armed."""
    log = tmp_path / "out.log"
    cmd = SysbenchCommand(argv=("printf", "hello\\n"), cwd=None)
    rc = run_streaming(cmd, {}, log, LOGGER, hard_timeout_s=30, stall_timeout_s=30)
    assert rc == 0
    assert "watchdog aborted" not in log.read_text()


def test_wait_until_writable_returns_true_when_writable(fake_env, tmp_path) -> None:
    spec = _spec(tmp_path)
    assert capture.wait_until_writable(spec, "pw", timeout_s=2, logger=LOGGER) is True


def test_wait_until_writable_times_out_on_read_only(
    fake_env, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_PSQL_READ_ONLY", "on")  # standby, not yet promoted
    spec = _spec(tmp_path)
    # timeout_s=0 still probes exactly once, then gives up immediately.
    assert capture.wait_until_writable(spec, "pw", timeout_s=0, logger=LOGGER) is False
