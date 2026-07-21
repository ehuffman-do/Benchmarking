"""Device-level IOPS ground truth for cluster-aware runs.

TPS cannot distinguish a 10K-throttled volume from a 40K one; the block
device counters can, against the published limits. This module:

* resolves the block device backing /pgdata inside the primary instance pod
  (``/proc/diskstats`` is not container-namespaced — the pod sees the node's
  device counters; the maj:min comes from ``/proc/self/mountinfo``);
* streams 1s counter snapshots to ``raw/diskstats.log`` for the whole load
  window (raw snapshots are the source of truth, derivation happens in
  summarize, per repo convention);
* derives reads/s, writes/s, IOPS, MB/s, await, utilization and queue depth
  into ``parsed/device_io.csv``;
* captures the storage identity (PVC -> PV -> StorageClass -> placement)
  into ``env/storage_identity.json``;
* computes the capped / exceeds / inconclusive verdict against the spec's
  reference limits.

Everything here degrades to a recorded warning — a benchmark must never fail
because observation broke.
"""

from __future__ import annotations

import csv
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.spec import Limits, Spec
from pgbench_harness.util import atomic_write_text, utc_now_iso

SECTOR_BYTES = 512
DEVICE_CSV_COLUMNS = ("t_epoch_ms", "reads_s", "writes_s", "iops",
                      "read_mb_s", "write_mb_s", "await_ms", "util_pct",
                      "queue_depth")
SUSTAIN_WINDOW_S = 10

# Parameter/annotation fragments that mark a non-default IOPS/QoS class.
HIGH_IOPS_MARKERS = ("iops", "throughput", "qos", "performance", "burst")


def _kube(spec: Spec):
    from pgbench_harness.ops.kube import Kube
    assert spec.cluster is not None
    return Kube(context=spec.cluster.context, namespace=spec.cluster.namespace)


def _primary_pod(spec: Spec, kube: Any) -> str:
    from pgbench_harness.ops.discover import resolve_leader_resilient
    _instances, leader, _view, _attempts = resolve_leader_resilient(
        kube, spec.cluster.cr_name, timeout_s=45, poll_s=3)
    return leader


def resolve_pgdata_device(kube: Any, pod: str) -> dict[str, Any]:
    """maj:min + name of the device mounted at /pgdata inside the pod."""
    res = kube.exec(pod, "database", ["cat", "/proc/self/mountinfo"],
                    timeout_s=20, check=True)
    for line in res.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[4] == "/pgdata":
            majmin = fields[2]
            break
    else:
        raise RuntimeError("no /pgdata mount found in /proc/self/mountinfo")
    res = kube.exec(pod, "database", ["cat", "/proc/diskstats"],
                    timeout_s=20, check=True)
    name = ""
    for line in res.stdout.splitlines():
        f = line.split()
        if len(f) >= 14 and f"{f[0]}:{f[1]}" == majmin:
            name = f[2]
            break
    return {"majmin": majmin, "device": name, "pod": pod,
            "resolved_utc": utc_now_iso()}


class DeviceIoSampler:
    """1s /proc/diskstats stream from the primary pod into raw/diskstats.log.

    One long-lived `kubectl exec` (not one exec per second): the in-pod shell
    loop prints an epoch-ms stamp, the full diskstats table, and a separator
    each second. stop() is always safe; a mid-run death is recorded as a
    warning in env/device_io_warning.txt, never raised.
    """

    RESPAWN_BACKOFF_S = 5.0
    MAX_RESPAWN_WARNINGS = 10

    def __init__(self, spec: Spec, run_dir: Path, logger=None) -> None:
        self.spec = spec
        self.run_dir = run_dir
        self.logger = logger
        self._proc: Optional[subprocess.Popen] = None
        self._fh = None
        self._watch: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self._respawns = 0
        self.device: dict[str, Any] = {}

    def _warn(self, msg: str) -> None:
        if self.logger:
            self.logger.warning("device-io sampler: %s", msg)
        wpath = self.run_dir / "env" / "device_io_warning.txt"
        wpath.parent.mkdir(parents=True, exist_ok=True)
        with open(wpath, "a", encoding="utf-8") as fh:
            fh.write(f"{utc_now_iso()} {msg}\n")

    def _spawn(self) -> None:
        """One stream incarnation. The in-pod loop prints only the ONE device
        line (not the whole diskstats table): a week-long run writes megabytes
        instead of gigabytes, and the raw truth for the sampled device is
        preserved unchanged."""
        kube = _kube(self.spec)
        pod = _primary_pod(self.spec, kube)
        if not self.device:
            self.device = resolve_pgdata_device(kube, pod)
            atomic_write_text(self.run_dir / "raw" / "diskstats_device.json",
                              json.dumps(self.device, indent=1))
        maj, minor = str(self.device["majmin"]).split(":")
        loop = ("while true; do date +%s%3N; "
                f"awk '$1 == {maj} && $2 == {minor}' /proc/diskstats; "
                "echo ===; sleep 1; done")
        if self._fh is not None:          # respawn: don't leak the old handle
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = open(self.run_dir / "raw" / "diskstats.log", "a",
                        encoding="utf-8", errors="replace")
        self._proc = kube.stream(
            ["exec", pod, "-c", "database", "--", "sh", "-c", loop],
            stdout=self._fh, stderr=subprocess.DEVNULL)
        if self.logger:
            self.logger.info("device-io sampler on %s (%s %s)", pod,
                             self.device.get("device"),
                             self.device.get("majmin"))

    def start(self) -> bool:
        try:
            self._spawn()
        except Exception as exc:  # noqa: BLE001 — observation must not kill the run
            self._warn(f"failed to start: {exc}")
            self.stop()
            return False
        self._watch = threading.Thread(target=self._supervise, daemon=True)
        self._watch.start()
        return True

    def _supervise(self) -> None:
        """Keep the stream alive for the whole run: exec streams DIE over long
        windows (token refresh, pod restart, network blips, failover moving
        the primary). Respawn with backoff until stop(); the derivation
        already treats gaps as gaps, never as fabricated rates."""
        while not self._stopped.is_set():
            proc = self._proc
            if proc is None:
                return
            proc.wait()
            if self._stopped.is_set():
                return
            self._respawns += 1
            if self._respawns <= self.MAX_RESPAWN_WARNINGS:
                self._warn(f"sampling stream died (rc={proc.returncode}) — "
                           f"respawn #{self._respawns} after "
                           f"{self.RESPAWN_BACKOFF_S:.0f}s; the gap is visible "
                           "in the series, not interpolated")
            if self._stopped.wait(self.RESPAWN_BACKOFF_S):
                return
            try:
                # the primary may have MOVED — re-resolve pod (device identity
                # is re-checked: a new maj:min would need a fresh resolution)
                self._spawn()
            except Exception as exc:  # noqa: BLE001
                if self._respawns <= self.MAX_RESPAWN_WARNINGS:
                    self._warn(f"respawn failed: {exc} — retrying")
            # stop() may have run while _spawn() was blocked resolving the
            # leader (up to ~45s) — it terminated the OLD dead proc and gave
            # up joining; without this check the fresh stream would outlive
            # the run and keep appending to raw/diskstats.log forever
            if self._stopped.is_set():
                self._kill_proc()
                return

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                pass
        self._proc = None

    def stop(self) -> None:
        self._stopped.set()
        self._kill_proc()
        if self._watch is not None:
            self._watch.join(timeout=5)
            self._kill_proc()          # a respawn may have landed mid-join
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def capture_storage_identity(spec: Spec, run_dir: Path, logger=None) -> dict[str, Any]:
    """PVC -> PV -> StorageClass -> placement, recorded as config evidence.

    "Config shows no high-IOPS marker" is itself evidence, so identical-to-
    standard output is recorded just like a positive marker."""
    out: dict[str, Any] = {"collected_utc": utc_now_iso(), "warnings": [],
                           "high_iops_markers": []}
    try:
        kube = _kube(spec)
        pod = _primary_pod(spec, kube)
        raw = kube.json(["get", "pod", pod])
        out["pod"] = pod
        out["node"] = (raw.get("spec") or {}).get("nodeName", "")
        pvc_name = ""
        for vol in (raw.get("spec") or {}).get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName", "")
            if claim and vol.get("name") in ("postgres-data", "pgdata") or \
                    (claim and "pgdata" in claim):
                pvc_name = claim
                break
        if not pvc_name:
            for vol in (raw.get("spec") or {}).get("volumes") or []:
                claim = (vol.get("persistentVolumeClaim") or {}).get("claimName", "")
                if claim:
                    pvc_name = claim
                    break
        if not pvc_name:
            raise RuntimeError(f"no PVC volume on pod {pod}")
        pvc = kube.json(["get", "pvc", pvc_name])
        sc_name = str((pvc.get("spec") or {}).get("storageClassName", ""))
        pv_name = str((pvc.get("spec") or {}).get("volumeName", ""))
        out["pvc"] = {"name": pvc_name, "storage_class": sc_name,
                      "capacity": ((pvc.get("status") or {}).get("capacity")
                                   or {}).get("storage", ""),
                      "volume_name": pv_name,
                      "annotations": (pvc.get("metadata") or {}).get(
                          "annotations", {}) or {}}
        if pv_name:
            pv = kube.json(["get", "pv", pv_name], namespaced=False)
            csi = ((pv.get("spec") or {}).get("csi") or {})
            out["pv"] = {"name": pv_name,
                         "provisioner": csi.get("driver", ""),
                         "volume_id": csi.get("volumeHandle", ""),
                         "volume_attributes": csi.get("volumeAttributes", {}) or {},
                         "capacity": ((pv.get("spec") or {}).get("capacity")
                                      or {}).get("storage", "")}
        if sc_name:
            sc = kube.json(["get", "storageclass", sc_name], namespaced=False)
            out["storage_class"] = {
                "name": sc_name,
                "provisioner": sc.get("provisioner", ""),
                "parameters": sc.get("parameters", {}) or {},
                "annotations": (sc.get("metadata") or {}).get("annotations",
                                                              {}) or {}}
        # surface anything that smells like a non-default IOPS/QoS knob
        hay: list[tuple[str, dict]] = [
            ("storage_class.parameters",
             (out.get("storage_class") or {}).get("parameters", {})),
            ("storage_class.annotations",
             (out.get("storage_class") or {}).get("annotations", {})),
            ("pvc.annotations", (out.get("pvc") or {}).get("annotations", {})),
            ("pv.volume_attributes",
             (out.get("pv") or {}).get("volume_attributes", {})),
        ]
        for where, d in hay:
            for k, v in (d or {}).items():
                if any(m in k.lower() for m in HIGH_IOPS_MARKERS):
                    out["high_iops_markers"].append(f"{where}.{k}={v}")
        if not out["high_iops_markers"]:
            out["finding"] = ("config shows no high-IOPS marker on the PVC, PV, "
                              "or StorageClass — indistinguishable from a "
                              "standard-class volume")
        else:
            out["finding"] = ("high-IOPS/QoS markers present: "
                              + "; ".join(out["high_iops_markers"]))
    except Exception as exc:  # noqa: BLE001 — evidence capture must not kill the run
        out["warnings"].append(f"storage identity capture degraded: {exc}")
        if logger:
            logger.warning("storage identity: %s", exc)
    atomic_write_text(run_dir / "env" / "storage_identity.json",
                      json.dumps(out, indent=2))
    return out


# ── derivation (summarize-side) ──

def _parse_snapshots(log_path: Path, majmin: str) -> list[tuple[float, list[int]]]:
    """(epoch_s, first-14-diskstats-fields-as-int) per snapshot for majmin."""
    snaps: list[tuple[float, list[int]]] = []
    ts: Optional[float] = None
    row: Optional[list[int]] = None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return snaps
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "===":
            if ts is not None and row is not None:
                snaps.append((ts, row))
            ts, row = None, None
            continue
        if ts is None and line.isdigit():
            ts = int(line) / 1000.0
            continue
        f = line.split()
        if len(f) >= 14 and f"{f[0]}:{f[1]}" == majmin:
            try:
                row = [int(x) for x in f[3:14]]
            except ValueError:
                row = None
    return snaps


def derive_device_series(run_dir: Path) -> list[dict[str, float]]:
    """Counter deltas -> per-interval rates; writes parsed/device_io.csv."""
    dev_path = run_dir / "raw" / "diskstats_device.json"
    log_path = run_dir / "raw" / "diskstats.log"
    if not dev_path.exists() or not log_path.exists():
        return []
    try:
        majmin = json.loads(dev_path.read_text())["majmin"]
    except (ValueError, KeyError, OSError):
        return []
    snaps = _parse_snapshots(log_path, majmin)
    # a sampler respawn onto a clock-skewed node can regress timestamps;
    # window math downstream assumes monotonic time
    snaps.sort(key=lambda s: s[0])
    rows: list[dict[str, float]] = []
    for (t0, a), (t1, b) in zip(snaps, snaps[1:]):
        dt = t1 - t0
        if dt <= 0 or dt > 30:            # stream gap — do not fabricate a rate
            continue
        d = [max(0, y - x) for x, y in zip(a, b)]
        # diskstats fields 4..14: rd rd_mrg rd_sec rd_ms wr wr_mrg wr_sec wr_ms
        #                        io_inflight io_ms weighted_ms
        reads, wr = d[0], d[4]
        ios = reads + wr
        rows.append({
            "t_epoch_ms": round(t1 * 1000),
            "reads_s": round(reads / dt, 1),
            "writes_s": round(wr / dt, 1),
            "iops": round(ios / dt, 1),
            "read_mb_s": round(d[2] * SECTOR_BYTES / dt / 1048576, 2),
            "write_mb_s": round(d[6] * SECTOR_BYTES / dt / 1048576, 2),
            "await_ms": round((d[3] + d[7]) / ios, 3) if ios else 0.0,
            "util_pct": round(min(100.0, d[9] / (dt * 1000) * 100), 1),
            "queue_depth": round(d[10] / (dt * 1000), 2),
        })
    if rows:
        out = run_dir / "parsed" / "device_io.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=DEVICE_CSV_COLUMNS)
            w.writeheader()
            w.writerows(rows)
    return rows


def _sustained_peak(
        rows: list[dict[str, float]], window_s: int,
) -> tuple[float, float, float, float, float]:
    """(peak windowed-avg IOPS, util%, queue, window start/end epoch-ms) over
    any window_s-long span."""
    if not rows:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    best = (0.0, 0.0, 0.0, rows[0]["t_epoch_ms"], rows[-1]["t_epoch_ms"])
    found_full_window = False
    for i in range(len(rows)):
        j = i
        t0 = rows[i]["t_epoch_ms"]
        while j < len(rows) and rows[j]["t_epoch_ms"] - t0 <= window_s * 1000:
            j += 1
        win = rows[i:j]
        if not win or (win[-1]["t_epoch_ms"] - t0) < (window_s - 2) * 1000:
            continue
        found_full_window = True
        avg = sum(r["iops"] for r in win) / len(win)
        if avg > best[0]:
            best = (avg,
                    sum(r["util_pct"] for r in win) / len(win),
                    sum(r["queue_depth"] for r in win) / len(win),
                    t0, win[-1]["t_epoch_ms"])
    if not found_full_window and rows:         # series shorter than the window
        best = (sum(r["iops"] for r in rows) / len(rows),
                sum(r["util_pct"] for r in rows) / len(rows),
                sum(r["queue_depth"] for r in rows) / len(rows),
                rows[0]["t_epoch_ms"], rows[-1]["t_epoch_ms"])
    return best


def load_event_markers(run_dir: Path) -> list[tuple[float, str]]:
    """(epoch_ms, label) markers from events.jsonl, sorted — used to attribute
    the verdict's peak window to the phase that produced it."""
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    out: list[tuple[float, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            ts = str(ev["ts_utc"]).replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            out.append((dt.timestamp() * 1000.0, str(ev.get("label", ""))))
        except (ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda p: p[0])
    return out


def _hms(epoch_ms: float) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000.0,
                                  tz=timezone.utc).strftime("%H:%M:%S")


def compute_verdict(rows: list[dict[str, float]], limits: Limits,
                    events: Optional[list[tuple[float, str]]] = None,
                    ) -> dict[str, Any]:
    """capped / exceeds / inconclusive against the recorded reference limits.

    ``events`` ((epoch_ms, label) markers, see load_event_markers) attributes
    the sustained-peak window to the phase that produced it — a peak inside
    "fileio run" is ceiling evidence; the same number during a prepare or an
    end-of-run flush is a different IO regime and must be read as such.
    """
    peak10, util, queue, w_start, w_end = _sustained_peak(rows, SUSTAIN_WINDOW_S)
    peak1 = max((r["iops"] for r in rows), default=0.0)
    tol = limits.tolerance_pct / 100.0
    std, burst = limits.standard_iops, limits.burst_iops
    v: dict[str, Any] = {
        "limits": {"standard_iops": std, "burst_iops": burst,
                   "target_iops": limits.target_iops,
                   "tolerance_pct": limits.tolerance_pct},
        "peak_sustained_iops": round(peak10, 1),
        "sustain_window_s": SUSTAIN_WINDOW_S,
        "peak_1s_iops": round(peak1, 1),
        "util_pct_at_peak": round(util, 1),
        "queue_depth_at_peak": round(queue, 2),
        "samples": len(rows),
    }
    peak_suffix = ""
    if rows:
        v["peak_window_start_utc"] = datetime.fromtimestamp(
            w_start / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        v["peak_window_end_utc"] = datetime.fromtimestamp(
            w_end / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        peak_suffix = f" [peak window {_hms(w_start)}–{_hms(w_end)} UTC"
        # attribute by window MIDPOINT: event stamps have second precision
        # and a window can straddle a phase boundary — majority wins
        mid = (w_start + w_end) / 2
        during, next_ts = "", None
        for ts_ms, label in events or []:
            if ts_ms <= mid:
                if label:
                    during = label
            elif next_ts is None:
                next_ts = ts_ms
        if during:
            v["peak_during"] = during
            peak_suffix += f", during '{during}'"
            # a peak butting against the phase's END is usually a transition
            # artifact (for fileio: sysbench's exit fsync flush — large
            # mergeable writes, a different regime than the steady load).
            # Bounded on both sides: a marker long after the window is not
            # "its tail", and next_ts < w_end means the window itself
            # straddles the boundary (already softened by midpoint rule).
            if next_ts is not None and 0 <= next_ts - w_end <= 15_000:
                v["peak_at_phase_tail"] = True
                peak_suffix += (", at its tail — check whether this is a "
                                "flush/transition burst rather than the "
                                "steady load")
        peak_suffix += "]"
    if not rows:
        v["finding"] = "inconclusive"
        v["detail"] = ("no device series was captured — run was not "
                       "cluster-aware or the sampler degraded (see "
                       "env/device_io_warning.txt)")
        return v
    if peak10 >= std * (1 + tol):
        v["finding"] = "exceeds"
        v["detail"] = (f"sustained {peak10:,.0f} IOPS over {SUSTAIN_WINDOW_S}s — "
                       f"above the standard limit of {std:,} (+{limits.tolerance_pct:.0f}% "
                       f"tolerance); observed ceiling ~{peak10:,.0f} IOPS"
                       + (f"; 1s bursts to {peak1:,.0f}" if peak1 > peak10 * 1.05 else ""))
    elif peak10 >= std * (1 - tol):
        v["finding"] = "capped"
        v["detail"] = (f"sustained IOPS plateau at {peak10:,.0f} — within "
                       f"±{limits.tolerance_pct:.0f}% of the standard "
                       f"{std:,} IOPS limit (device util {util:.0f}%, queue "
                       f"{queue:.1f}); the throttle is the binding constraint")
        if peak1 >= std * (1 + tol):
            v["detail"] += (f"; 1s peaks to {peak1:,.0f} consistent with the "
                            f"{burst:,} burst allowance")
    else:
        v["finding"] = "inconclusive"
        if util >= 95 and queue >= 64:
            v["detail"] = (f"device fully utilized ({util:.0f}%) with a deep "
                           f"queue (QD~{queue:.0f}) but only {peak10:,.0f} IOPS "
                           f"sustained — plateau far below the {std:,} limit; "
                           "the volume itself looks smaller-provisioned than "
                           "standard, re-check sizing")
        elif util >= 95:
            v["detail"] = (f"device busy ({util:.0f}% util) but the queue was "
                           f"shallow (QD~{queue:.0f}) at only {peak10:,.0f} "
                           "sustained IOPS — on network volumes utilization "
                           "means time-busy, NOT saturation; this concurrency "
                           "never actually tested the limit. Drive deeper "
                           "queues: the device-probe (async backlog) or more "
                           "client concurrency")
        else:
            v["detail"] = (f"only {peak10:,.0f} sustained IOPS with device util "
                           f"{util:.0f}% — the load never generated enough "
                           "pressure to test the limit; the bottleneck is "
                           "upstream (driver concurrency, CPU, or Postgres's "
                           "synchronous read path). Redesign with more threads, "
                           "a larger dataset, or the device-probe mode")
    v["detail"] += peak_suffix
    return v
