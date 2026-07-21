"""Backup operations: preflight -> trigger (direct or operator path) -> capture.

Ports backup-impact.sh with the field lessons intact:
* LOCK PREFLIGHT: if the stanza lock is held ("backup/expire running"), ABORT
  with a clear message — the naive path "succeeds" in 0.6s with rc=50 having
  done nothing (this exact false-success happened in the field);
* schedule deconfliction is surfaced (Advanced PG defaults collide hourly),
  along with stray manual: blocks, active backup Jobs, and the
  repo1-retention-full warning state;
* samplers run every 5s to CSVs: pg_stat_archiver (query built in Python via
  psql -c — the bash sampler's empty-result quoting bug is structurally
  impossible, and a unit test asserts non-empty parse), archive queue depth
  (*.ready count — the "backup starving archiving" signal; peak is a headline
  metric), and leader+source load so leader-vs-replica impact is a
  first-class report axis;
* meta.json records backup_start/backup_end as UTC ISO + epoch-ms — the
  alignment key for correlating with a live benchmark run.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from pgbench_harness.ops.crconfig import resolve_leader
from pgbench_harness.ops.kube import Kube, KubeError, KubeResult
from pgbench_harness.ops.oprun import (EXIT_ABORTED, EXIT_FAILED, EXIT_OK,
                                       OpsRun, utc_ms)
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.util import atomic_write_text, utc_now_iso

ARCHIVER_SQL = (
    "SELECT extract(epoch from now())::bigint, archived_count, failed_count, "
    "coalesce(last_archived_wal,''), "
    "coalesce(extract(epoch from now() - last_archived_time)::bigint, -1), "
    "coalesce(last_failed_wal,''), "
    "coalesce(extract(epoch from now() - last_failed_time)::bigint, -1) "
    "FROM pg_stat_archiver")
ARCHIVER_HEADER = ("epoch_s,archived_count,failed_count,last_archived_wal,"
                   "last_archived_age_s,last_failed_wal,last_failed_age_s")

QUEUE_DEPTH_CMD = ["bash", "-c",
                   "ls /pgdata/pg*/pg_wal/archive_status/ 2>/dev/null "
                   "| grep -c '\\.ready$' || true"]

WAL_SQL = ("SELECT extract(epoch from now())::bigint, wal_bytes, wal_buffers_full "
           "FROM pg_stat_wal")


def parse_archiver_row(text: str) -> Optional[list[str]]:
    """Parse one pg_stat_archiver sample (psql -A -t -F, output) into CSV cells.

    Returns None when the output is empty/malformed — callers count misses; a
    unit test pins non-empty parsing against captured real output (the bash
    sampler regression).
    """
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not line:
        return None
    cells = line.split(",")
    if len(cells) < 7 or not cells[0].isdigit():
        return None
    return cells[:7]


def parse_pgbackrest_info_json(text: str) -> dict[str, Any]:
    """Backup inventory from ``pgbackrest info --output=json`` (best effort)."""
    out: dict[str, Any] = {"status": "", "backups": []}
    try:
        doc = json.loads(text)
        stanza = doc[0] if isinstance(doc, list) and doc else {}
        out["status"] = ((stanza.get("status") or {}).get("message")
                         or str((stanza.get("status") or {}).get("code", "")))
        for b in stanza.get("backup") or []:
            info = b.get("info") or {}
            repo = info.get("repository") or {}
            ts = b.get("timestamp") or {}
            out["backups"].append({
                "label": b.get("label", ""), "type": b.get("type", ""),
                "db_size": info.get("size"), "backup_size": info.get("delta"),
                "repo_size": repo.get("size"), "repo_backup_size": repo.get("delta"),
                "start_epoch": ts.get("start"), "stop_epoch": ts.get("stop"),
            })
    except (ValueError, TypeError, IndexError):
        pass
    return out


def lock_held(info_text: str) -> bool:
    """The field-famous check: 'ok (backup/expire running)' == lock held."""
    return "backup/expire running" in info_text


class Sampler(threading.Thread):
    """Every *interval_s*, append fn()'s CSV row(s) to parsed/<name>.csv."""

    def __init__(self, run: OpsRun, name: str, header: str, interval_s: float,
                 fn: Callable[[], Optional[list[list[str]]]]) -> None:
        super().__init__(name=f"sampler-{name}", daemon=True)
        # NB: never assign to self.run — it would shadow threading.Thread.run().
        self.path = run.run_dir / "parsed" / f"{name}.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.header = header
        self.interval_s = interval_s
        self.fn = fn
        self.stop_event = threading.Event()
        self.misses = 0

    def run_once(self) -> None:
        # The whole cycle (collect AND write) is guarded: an IO error on the
        # write must not escape and silently kill the sampler thread mid-run.
        try:
            rows = self.fn()
            if not rows:
                self.misses += 1
                return
            new_file = not self.path.exists()
            with open(self.path, "a", encoding="utf-8") as fh:
                if new_file:
                    fh.write(self.header + "\n")
                for row in rows:
                    fh.write(",".join(str(c) for c in row) + "\n")
        except Exception:  # noqa: BLE001 — a sampler must never kill the run
            self.misses += 1

    def run(self) -> None:  # noqa: A003
        while not self.stop_event.is_set():
            self.run_once()
            self.stop_event.wait(self.interval_s)

    def stop(self) -> None:
        self.stop_event.set()


def preflight(kube: Kube, run: OpsRun, spec: OpsSpec,
              exec_pod: str) -> tuple[bool, str, dict[str, Any]]:
    """Lock + deconfliction checks. Returns (clear, info_before_text, cr_backup_cfg)."""
    from pgbench_harness.ops.discover import cr_backup_config
    t = spec.target
    res = kube.exec(exec_pod, "database", ["pgbackrest", "--stanza=db", "info"],
                    timeout_s=30)
    info_text = res.stdout
    atomic_write_text(run.raw_path("pgbackrest_info_before.txt"), info_text)
    resj = kube.exec(exec_pod, "database",
                     ["pgbackrest", "--stanza=db", "info", "--output=json"],
                     timeout_s=30)
    atomic_write_text(run.raw_path("pgbackrest_info_before.json"), resj.stdout)

    # A safety rail must fail CLOSED: if the info exec itself failed (API
    # blip, pod restarting), we cannot know whether a backup is running —
    # "cannot verify" is an abort, not a pass, or we fire into a running
    # backup (the rc=50 field bug this check exists to prevent).
    if not res.ok or not info_text.strip():
        run.event("preflight", "ABORT: cannot verify stanza lock",
                  f"pgbackrest info failed (rc={res.returncode}: "
                  f"{(res.stderr or 'no output').strip()[:200]}) — refusing "
                  "to fire a backup without proof the stanza is idle. "
                  "Retry once the primary pod answers execs.")
        return False, info_text, {}

    if lock_held(info_text) or lock_held(resj.stdout):
        run.event("preflight", "ABORT: stanza lock held",
                  "a backup/expire is already running — firing now would exit "
                  "rc=50 in under a second having done nothing (field bug). "
                  "Wait for it or check the operator's schedule.")
        return False, info_text, {}

    cr = kube.cluster_cr(t.cr_kind, t.cr_name)
    bcfg = cr_backup_config(cr)
    sched_bits = []
    for entry in bcfg.get("schedules") or []:
        for stype, cronexp in (entry.get("schedules") or {}).items():
            sched_bits.append(f"{entry.get('repo', 'repo1')}/{stype}: {cronexp}")
    if sched_bits:
        run.event("preflight", "operator schedules active",
                  "; ".join(sched_bits) + " — a scheduled backup colliding with "
                  "this run fails rc=50; consider pausing schedules for the window")
    if bcfg.get("manual"):
        run.event("preflight", "stray manual: block present in CR",
                  json.dumps(bcfg["manual"])[:200])
    try:
        jobs = kube.json(["get", "jobs"]).get("items") or []
        # Only a running pgBackRest BACKUP Job conflicts — an unrelated Job
        # (or a scheduled restore/expire) in the namespace must not abort us.
        active = [j.get("metadata", {}).get("name", "?") for j in jobs
                  if (j.get("status") or {}).get("active")
                  and "pgbackrest-backup" in json.dumps(j.get("metadata", {}))]
        if active:
            run.event("preflight", "ABORT: active pgBackRest backup Job(s)",
                      ", ".join(active))
            return False, info_text, bcfg
    except KubeError:
        pass
    if "repo1-retention-full" not in json.dumps(bcfg.get("global") or {}):
        run.event("preflight", "warning: repo1-retention-full not set",
                  "repository will grow unbounded; set retention in "
                  "spec.backups.pgbackrest.global")
    run.event("preflight", "lock path clear", "no in-flight backup detected")
    return True, info_text, bcfg


def _resolve_source(kube: Kube, run: OpsRun, spec: OpsSpec) -> tuple[str, str, str]:
    """(leader_pod, source_pod, source_role) honouring --from leader|replica|<pod>."""
    frm = str(spec.params.get("source") or "leader")
    instances, leader, view = resolve_leader(kube, spec.target.cr_name)
    run.status_update(leader=leader, members=view.to_dict()["members"])
    if frm == "leader":
        return leader, leader, "leader"
    replicas = [m.name for m in view.members
                if not m.is_leader and m.name in instances]
    if frm == "replica":
        if not replicas:
            raise KubeError("no running replica available for --from replica")
        return leader, replicas[0], "replica"
    if frm not in instances:
        raise KubeError(f"requested source pod '{frm}' is not a running instance "
                        f"(have: {', '.join(instances)})")
    return leader, frm, ("leader" if frm == leader else "replica")


def run_backup(spec: OpsSpec, results_dir: Path) -> int:
    t = spec.target
    params = spec.params
    btype = str(params.get("type") or "incr")
    path_mode = str(params.get("path") or "direct")     # direct | operator
    interval_s = float(params.get("sample_interval_s", 5))
    settle_s = float(params.get("settle_s", 30))
    timeout_s = float(params.get("timeout_s", 4 * 3600))
    run = OpsRun(results_dir, "backup", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=params)
    log = run.get_logger()
    kube = Kube(context=t.context, namespace=t.namespace)
    samplers: list[Sampler] = []
    try:
        leader, source, source_role = _resolve_source(kube, run, spec)
        run.event("plan", f"{btype} backup via {path_mode} path",
                  f"source {source} ({source_role}); leader {leader}")

        # Architecture guard: a direct `kubectl exec` into an instance pod runs
        # pgBackRest against that pod's LOCAL Postgres only. On a replica that is
        # a standby (in recovery), so pgBackRest fails with "unable to find
        # primary cluster" (rc=56). A replica-offloaded backup must run on the
        # operator's repo-host Job, which can reach both primary and standby.
        if path_mode == "direct" and source_role == "replica":
            run.event("preflight", "ABORT: replica-source backup needs the operator path",
                      "In PGO, a direct exec into a replica pod can only see its "
                      "local standby Postgres (pgBackRest rc=56: 'unable to find "
                      "primary cluster'). For a replica-offloaded backup, set "
                      "Trigger path = operator and backup-standby=y in "
                      "spec.backups.pgbackrest.global; for a direct backup, set "
                      "Source = leader.")
            run.finalize("aborted", headline={"reason": "replica-direct-unsupported",
                                              "type": btype, "path": path_mode,
                                              "source": source, "source_role": source_role,
                                              "leader": leader})
            return EXIT_ABORTED

        clear, info_before, _bcfg = preflight(kube, run, spec, leader)
        if not clear:
            run.finalize("aborted", headline={"reason": "preflight",
                                              "type": btype, "path": path_mode})
            return EXIT_ABORTED

        # ── samplers (leader + source; every 5s) ──
        def archiver_rows() -> Optional[list[list[str]]]:
            res = kube.psql(leader, ARCHIVER_SQL, csv_sep=",")
            row = parse_archiver_row(res.stdout) if res.ok else None
            return [row] if row else None

        def queue_rows() -> Optional[list[list[str]]]:
            res = kube.exec(source, "database", QUEUE_DEPTH_CMD, timeout_s=15)
            val = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
            if not val.isdigit():
                return None
            return [[str(int(time.time())), val]]

        def load_rows() -> Optional[list[list[str]]]:
            rows: list[list[str]] = []
            ts = str(int(time.time()))
            wal = kube.psql(leader, WAL_SQL, csv_sep=",")
            wal_bytes = ""
            if wal.ok and wal.stdout.strip():
                cells = wal.stdout.strip().split(",")
                if len(cells) >= 2:
                    wal_bytes = cells[1]
            cpus: dict[str, str] = {}
            top = kube.run(["top", "pod"], timeout_s=15)
            if top.ok:
                for line in top.stdout.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].endswith("m"):
                        cpus[parts[0]] = parts[1][:-1]
            rows.append([ts, leader, "leader", cpus.get(leader, ""), wal_bytes])
            if source != leader:
                rows.append([ts, source, source_role, cpus.get(source, ""), ""])
            return rows

        samplers = [
            Sampler(run, "archiver", ARCHIVER_HEADER, interval_s, archiver_rows),
            Sampler(run, "queue_depth", "epoch_s,ready_files", interval_s, queue_rows),
            Sampler(run, "load", "epoch_s,pod,role,cpu_m,wal_bytes", interval_s,
                    load_rows),
        ]
        for s in samplers:
            s.start()

        # ── fire ──
        start_iso, start_ms = utc_now_iso(), utc_ms()
        run.meta["backup_start_utc"] = start_iso
        run.meta["backup_start_epoch_ms"] = start_ms
        run.save_meta()
        run.status_update(phase="backing-up", backup_type=btype, source=source)
        run.event("fire", f"backup started ({btype}, {path_mode} path)",
                  f"on {source}")

        rc: int
        trigger_out = ""
        if path_mode == "direct":
            # Replica+direct was refused in preflight, so this is always a
            # local backup against the leader's own Postgres.
            argv = ["pgbackrest", "--stanza=db", "backup", f"--type={btype}"]
            res = kube.exec(source, "pgbackrest", argv, timeout_s=timeout_s)
            if not res.ok and "unknown container" in (res.stderr or ""):
                res = kube.exec(source, "database", argv, timeout_s=timeout_s)
            rc, trigger_out = res.returncode, (res.stdout + res.stderr)
        else:
            rc, trigger_out = _operator_path(kube, run, spec, btype, timeout_s,
                                             source_role=source_role)
        atomic_write_text(run.raw_path("trigger_output.txt"), trigger_out)

        end_iso, end_ms = utc_now_iso(), utc_ms()
        run.meta["backup_end_utc"] = end_iso
        run.meta["backup_end_epoch_ms"] = end_ms
        run.save_meta()

        fail_headline = {"type": btype, "path": path_mode, "source": source,
                         "source_role": source_role, "leader": leader, "rc": rc}
        if rc == 50:
            run.event("fire", "rc=50: lock collision mid-run",
                      "a concurrent backup grabbed the stanza lock despite "
                      "preflight — see operator schedules")
            run.finalize("failed", headline=fail_headline)
            return EXIT_FAILED
        if rc != 0:
            run.event("fire", f"backup failed rc={rc}", trigger_out[-300:])
            run.finalize("failed", headline=fail_headline, error=f"backup rc={rc}")
            return EXIT_FAILED
        run.event("fire", "backup command completed",
                  f"{(end_ms - start_ms) / 1000:.1f}s")

        # ── settle window: watch the archive queue drain ──
        run.status_update(phase="settling")
        time.sleep(settle_s)

        for s in samplers:
            s.stop()
        for s in samplers:
            s.join(timeout=10)

        # ── after-info + headline ──
        resj = kube.exec(leader, "database",
                         ["pgbackrest", "--stanza=db", "info", "--output=json"],
                         timeout_s=30)
        atomic_write_text(run.raw_path("pgbackrest_info_after.json"), resj.stdout)
        res_txt = kube.exec(leader, "database",
                            ["pgbackrest", "--stanza=db", "info"], timeout_s=30)
        atomic_write_text(run.raw_path("pgbackrest_info_after.txt"), res_txt.stdout)

        before = {b["label"] for b in
                  parse_pgbackrest_info_json(
                      (run.raw_path("pgbackrest_info_before.json"))
                      .read_text(encoding="utf-8")).get("backups", [])}
        after = parse_pgbackrest_info_json(resj.stdout)
        new = [b for b in after.get("backups", []) if b["label"] not in before]
        newest = new[-1] if new else None

        peak_queue = _peak_queue(run.run_dir / "parsed" / "queue_depth.csv")
        headline: dict[str, Any] = {
            "type": btype, "path": path_mode, "source": source,
            "source_role": source_role, "leader": leader,
            "duration_s": round((end_ms - start_ms) / 1000, 1),
            "peak_archive_queue": peak_queue,
            "backup_start_epoch_ms": start_ms, "backup_end_epoch_ms": end_ms,
        }
        if params.get("linked_run_id"):
            headline["linked_run_id"] = params["linked_run_id"]
        if newest:
            headline.update({"label": newest["label"],
                             "db_size": newest.get("db_size"),
                             "backup_size": newest.get("backup_size"),
                             "repo_size": newest.get("repo_backup_size")})
            run.event("result", f"backup {newest['label']} registered",
                      f"db {newest.get('db_size')} B, repo "
                      f"{newest.get('repo_backup_size')} B")
        else:
            run.event("result", "backup finished but no new label in repo info",
                      "check trigger_output.txt")
        if peak_queue is not None:
            run.event("result", f"peak archive queue: {peak_queue} .ready files",
                      "healthy runs peaked 7-32; ~475 preceded an error-82 "
                      "WAL-archive-timeout in the field")
        run.finalize("complete", headline=headline)
        return EXIT_OK
    except KubeError as exc:
        log.error("backup failed: %s", exc)
        run.finalize("failed", error=str(exc)[:500])
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — the run dir must ALWAYS reach a
        # terminal state; a bare exception (bad param, parse error, disk) must
        # not leave meta.json stuck at "running" forever.
        log.exception("backup crashed")
        run.finalize("failed", error=f"internal error: {str(exc)[:300]}")
        return EXIT_FAILED
    finally:
        for s in samplers:
            s.stop()


def _operator_path(kube: Kube, run: OpsRun, spec: OpsSpec, btype: str,
                   timeout_s: float, source_role: str = "leader") -> tuple[int, str]:
    """Operator path: set the CR manual: block, annotate, track the Job.

    Different lock/scheduling interactions than direct exec — which path ran
    is recorded in meta/report (they are not interchangeable evidence).

    A replica source adds ``--backup-standby=y`` to the manual options — a
    per-backup override, so this one backup copies files from the standby
    (primary only coordinates start/stop + WAL switch) WITHOUT changing the
    CR's global map. Scheduled backups can't carry extra options, so making
    ALL backups standby-sourced still requires global backup-standby: "y".
    """
    t = spec.target

    def _backup_jobs() -> list[dict[str, Any]]:
        jobs = kube.json(["get", "jobs"]).get("items") or []
        return [j for j in jobs
                if "pgbackrest-backup" in json.dumps(j.get("metadata", {}))]

    # Snapshot the backup Jobs that ALREADY exist, so a leftover completed/failed
    # Job from a prior run can't be mistaken for this one (the field false-success
    # class: reporting done/failed in 0.1s from a stale Job before ours exists).
    pre_existing = {j.get("metadata", {}).get("name", "") for j in _backup_jobs()}

    options = [f"--type={btype}"]
    if source_role == "replica":
        options.append("--backup-standby=y")
        run.event("fire", "backup-standby requested for this backup",
                  "one-off --backup-standby=y in manual options; requires a "
                  "healthy streaming replica or pgBackRest exits rc=56")
    patch = {"spec": {"backups": {"pgbackrest": {"manual": {
        "repoName": "repo1", "options": options}}}}}
    kube.run(["patch", t.cr_kind, t.cr_name, "--type", "merge",
              "-p", json.dumps(patch)], check=True)
    stamp = utc_now_iso().replace(":", "-")
    anno = ("postgres-operator.crunchydata.com/pgbackrest-backup"
            if t.cr_kind == "postgrescluster"
            else "pgv2.percona.com/pgbackrest-backup")
    kube.run(["annotate", t.cr_kind, t.cr_name, f"{anno}={stamp}",
              "--overwrite"], check=True)
    run.event("fire", "manual backup annotated", f"{anno}={stamp}")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # Only Jobs created for THIS run (not present before we annotated) —
        # this is what prevents a stale prior-run Job from being read as ours.
        new_jobs = [j for j in _backup_jobs()
                    if j.get("metadata", {}).get("name", "") not in pre_existing]
        failed = [j for j in new_jobs if (j.get("status") or {}).get("failed")]
        done = [j for j in new_jobs if (j.get("status") or {}).get("succeeded")]
        if failed:
            return 1, f"backup Job failed: {failed[0]['metadata']['name']}"
        if done:
            return 0, f"backup Job succeeded: {done[0]['metadata']['name']}"
        time.sleep(2)
    return 1, "timeout waiting for the operator's backup Job"


def _peak_queue(path: Path) -> Optional[int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[1:]
        vals = [int(ln.split(",")[1]) for ln in lines if "," in ln]
        return max(vals) if vals else None
    except (OSError, ValueError, IndexError):
        return None
