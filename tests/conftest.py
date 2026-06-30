"""Shared test fixtures: fake sysbench/psql binaries and a tiny run spec."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
FAKEBIN = TESTS_DIR / "fakebin"

TEST_PASSWORD = "s3cr3t-hunter2-do-not-leak"


def make_spec_doc(**overrides: object) -> dict:
    """A minimal valid oltp spec document; override sections via kwargs."""
    doc: dict = {
        "run": {
            "label": "test-tiny",
            "edition": "advanced",
            "tshirt_size": "4c16g",
            "notes": "unit-test spec",
        },
        "target": {
            "host": "db.example.invalid",
            "port": 5432,
            "database": "sbtest",
            "user": "doadmin",
            "password_env": "PGB_TARGET_PASSWORD",
            "sslmode": "require",
        },
        "workload": {
            "type": "oltp_read_write",
            "tables": 9,
            "table_size": 1000,
            "extra_args": [],
        },
        "sweep": {
            "threads": [1, 4],
            "duration_s": 5,
            "warmup_s": 1,
            "cooldown_s": 0,
            "repetitions": 2,
        },
        "capture": {
            "pg_settings": True,
            "pg_stat_monitor": "auto",
            "bgwriter_stats": True,
            "histogram": True,
        },
        "report": {
            "percentiles": [50, 95, 99],
            "timeseries_levels": [4],
            "variance_warn_pct": 10,
        },
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and key in doc:
            doc[key] = {**doc[key], **val}
        else:
            doc[key] = val
    return doc


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    """Write the default tiny spec to disk and return its path."""
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(make_spec_doc()), encoding="utf-8")
    return path


@pytest.fixture()
def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put fake sysbench/psql on PATH and set the password env var."""
    for exe in ("sysbench", "psql"):
        path = FAKEBIN / exe
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGB_TARGET_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("PGB_PROBE_GRACE_S", "0.4")
    state = tmp_path / "fake_state"
    state.mkdir()
    monkeypatch.setenv("FAKE_PSQL_STATE", str(state))
    return state
