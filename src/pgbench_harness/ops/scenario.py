"""Failover scenarios: capture streams start -> baseline -> FIRE -> settle ->
stitch -> report. One command, fully captured, like failover-probe.sh.

Field lessons preserved:
* the write probe goes THROUGH pgBouncer (the customer path), runs a separate
  write plus ``SELECT clock_timestamp(), pg_is_in_recovery(),
  inet_server_addr()`` with ``-q`` — never ``INSERT ... RETURNING`` (its
  command tag corrupted address parsing in the field);
* probe milliseconds come from ``clock_timestamp()`` in-database; the local
  timestamp on each line is only the shared axis for window math (and the
  skew between the two is measured, not assumed);
* ``fire.marker`` records the exact trigger instant + the leader name/TL
  before — the stitcher's classification anchor;
* every log stream reattaches when its pod dies (the pod dying IS the event
  under test); port-forward restarts are logged as probe artifacts so they
  are never counted as cluster downtime;
* firing refuses to proceed while a pgBackRest lock is held (shared preflight
  with backup ops).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO, Optional

from pgbench_harness.ops import patroni
from pgbench_harness.ops.backup import lock_held
from pgbench_harness.ops.crconfig import resolve_leader
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.oprun import (EXIT_ABORTED, EXIT_FAILED, EXIT_OK,
                                       OpsRun, utc_ms)
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.util import get_redactor, utc_now_iso

PROBE_WRITE_SQL = ("CREATE TABLE IF NOT EXISTS pgb_ops_probe(id int PRIMARY KEY, "
                   "ts timestamptz); INSERT INTO pgb_ops_probe VALUES (1, now()) "
                   "ON CONFLICT (id) DO UPDATE SET ts = excluded.ts")
PROBE_SELECT_SQL = ("SELECT clock_timestamp() || '|' || pg_is_in_recovery() || '|' "
                    "|| coalesce(inet_server_addr()::text, '-')")

CASES = {
    "switchover": "graceful switchover (patronictl switchover --force)",
    "pgkill": "postgres crash: kill -9 the postmaster on the leader",
    "pod-delete": "force-delete the leader pod (--grace-period=0)",
    "node-loss": "EXPERIMENTAL: cordon + delete the leader's node",
}


def _now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat()


def _libpq_quote(value: str) -> str:
    """Quote a libpq conninfo value so spaces/specials in a target's db name,
    user, or host can't inject extra keywords (e.g. downgrade sslmode)."""
    v = str(value)
    if v == "" or any(c in v for c in " '\\"):
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return v


class ProbeThread(threading.Thread):
    """~5 Hz write probe through pgBouncer, logging OK/FAIL lines."""

    def __init__(self, run: OpsRun, conninfo: str, env: dict[str, str],
                 hz: float) -> None:
        super().__init__(name="probe", daemon=True)
        self.path = run.run_dir / "raw" / "probe.log"
        self.conninfo = conninfo
        self.env = env
        self.period = 1.0 / max(0.5, hz)
        self.stop_event = threading.Event()
        self.consecutive_ok = 0

    def tick(self) -> None:
        argv = ["psql", self.conninfo, "-X", "-q", "-A", "-t",
                "-c", PROBE_WRITE_SQL, "-c", PROBE_SELECT_SQL]
        local = _now_iso_ms()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=3,
                                  env=self.env)
        except subprocess.TimeoutExpired:
            self._log(f"FAIL {local} timeout")
            self.consecutive_ok = 0
            return
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.strip().splitlines()[-1].split("|")
            db_ts = parts[0] if parts else ""
            rec = parts[1] if len(parts) > 1 else ""
            addr = parts[2] if len(parts) > 2 else ""
            self._log(f"OK {local} {db_ts.replace(' ', 'T')} {rec} {addr}")
            self.consecutive_ok += 1
        else:
            reason = (proc.stderr or proc.stdout).strip().splitlines()
            self._log(f"FAIL {local} {reason[0][:160] if reason else 'no output'}")
            self.consecutive_ok = 0

    def _log(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def run(self) -> None:  # noqa: A003
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.tick()
            delay = self.period - (time.monotonic() - started)
            if delay > 0:
                self.stop_event.wait(delay)

    def stop(self) -> None:
        self.stop_event.set()


class LogStream(threading.Thread):
    """kubectl logs -f for one pod/container, auto-reattaching when the pod
    dies (log streams die with the pod they follow — which is the event
    under test). Reattach markers land in the raw file itself."""

    def __init__(self, kube: Kube, run: OpsRun, pod: str, container: str,
                 out_name: str) -> None:
        super().__init__(name=f"logs-{pod}", daemon=True)
        self.kube = kube
        self.pod = pod
        self.container = container
        self.path = run.run_dir / "raw" / out_name
        self.stop_event = threading.Event()
        self.proc: Optional[subprocess.Popen] = None

    def run(self) -> None:  # noqa: A003
        attach = 0
        while not self.stop_event.is_set():
            attach += 1
            with open(self.path, "a", encoding="utf-8") as fh:
                if attach > 1:
                    fh.write(f"{_now_iso_ms()} [capture] reattach #{attach}\n")
                    fh.flush()
                try:
                    self.proc = self.kube.stream(
                        ["logs", "-f", "--since=1s", "--timestamps",
                         self.pod, "-c", self.container], stdout=fh)
                except OSError:
                    self.stop_event.wait(2)
                    continue
                while self.proc.poll() is None and not self.stop_event.is_set():
                    time.sleep(0.3)
                if self.proc.poll() is None:
                    self.proc.terminate()
            if not self.stop_event.is_set():
                self.stop_event.wait(1.5)     # pod may be coming back

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass


class ClusterWatch(threading.Thread):
    """1s poller: pod phases/ready -> pods_watch.log; every other tick a
    patronictl sample -> patroni_samples.jsonl; live status.json updates."""

    def __init__(self, kube: Kube, run: OpsRun, cr_name: str,
                 expected_instances: int) -> None:
        super().__init__(name="cluster-watch", daemon=True)
        self.kube = kube
        self.run_ref = run
        self.cr_name = cr_name
        self.expected = expected_instances
        self.stop_event = threading.Event()
        self.pods_path = run.run_dir / "raw" / "pods_watch.log"
        self.samples_path = run.run_dir / "raw" / "patroni_samples.jsonl"
        self._tick_n = 0

    def _pods(self) -> tuple[list[dict[str, Any]], int]:
        from pgbench_harness.ops.discover import classify_pods
        items = self.kube.json(["get", "pods"]).get("items") or []
        buckets = classify_pods(items, self.cr_name)
        ready = 0
        with open(self.pods_path, "a", encoding="utf-8") as fh:
            for p in buckets["instances"]:
                fh.write(f"{_now_iso_ms()} {p['name']} {p['phase']} {p['ready']}\n")
                num, den = (p["ready"].split("/") + ["0"])[:2]
                if p["phase"] == "Running" and num == den and num != "0":
                    ready += 1
        return buckets["instances"], ready

    def tick(self) -> None:
        self._tick_n += 1
        try:
            instances, ready = self._pods()
        except KubeError:
            return
        sample: dict[str, Any] = {"ts_epoch_ms": utc_ms(), "ready": ready,
                                  "total": self.expected}
        running = [p["name"] for p in instances if p["phase"] == "Running"]
        if running:
            try:
                view = patroni.fetch_view(self.kube, running[0], timeout_s=10)
                sample.update({"leader": view.leader_name,
                               "timeline": view.timeline,
                               "members": [{"name": m.name, "role": m.role,
                                            "state": m.state}
                                           for m in view.members]})
            except KubeError:
                sample.update({"leader": "", "timeline": None, "members": []})
        with open(self.samples_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample) + "\n")
        self.run_ref.status_update(
            leader=sample.get("leader", ""), timeline=sample.get("timeline"),
            ready=f"{ready}/{self.expected}", members=sample.get("members", []))

    def run(self) -> None:  # noqa: A003
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(1.0)

    def stop(self) -> None:
        self.stop_event.set()

    def last_sample(self) -> Optional[dict[str, Any]]:
        try:
            lines = self.samples_path.read_text(encoding="utf-8").splitlines()
            return json.loads(lines[-1]) if lines else None
        except (OSError, ValueError):
            return None


class PortForward(threading.Thread):
    """kubectl port-forward supervisor; restarts logged as probe artifacts."""

    def __init__(self, kube: Kube, run: OpsRun, svc: str, local_port: int) -> None:
        super().__init__(name="port-forward", daemon=True)
        self.kube = kube
        self.run_ref = run
        self.svc = svc
        self.local_port = local_port
        self.stop_event = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.artifacts = run.run_dir / "raw" / "probe_artifacts.log"

    def run(self) -> None:  # noqa: A003
        first = True
        while not self.stop_event.is_set():
            if not first:
                with open(self.artifacts, "a", encoding="utf-8") as fh:
                    fh.write(f"{_now_iso_ms()} port-forward restart\n")
            first = False
            with open(self.run_ref.run_dir / "raw" / "port_forward.log", "a",
                      encoding="utf-8") as fh:
                try:
                    self.proc = self.kube.stream(
                        ["port-forward", f"svc/{self.svc}",
                         f"{self.local_port}:5432"], stdout=fh)
                except OSError:
                    self.stop_event.wait(2)
                    continue
                while self.proc.poll() is None and not self.stop_event.is_set():
                    time.sleep(0.3)
                if self.proc.poll() is None:
                    self.proc.terminate()
            self.stop_event.wait(1.0)

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass


def _fire(kube: Kube, run: OpsRun, spec: OpsSpec, case: str, leader: str,
          leader_node: str) -> None:
    t = spec.target
    if case == "switchover":
        kube.exec(leader, "database",
                  ["patronictl", "switchover", t.patroni_scope, "--force"],
                  timeout_s=60, check=True)
    elif case == "pgkill":
        kube.exec(leader, "database",
                  ["bash", "-c", "kill -9 $(head -1 /pgdata/pg*/postmaster.pid)"],
                  timeout_s=30, check=True)
    elif case == "pod-delete":
        kube.run(["delete", "pod", leader, "--grace-period=0", "--force"],
                 timeout_s=60, check=True)
    elif case == "node-loss":
        run.event("warning", "node-loss is EXPERIMENTAL",
                  "least-characterized path; validate against a disposable cluster")
        kube.run(["cordon", leader_node], namespaced=False, timeout_s=60)
        kube.run(["delete", "node", leader_node], namespaced=False,
                 timeout_s=120, check=True)
    else:
        raise KubeError(f"unknown scenario case '{case}'")


def run_scenario(spec: OpsSpec, results_dir: Path) -> int:
    t = spec.target
    params = spec.params
    case = str(params.get("case"))
    baseline_s = float(params.get("baseline_s", 30))
    settle_s = float(params.get("settle_s", 180))
    hold_s = float(params.get("recovery_hold_s", 10))
    probe_cfg = dict(params.get("probe") or {})
    run = OpsRun(results_dir, "scenario", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=params)
    log = run.get_logger()
    kube = Kube(context=t.context, namespace=t.namespace)
    threads: list[Any] = []
    events_proc: Optional[subprocess.Popen] = None
    events_fh: Optional[IO[str]] = None
    try:
        instances, leader, view = resolve_leader(kube, t.cr_name)
        leader_pod_doc = kube.json(["get", "pod", leader])
        leader_node = (leader_pod_doc.get("spec") or {}).get("nodeName", "")
        run.event("plan", f"scenario {case}: {CASES.get(case, '?')}",
                  f"leader {leader} (node {leader_node}), TL {view.timeline}, "
                  f"{len(instances)} instances")

        # Safety rail: never fire while a backup holds the stanza lock.
        # Fails CLOSED — an exec failure means "cannot verify", not "clear".
        info = kube.exec(leader, "database",
                         ["pgbackrest", "--stanza=db", "info"], timeout_s=30)
        if not info.ok or not info.stdout.strip():
            run.event("preflight", "ABORT: cannot verify pgBackRest lock",
                      f"pgbackrest info failed (rc={info.rc}) — refusing to "
                      "fire a failover without proof no backup is in flight")
            run.finalize("aborted", headline={"case": case,
                                              "reason": "lock unverifiable"})
            return EXIT_ABORTED
        if lock_held(info.stdout):
            run.event("preflight", "ABORT: pgBackRest lock held",
                      "a backup/expire is in flight on this target — firing a "
                      "failover now would corrupt both experiments")
            run.finalize("aborted", headline={"case": case, "reason": "backup lock"})
            return EXIT_ABORTED

        # ── capture streams ──
        from pgbench_harness.ops.discover import classify_pods
        pods = kube.json(["get", "pods"]).get("items") or []
        buckets = classify_pods(pods, t.cr_name)
        for p in buckets["instances"]:
            threads.append(LogStream(kube, run, p["name"], "database",
                                     f"patroni_{p['name']}.log"))
        for p in buckets["pgbouncer"]:
            threads.append(LogStream(kube, run, p["name"], "pgbouncer",
                                     f"pgbouncer_{p['name']}.log"))
        events_fh = open(run.raw_path("events_watch.log"), "a", encoding="utf-8")
        events_proc = kube.stream(["get", "events", "-w"], stdout=events_fh)
        watch = ClusterWatch(kube, run, t.cr_name, len(buckets["instances"]))
        threads.append(watch)

        probe: Optional[ProbeThread] = None
        pf: Optional[PortForward] = None
        probe_mode = str(probe_cfg.get("mode") or "port-forward")
        if probe_mode != "off":
            pw = ""
            try:
                pw = kube.get_secret_value(t.pguser_secret_name, t.pguser_secret_key)
                get_redactor().register(pw)
            except KubeError as exc:
                run.event("probe", "probe disabled: cannot read pguser secret",
                          str(exc)[:200])
                probe_mode = "off"
            if probe_mode == "port-forward":
                local_port = int(probe_cfg.get("local_port", 15432))
                pf = PortForward(kube, run, f"{t.cr_name}-pgbouncer", local_port)
                threads.append(pf)
                host, port = "127.0.0.1", local_port
            else:
                host = str(probe_cfg.get("host") or f"{t.cr_name}-pgbouncer")
                port = int(probe_cfg.get("port", 5432))
            if probe_mode != "off":
                conninfo = " ".join(f"{k}={_libpq_quote(v)}" for k, v in (
                    ("host", host), ("port", str(port)), ("user", t.db_user),
                    ("dbname", t.db_name),
                    ("sslmode", str(probe_cfg.get("sslmode", "require"))),
                    ("connect_timeout", "2")))
                env = dict(os.environ)
                env["PGPASSWORD"] = pw          # child env only, never argv/logs
                probe = ProbeThread(run, conninfo, env,
                                    float(probe_cfg.get("hz", 5)))
                threads.append(probe)

        for th in threads:
            th.start()
        run.event("capture", f"{len(threads)} capture streams started",
                  f"probe={probe_mode}")

        # ── baseline ──
        run.status_update(phase="baseline")
        time.sleep(baseline_s)

        # ── FIRE ──
        # Re-resolve the leader at the last instant (it may have moved during
        # baseline) and stamp the authoritative before-state into the marker.
        instances, leader, view = resolve_leader(kube, t.cr_name)
        marker = {"ts_utc": utc_now_iso(), "ts_epoch_ms": utc_ms(),
                  "scenario": case, "target_pod": leader,
                  "leader_before": leader, "tl_before": view.timeline}
        with open(run.raw_path("fire.marker"), "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
        run.event("fire", f"FIRE: {case}", f"target {leader}")
        run.status_update(phase="fired", fired_at_ms=marker["ts_epoch_ms"])
        _fire(kube, run, spec, case, leader, leader_node)

        # ── settle (early exit once recovered and held) ──
        run.status_update(phase="settling")
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            time.sleep(1)
            if probe is not None:
                held = probe.consecutive_ok >= hold_s * float(probe_cfg.get("hz", 5))
            else:
                held = True
            sample = watch.last_sample()
            healthy = bool(sample and sample.get("leader")
                           and sample.get("ready", 0) >= sample.get("total", 1))
            fired_for = time.monotonic() - (deadline - settle_s)
            if held and healthy and fired_for >= max(hold_s, 10):
                run.event("settle", "recovered and held — ending settle early",
                          f"after {fired_for:.0f}s")
                break

        # ── stop captures ──
        for th in threads:
            th.stop()
        _stop_events(events_proc, events_fh)
        events_proc, events_fh = None, None
        for th in threads:
            th.join(timeout=8)
        run.event("capture", "capture streams stopped", "")

        # ── stitch + report ──
        # Stitching is DERIVED data over captures already safe on disk: a
        # stitcher bug must downgrade to a warning, never flip a successful
        # failover run to 'failed' (same contract as report generation).
        headline: dict[str, Any] = {"case": case,
                                    "fire_epoch_ms": marker["ts_epoch_ms"]}
        try:
            from pgbench_harness.ops.stitch import stitch_run_dir
            stitched = stitch_run_dir(run.run_dir)
            cls = stitched.classification
            headline.update({
                "downtime_ms": stitched.probe.get("client_downtime_ms"),
                "detection_ms": stitched.probe.get("detection_ms"),
                "flip": cls.get("flip"), "kind": cls.get("kind"),
                "leader_before": stitched.patroni.get("leader_before"),
                "leader_after": stitched.patroni.get("leader_after"),
                "tl_before": stitched.patroni.get("tl_before"),
                "tl_after": stitched.patroni.get("tl_after"),
                "backoff_tail_ms": stitched.pgbouncer.get("backoff_tail_ms"),
                "full_ha_recovery_s": stitched.recovery.get("full_ha_recovery_s"),
            })
            run.event("stitch", "timeline stitched",
                      f"downtime {headline['downtime_ms']} ms, "
                      f"flip={headline['flip']} ({headline['kind']})")
        except Exception as exc:  # noqa: BLE001
            log.warning("stitch failed: %s — captures are intact on disk", exc)
            run.event("stitch", "stitch failed (derived data)",
                      f"{str(exc)[:300]} — raw captures are intact; re-run "
                      "stitching after a harness update")
        if params.get("linked_run_id"):
            headline["linked_run_id"] = params["linked_run_id"]
        # Finalize BEFORE rendering: the report reads meta.json from disk, so
        # the headline/status must be terminal or the KPI tiles render empty.
        run.finalize("complete", headline=headline)
        try:
            from pgbench_harness.ops.report_ops import generate_ops_report
            generate_ops_report(run.run_dir)
        except Exception as exc:  # noqa: BLE001 — report is derived, never fatal
            log.warning("report generation failed: %s", exc)
        return EXIT_OK
    except KubeError as exc:
        log.error("scenario failed: %s", exc)
        run.finalize("failed", error=str(exc)[:500])
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — never leave the run stuck 'running'
        log.exception("scenario crashed")
        run.finalize("failed", error=f"internal error: {str(exc)[:300]}")
        return EXIT_FAILED
    finally:
        # Reap every capture child even on the error path: the events watcher
        # (kubectl get events -w) is owned by no thread and would otherwise leak.
        for th in threads:
            try:
                th.stop()
            except Exception:  # noqa: BLE001
                pass
        _stop_events(events_proc, events_fh)


def _stop_events(proc: "Optional[subprocess.Popen]", fh: "Optional[IO[str]]") -> None:
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass
