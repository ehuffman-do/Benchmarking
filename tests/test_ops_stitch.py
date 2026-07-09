"""Stitcher unit tests against synthetic-but-realistic capture fixtures.

Each fixture encodes a field lesson: leader-name classification (NOT probe
IP), the T7 ready-count-dip latch, the pgBouncer backoff-tail math, and
probe-artifact windows excluded from downtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pgbench_harness.ops.stitch import (parse_iso_ms, stitch, stitch_run_dir)

BASE_MS = 1751900000000    # arbitrary fixed anchor


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def write_fixture(run_dir: Path, *, scenario: str, leader_before: str,
                  leader_after: str, tl_before: int, tl_after: int,
                  fire_ms: int, fail_from_ms: int, ok_again_ms: int,
                  addr_before: str = "10.0.0.1", addr_after: str = "10.0.0.1",
                  pgbouncer_lines: list[tuple[int, str]] = (),
                  samples: list[dict] = (), artifacts: list[int] = (),
                  probe_hz: float = 5.0) -> None:
    raw = run_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps(
        {"op_run_id": run_dir.name, "op": "scenario", "status": "complete",
         "target": {"name": "t"}, "label": scenario, "params": {},
         "created_utc": iso(BASE_MS), "headline": {}}))
    (raw / "fire.marker").write_text(json.dumps(
        {"ts_utc": iso(fire_ms), "ts_epoch_ms": fire_ms, "scenario": scenario,
         "target_pod": leader_before, "leader_before": leader_before,
         "tl_before": tl_before}))
    # probe: OK ticks before fail_from, FAILs until ok_again, OKs after
    step = int(1000 / probe_hz)
    lines = []
    for ms in range(fire_ms - 20000, fire_ms + 60000, step):
        if fail_from_ms <= ms < ok_again_ms:
            lines.append(f"FAIL {iso(ms)} connection refused")
        else:
            addr = addr_before if ms < fire_ms else addr_after
            lines.append(f"OK {iso(ms)} {iso(ms + 40)} f {addr}")
    (raw / "probe.log").write_text("\n".join(lines) + "\n")
    if not samples:
        samples = [
            {"ts_epoch_ms": fire_ms - 5000, "leader": leader_before,
             "timeline": tl_before, "ready": 3, "total": 3,
             "members": [{"name": leader_before, "role": "Leader", "state": "running"}]},
            {"ts_epoch_ms": fire_ms + 3000, "leader": leader_after or "",
             "timeline": tl_after, "ready": 2, "total": 3,
             "members": [{"name": leader_after or leader_before,
                          "role": "Leader", "state": "starting"}]},
            {"ts_epoch_ms": ok_again_ms + 8000, "leader": leader_after,
             "timeline": tl_after, "ready": 3, "total": 3,
             "members": [{"name": leader_after, "role": "Leader", "state": "running"}]},
        ]
    (raw / "patroni_samples.jsonl").write_text(
        "\n".join(json.dumps(s) for s in samples) + "\n")
    if pgbouncer_lines:
        (raw / "pgbouncer_pod-a.log").write_text(
            "\n".join(f"{iso(ms)} {txt}" for ms, txt in pgbouncer_lines) + "\n")
    if artifacts:
        (raw / "probe_artifacts.log").write_text(
            "\n".join(f"{iso(ms)} port-forward restart" for ms in artifacts) + "\n")


def test_parse_iso_ms_tolerates_rfc3339nano_and_space():
    assert parse_iso_ms("2026-07-07T12:00:00.123456789Z") == \
        parse_iso_ms("2026-07-07T12:00:00.123456Z")
    assert parse_iso_ms("2026-07-07 12:00:00.123+00:00") is not None
    assert parse_iso_ms("garbage") is None


def test_election_classified_by_leader_name(tmp_path):
    """Case A: leader changes, TL bumps -> flip YES; downtime ~4.6s."""
    fire = BASE_MS + 100000
    write_fixture(tmp_path / "r", scenario="switchover",
                  leader_before="pod-a", leader_after="pod-b",
                  tl_before=5, tl_after=6,
                  fire_ms=fire, fail_from_ms=fire + 400,
                  ok_again_ms=fire + 5000)
    s = stitch(tmp_path / "r")
    assert s.classification["flip"] is True
    assert s.classification["kind"] == "election"
    assert s.classification["tl_change"] is True
    dt = s.probe["client_downtime_ms"]
    assert 4000 <= dt <= 6000
    assert s.patroni["leader_after"] == "pod-b"


def test_restart_in_place_despite_ip_change(tmp_path):
    """Case B lesson: the pod IP CHANGES across the restart but the leader
    name does not -> must classify NO (restart in place). The IP heuristic
    misclassified this in the field."""
    fire = BASE_MS + 100000
    write_fixture(tmp_path / "r", scenario="pgkill",
                  leader_before="pod-a", leader_after="pod-a",
                  tl_before=5, tl_after=5,
                  fire_ms=fire, fail_from_ms=fire + 600,
                  ok_again_ms=fire + 14000,
                  addr_before="10.0.0.1", addr_after="10.0.9.9")   # IP changed!
    s = stitch(tmp_path / "r")
    assert s.classification["flip"] is False
    assert s.classification["kind"] == "restart-in-place"
    assert s.classification["tl_change"] is False
    assert "IP" in s.classification["basis"]
    assert 12000 <= s.probe["client_downtime_ms"] <= 16000


def test_backoff_tail_reported_separately(tmp_path):
    """pgBouncer keeps rejecting for ~13s AFTER the DB recovered — the tail
    must be reported separately from DB downtime."""
    fire = BASE_MS + 100000
    ok_again = fire + 12000
    write_fixture(tmp_path / "r", scenario="pgkill",
                  leader_before="pod-a", leader_after="pod-a",
                  tl_before=5, tl_after=5,
                  fire_ms=fire, fail_from_ms=fire + 500, ok_again_ms=ok_again,
                  pgbouncer_lines=[
                      (fire + 700, "pgbouncer: LOG S-0x1: closing because: "
                                   "server conn crashed? (age=42s)"),
                      (ok_again + 13000, "pgbouncer: LOG S-0x2: retrying in 15 s "
                                         "(server_login_retry)")])
    s = stitch(tmp_path / "r")
    assert s.pgbouncer["first_crash_ms"] == fire + 700
    assert abs(s.pgbouncer["backoff_tail_ms"] - 13000) < 500
    assert "negative-cache" in s.pgbouncer["note"]


def test_t7_no_dip_means_no_latch(tmp_path):
    """The T7 fix: if every post-fire sample already looks healthy (stale
    samples), full-HA recovery must NOT latch — flag it instead."""
    fire = BASE_MS + 100000
    healthy = [{"ts_epoch_ms": fire + off, "leader": "pod-a", "timeline": 5,
                "ready": 3, "total": 3,
                "members": [{"name": "pod-a", "role": "Leader", "state": "running"}]}
               for off in (-5000, 2000, 6000, 15000)]
    write_fixture(tmp_path / "r", scenario="pgkill",
                  leader_before="pod-a", leader_after="pod-a",
                  tl_before=5, tl_after=5, fire_ms=fire,
                  fail_from_ms=fire + 500, ok_again_ms=fire + 13000,
                  samples=healthy)
    s = stitch(tmp_path / "r")
    assert s.recovery["full_ha_recovery_ms"] is None
    assert "stale-sample guard" in s.recovery["note"]


def test_t7_latches_only_after_observed_dip(tmp_path):
    fire = BASE_MS + 100000
    samples = [
        {"ts_epoch_ms": fire - 4000, "leader": "pod-a", "timeline": 5,
         "ready": 3, "total": 3,
         "members": [{"name": "pod-a", "role": "Leader", "state": "running"}]},
        {"ts_epoch_ms": fire + 2000, "leader": "pod-a", "timeline": 5,
         "ready": 2, "total": 3,                      # the dip
         "members": [{"name": "pod-a", "role": "Leader", "state": "starting"}]},
        {"ts_epoch_ms": fire + 21000, "leader": "pod-a", "timeline": 5,
         "ready": 3, "total": 3,
         "members": [{"name": "pod-a", "role": "Leader", "state": "running"}]},
    ]
    write_fixture(tmp_path / "r", scenario="pgkill",
                  leader_before="pod-a", leader_after="pod-a",
                  tl_before=5, tl_after=5, fire_ms=fire,
                  fail_from_ms=fire + 500, ok_again_ms=fire + 13000,
                  samples=samples)
    s = stitch(tmp_path / "r")
    assert s.recovery["ready_dip_observed_ms"] == fire + 2000
    assert s.recovery["full_ha_recovery_ms"] == fire + 21000
    assert s.recovery["full_ha_recovery_s"] == 21.0


def test_probe_artifact_windows_excluded(tmp_path):
    """FAILs inside a port-forward-restart window are probe artifacts, not
    cluster downtime."""
    fire = BASE_MS + 100000
    run = tmp_path / "r"
    write_fixture(run, scenario="pod-delete",
                  leader_before="pod-a", leader_after="pod-a",
                  tl_before=5, tl_after=5, fire_ms=fire,
                  fail_from_ms=fire + 30000,           # only artifact-window fails
                  ok_again_ms=fire + 31000,
                  artifacts=[fire + 30200])
    s = stitch(run)
    # every FAIL fell inside the artifact window -> no real downtime measured
    assert s.probe["fail_count"] == 0
    assert s.probe["client_downtime_ms"] is None
    assert s.probe_artifacts and s.probe_artifacts[0]["label"] == "port-forward restart"


def test_stitch_run_dir_writes_artifacts(tmp_path):
    fire = BASE_MS + 100000
    run = tmp_path / "r"
    write_fixture(run, scenario="switchover",
                  leader_before="pod-a", leader_after="pod-b",
                  tl_before=5, tl_after=6, fire_ms=fire,
                  fail_from_ms=fire + 400, ok_again_ms=fire + 4600)
    stitch_run_dir(run)
    tl = (run / "TIMELINE.txt").read_text()
    assert "FLIP?      : YES — real election" in tl
    assert "FIRE: switchover" in tl
    csv_text = (run / "events.csv").read_text()
    assert csv_text.splitlines()[0] == "ts_utc,ts_epoch_ms,rel_s,source,event,detail"
    assert "first FAILed write" in csv_text
    stitched = json.loads((run / "stitched.json").read_text())
    assert stitched["classification"]["flip"] is True
    # skew between the synthetic db and local clocks is the +40ms we wrote
    assert abs(stitched["clock"]["db_minus_local_ms_median"] - 40) <= 1


def test_report_renders_for_stitched_scenario(tmp_path):
    from pgbench_harness.ops.report_ops import generate_ops_report
    fire = BASE_MS + 100000
    run = tmp_path / "r"
    write_fixture(run, scenario="pgkill",
                  leader_before="pod-a", leader_after="pod-a",
                  tl_before=5, tl_after=5, fire_ms=fire,
                  fail_from_ms=fire + 600, ok_again_ms=fire + 14000)
    stitch_run_dir(run)
    out = generate_ops_report(run)
    html = out.read_text()
    assert "restart in place" in html
    assert "uPlot" in html                    # chart lib inlined (self-contained)
    assert "probe IP is deliberately ignored" in html


# ── bug-bash regressions (stitcher) ──

def test_pre_fire_flap_does_not_inflate_downtime(tmp_path):
    """A transient FAIL during baseline (before fire) must NOT become first_fail
    — that produced a negative detection and over-counted downtime in the field."""
    fire = BASE_MS + 100000
    run = tmp_path / "r"
    raw = run / "raw"; raw.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps(
        {"op_run_id": "r", "op": "scenario", "status": "complete",
         "target": {"name": "t"}, "label": "pgkill", "params": {}, "headline": {}}))
    (raw / "fire.marker").write_text(json.dumps(
        {"ts_utc": iso(fire), "ts_epoch_ms": fire, "scenario": "pgkill",
         "target_pod": "pod-a", "leader_before": "pod-a", "tl_before": 5}))
    lines = []
    for ms in range(fire - 20000, fire + 40000, 200):
        # one baseline blip 2s BEFORE the fire, then the real outage after fire
        blip = fire - 2000 <= ms < fire - 1800
        outage = fire + 500 <= ms < fire + 8000
        lines.append(f"FAIL {iso(ms)} refused" if (blip or outage)
                     else f"OK {iso(ms)} {iso(ms+40)} f 10.0.0.1")
    (raw / "probe.log").write_text("\n".join(lines) + "\n")
    (raw / "patroni_samples.jsonl").write_text(json.dumps(
        {"ts_epoch_ms": fire + 3000, "leader": "pod-a", "timeline": 5,
         "ready": 3, "total": 3, "members": []}) + "\n")
    s = stitch(run)
    assert s.probe["detection_ms"] >= 0          # never negative
    assert s.probe["first_fail_ms"] >= fire      # anchored at/after fire
    assert 7000 <= s.probe["client_downtime_ms"] <= 9000   # the real outage only


def test_no_outage_reports_zero_not_none(tmp_path):
    """Fire happened, probe never dropped -> downtime 0 (not 'n/a')."""
    fire = BASE_MS + 100000
    run = tmp_path / "r"
    write_fixture(run, scenario="switchover", leader_before="pod-a",
                  leader_after="pod-a", tl_before=5, tl_after=5, fire_ms=fire,
                  fail_from_ms=fire + 999999, ok_again_ms=fire + 1000000)  # no fails
    s = stitch(run)
    assert s.probe["client_downtime_ms"] == 0
    assert s.probe["fail_count"] == 0
