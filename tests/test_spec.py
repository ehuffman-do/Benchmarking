"""Spec validation tests: strict schema, clear failure messages, secret rules."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pgbench_harness.errors import SpecError
from pgbench_harness.spec import load_spec, parse_spec

from conftest import make_spec_doc


def test_valid_spec_parses() -> None:
    spec = parse_spec(make_spec_doc())
    assert spec.run.label == "test-tiny"
    assert spec.run.edition == "advanced"
    assert spec.sweep.threads == (1, 4)
    assert spec.sweep.repetitions == 2
    assert spec.capture.pg_stat_statements == "auto"
    assert spec.report.percentiles == (50, 95, 99)


def test_defaults_applied() -> None:
    doc = make_spec_doc()
    del doc["capture"]
    del doc["report"]
    del doc["sweep"]["warmup_s"]
    del doc["sweep"]["cooldown_s"]
    del doc["sweep"]["repetitions"]
    spec = parse_spec(doc)
    assert spec.sweep.warmup_s == 0
    assert spec.sweep.repetitions == 1
    assert spec.sweep.retries == 0
    assert spec.sweep.retry_wait_s == 30
    assert spec.capture.histogram is True
    assert spec.report.variance_warn_pct == 10.0


def test_retries_parsed() -> None:
    doc = make_spec_doc()
    doc["sweep"]["retries"] = 5
    doc["sweep"]["retry_wait_s"] = 180
    spec = parse_spec(doc)
    assert spec.sweep.retries == 5
    assert spec.sweep.retry_wait_s == 180


@pytest.mark.parametrize("mutate, fragment", [
    (lambda d: d.__setitem__("bogus_section", {}), "bogus_section"),
    (lambda d: d["run"].__setitem__("color", "blue"), "run"),
    (lambda d: d["run"].pop("label"), "label"),
    (lambda d: d["run"].__setitem__("edition", "premium"), "edition"),
    (lambda d: d["target"].pop("password_env"), "password_env"),
    (lambda d: d["target"].__setitem__("password", "oops"), "never contain a password"),
    (lambda d: d["sweep"].__setitem__("threads", []), "threads"),
    (lambda d: d["sweep"].__setitem__("threads", [1, "two"]), "threads"),
    (lambda d: d["sweep"].__setitem__("warmup_s", 5), "warmup_s"),
    (lambda d: d["sweep"].__setitem__("repetitions", 0), "repetitions"),
    (lambda d: d["sweep"].__setitem__("retries", -1), "retries"),
    (lambda d: d["sweep"].__setitem__("retry_wait_s", -5), "retry_wait_s"),
    (lambda d: d["workload"].__setitem__("type", "ycsb"), "workload.type"),
    (lambda d: d["report"].__setitem__("timeseries_levels", [3]), "timeseries_levels"),
    (lambda d: d["capture"].__setitem__("pg_stat_statements", "maybe"), "pg_stat_statements"),
], ids=["unknown-section", "unknown-key", "missing-label", "bad-edition",
        "missing-password-env", "inline-password", "empty-threads", "non-int-threads",
        "warmup-ge-duration", "zero-reps", "neg-retries", "neg-retry-wait",
        "bad-workload", "ts-level-not-in-ladder", "bad-pss"])
def test_invalid_specs_fail_with_clear_message(mutate, fragment) -> None:
    doc = make_spec_doc()
    mutate(doc)
    with pytest.raises(SpecError) as exc:
        parse_spec(doc)
    assert fragment in str(exc.value)


def test_tpcc_requires_path_and_scale() -> None:
    doc = make_spec_doc(workload={"type": "tpcc", "tables": 10})
    doc["workload"].pop("table_size", None)
    with pytest.raises(SpecError, match="tpcc_path"):
        parse_spec(doc)
    doc["workload"]["tpcc_path"] = "/opt/sysbench-tpcc"
    with pytest.raises(SpecError, match="scale"):
        parse_spec(doc)
    doc["workload"]["scale"] = 30
    assert parse_spec(doc).workload.scale == 30


def test_oltp_requires_table_size() -> None:
    doc = make_spec_doc()
    del doc["workload"]["table_size"]
    with pytest.raises(SpecError, match="table_size"):
        parse_spec(doc)


def test_password_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGB_TARGET_PASSWORD", raising=False)
    spec = parse_spec(make_spec_doc())
    with pytest.raises(SpecError, match="PGB_TARGET_PASSWORD"):
        spec.password()


def test_load_spec_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="not found"):
        load_spec(tmp_path / "nope.yaml")


def test_load_spec_bad_yaml(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("run: [unclosed", encoding="utf-8")
    with pytest.raises(SpecError, match="not valid YAML"):
        load_spec(p)


def test_load_spec_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "ok.yaml"
    p.write_text(yaml.safe_dump(make_spec_doc()), encoding="utf-8")
    spec = load_spec(p)
    assert spec.target.host == "db.example.invalid"
