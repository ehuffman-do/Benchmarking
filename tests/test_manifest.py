"""Manifest plan/resume/status tests."""

from __future__ import annotations

from pathlib import Path

from pgbench_harness.manifest import (
    STATUS_FAILED, STATUS_OK, STATUS_RUNNING, Manifest, plan_levels,
)


def make_manifest() -> Manifest:
    return Manifest(
        run_id="r1", label="l", edition="standard", tshirt_size="4c16g",
        levels=plan_levels((1, 4, 16), repetitions=2),
    )


def test_plan_levels_order() -> None:
    levels = plan_levels((1, 4), 2)
    assert [(l.rep, l.threads) for l in levels] == [(1, 1), (1, 4), (2, 1), (2, 4)]


def test_pending_levels_skips_completed() -> None:
    m = make_manifest()
    m.levels[0].status = STATUS_OK
    m.levels[1].status = STATUS_FAILED        # completed outcome: not retried
    m.levels[2].status = STATUS_RUNNING       # crashed mid-level: retried
    pending = m.pending_levels()
    assert [(l.rep, l.threads) for l in pending] == [(1, 16), (2, 1), (2, 4), (2, 16)]


def test_finalize_status() -> None:
    m = make_manifest()
    for l in m.levels:
        l.status = STATUS_OK
    assert m.finalize_status() == "complete"
    m.levels[0].status = STATUS_FAILED
    assert m.finalize_status() == "partial"
    for l in m.levels:
        l.status = STATUS_FAILED
    assert m.finalize_status() == "failed"


def test_save_load_roundtrip(tmp_path: Path) -> None:
    m = make_manifest()
    m.levels[0].status = STATUS_OK
    m.levels[0].raw_log = "raw/rep1_t001.log"
    m.levels[1].status = STATUS_FAILED
    m.levels[1].error_excerpt = "FATAL: Worker threads failed to initialize within 30 seconds!"
    m.save(tmp_path)
    loaded = Manifest.load(tmp_path)
    assert loaded.run_id == "r1"
    assert loaded.level(1, 1).status == STATUS_OK
    assert loaded.level(1, 4).error_excerpt.startswith("FATAL")
    assert len(loaded.pending_levels()) == 4


def test_level_key_format() -> None:
    m = make_manifest()
    assert m.level(1, 4).key == "rep1_t004"
    assert m.level(2, 16).key == "rep2_t016"
