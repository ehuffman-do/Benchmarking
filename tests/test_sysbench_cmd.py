"""sysbench command construction tests (tpcc + oltp, secrets policy)."""

from __future__ import annotations

from pgbench_harness.spec import parse_spec
from pgbench_harness.sysbench import build_prepare_command, build_run_command, child_env

from conftest import TEST_PASSWORD, make_spec_doc


def test_oltp_run_command() -> None:
    spec = parse_spec(make_spec_doc())
    cmd = build_run_command(spec, threads=4)
    assert cmd.argv[0] == "sysbench"
    assert cmd.argv[1] == "oltp_read_write"
    assert "--pgsql-host=db.example.invalid" in cmd.argv
    assert "--pgsql-port=5432" in cmd.argv
    assert "--pgsql-db=sbtest" in cmd.argv
    assert "--tables=9" in cmd.argv
    assert "--table-size=1000" in cmd.argv
    assert "--threads=4" in cmd.argv
    assert "--time=5" in cmd.argv
    assert "--report-interval=1" in cmd.argv
    assert "--percentile=99" in cmd.argv
    assert "--histogram" in cmd.argv
    assert cmd.argv[-1] == "run"
    assert cmd.cwd is None


def test_tpcc_run_command_matches_percona_invocation() -> None:
    doc = make_spec_doc(workload={
        "type": "tpcc", "tpcc_path": "/opt/sysbench-tpcc", "tables": 10, "scale": 30,
        "extra_args": ["--use_fk=0", "--trx_level=RC"],
    })
    doc["workload"].pop("table_size", None)
    spec = parse_spec(doc)
    cmd = build_run_command(spec, threads=64)
    assert cmd.argv[1] == "./tpcc.lua"
    assert cmd.cwd == "/opt/sysbench-tpcc"
    assert "--tables=10" in cmd.argv
    assert "--scale=30" in cmd.argv
    assert "--use_fk=0" in cmd.argv
    assert "--trx_level=RC" in cmd.argv


def test_histogram_flag_respects_capture() -> None:
    doc = make_spec_doc(capture={"histogram": False})
    spec = parse_spec(doc)
    assert "--histogram" not in build_run_command(spec, 1).argv


def test_password_never_in_argv(monkeypatch) -> None:
    monkeypatch.setenv("PGB_TARGET_PASSWORD", TEST_PASSWORD)
    spec = parse_spec(make_spec_doc())
    for cmd in (build_run_command(spec, 256), build_prepare_command(spec)):
        assert TEST_PASSWORD not in " ".join(cmd.argv)
        assert "password" not in " ".join(cmd.argv).lower()
    env = child_env(spec, TEST_PASSWORD)
    assert env["PGPASSWORD"] == TEST_PASSWORD
    assert env["PGSSLMODE"] == "require"


def test_prepare_threads_capped() -> None:
    doc = make_spec_doc(sweep={"threads": [1, 4, 256], "duration_s": 5, "warmup_s": 1,
                               "cooldown_s": 0, "repetitions": 1},
                        report={"timeseries_levels": []})
    cmd = build_prepare_command(parse_spec(doc))
    assert "--threads=16" in cmd.argv
    assert cmd.argv[-1] == "prepare"
