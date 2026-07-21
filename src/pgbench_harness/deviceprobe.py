"""Direct device probe: sysbench fileio on the pgdata volume (Percona's
methodology, under harness control). TEST CLUSTERS ONLY.

A pod is pinned to the primary's node and mounts the pgdata PVC (RWO allows a
second mount on the same node). sysbench fileio drives direct async IO against
test files inside a dedicated subdirectory — Postgres's synchronous read path
is out of the equation, so a deep async queue exposes the hard throttle (and
any burst window). The 1s device sampler runs throughout; results flow into
the same raw/summarize/evidence pipeline as SQL runs.

Guardrails: refuses without ``allow_device_probe: true``; requires free space
>= 2x file_total_size before prepare; cleans up the test files AND the pod on
every exit path including SIGTERM.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any, Optional

from pgbench_harness import deviceio, evidence, report_evidence
from pgbench_harness.errors import RunError
from pgbench_harness.manifest import Manifest
from pgbench_harness.spec import Spec, dump_spec_copy
from pgbench_harness.util import (atomic_write_json, atomic_write_text,
                                  make_run_id, setup_logging, utc_now_iso)

PROBE_DIR = "pgb-fileio-probe"          # all test files live under /pgdata/<this>


def _mark(run_dir: Path, label: str, note: str = "") -> None:
    """Stamp a probe phase into events.jsonl — the verdict attributes its
    sustained-peak window to these markers (a peak during 'fileio run' is
    ceiling evidence; during prepare or the end-of-run flush it is not)."""
    ev = {"ts_utc": utc_now_iso(), "type": "phase", "label": label,
          "note": note, "source": "device-probe"}
    path = run_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")

FILEIO_RESULT_RES = {
    "reads_s": re.compile(r"reads/s:\s+([\d.]+)"),
    "writes_s": re.compile(r"writes/s:\s+([\d.]+)"),
    "fsyncs_s": re.compile(r"fsyncs/s:\s+([\d.]+)"),
    "read_mb_s": re.compile(r"read, MiB/s:\s+([\d.]+)"),
    "write_mb_s": re.compile(r"written, MiB/s:\s+([\d.]+)"),
}


def _fileio_args(spec: Spec) -> list[str]:
    dp = spec.device_probe
    assert dp is not None
    return [
        f"--file-num={dp.file_num}",
        # integer MB: sysbench's size parser is integer+suffix, so a
        # fractional spec value like 0.5 (G) must not be emitted as "0.5G"
        f"--file-total-size={int(dp.file_total_size_gb * 1024)}M",
        f"--file-test-mode={dp.test_mode}",
        f"--file-io-mode={dp.io_mode}",
        f"--file-async-backlog={dp.async_backlog}",
        f"--file-fsync-freq={dp.fsync_freq}",
        f"--file-block-size={dp.block_size_kb * 1024}",
        f"--threads={dp.threads}",
    ]


def _pod_manifest(spec: Spec, pod_name: str, node: str, pvc: str) -> str:
    dp = spec.device_probe
    assert dp is not None
    return json.dumps({
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod_name,
                     "labels": {"app": "pgbench-harness-device-probe"}},
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "containers": [{
                "name": "fileio", "image": dp.image,
                "command": ["sleep", "infinity"],
                "volumeMounts": [{"name": "pgdata", "mountPath": "/pgdata"}],
            }],
            "volumes": [{"name": "pgdata",
                         "persistentVolumeClaim": {"claimName": pvc}}],
        },
    })


def _fill_from_device(run_dir: Path, start_iso: str,
                      end_iso: str) -> dict[str, Any]:
    """Probe figures from the harness's own device series over the fileio
    window — used when the sysbench summary format is unrecognized."""
    from datetime import datetime, timezone

    from pgbench_harness.deviceio import derive_device_series

    def ms(iso: str) -> float:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp() * 1000

    try:
        t0, t1 = ms(start_iso), ms(end_iso)
    except (ValueError, TypeError):
        return {}
    all_rows = derive_device_series(run_dir)
    rows = [r for r in all_rows if t0 <= r["t_epoch_ms"] <= t1]
    skew_note = ""
    if not rows and all_rows:
        # device samples are stamped by the pod's clock, the window by the
        # harness host's — NTP skew can empty the intersection while the
        # series itself is complete. The whole probe run IS the series.
        rows, skew_note = all_rows, (" — clock skew between harness host and "
                                     "pod suspected: window matched no "
                                     "samples, averaged the whole series")
    if not rows:
        return {"source": "no summary and no device rows — see raw/fileio_run.log"}
    n = len(rows)
    return {
        "reads_s": round(sum(r["reads_s"] for r in rows) / n, 1),
        "writes_s": round(sum(r["writes_s"] for r in rows) / n, 1),
        "iops": round(sum(r["iops"] for r in rows) / n, 1),
        "read_mb_s": round(sum(r["read_mb_s"] for r in rows) / n, 2),
        "write_mb_s": round(sum(r["write_mb_s"] for r in rows) / n, 2),
        "source": "derived from device counters (sysbench summary format "
                  "unrecognized — raw/fileio_run.log has the original)"
                  + skew_note,
    }


def parse_fileio_result(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, rx in FILEIO_RESULT_RES.items():
        m = rx.search(text)
        if m:
            out[key] = float(m.group(1))
    if "reads_s" in out or "writes_s" in out:
        out["iops"] = round(out.get("reads_s", 0.0) + out.get("writes_s", 0.0), 1)
    return out


def run_device_probe(spec: Spec, results_dir: Path, dry_run: bool = False) -> int:
    from pgbench_harness.ops.discover import resolve_leader_resilient
    from pgbench_harness.ops.kube import Kube, KubeError
    dp, cl = spec.device_probe, spec.cluster
    assert dp is not None and cl is not None
    if not dp.allow_device_probe:
        raise RunError(
            "device-probe refused: the spec does not set "
            "device_probe.allow_device_probe: true",
            hint="this writes test files onto the cluster's pgdata volume and "
                 "saturates its IO — TEST CLUSTERS ONLY. Arm it explicitly.")
    fileio = ["sysbench", "fileio"] + _fileio_args(spec)
    if dry_run:
        print(f"# device-probe dry run for '{spec.run.label}' "
              f"(cluster {cl.cr_kind}/{cl.cr_name}, ns {cl.namespace})")
        print(f"# pod: image {dp.image}, pinned to the primary's node, "
              f"mounting the pgdata PVC; files under /pgdata/{PROBE_DIR}/")
        print("  " + " ".join(fileio + ["prepare"]))
        print("  " + " ".join(fileio + [f"--time={dp.duration_s}",
                                        "--report-interval=1", "run"]))
        print("  " + " ".join(fileio + ["cleanup"]))
        print("# guardrails: free space >= 2x file size checked before prepare; "
              "files + pod removed on every exit path")
        return 0

    run_id = make_run_id(spec.run.label or "device-probe")
    run_dir = results_dir / run_id
    n = 1
    while run_dir.exists():
        n += 1
        run_id = f"{make_run_id(spec.run.label or 'device-probe')}-{n}"
        run_dir = results_dir / run_id
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    dump_spec_copy(spec, run_dir / "spec.yaml")
    dump_spec_copy(spec, run_dir / "env" / "spec.yaml")
    manifest = Manifest(run_id=run_id, label=spec.run.label,
                        edition=spec.run.edition,
                        tshirt_size=spec.run.tshirt_size, mode="probe")
    manifest.status = "running"
    manifest.save(run_dir)
    logger = setup_logging(run_dir / "harness.log")
    logger.info("device-probe %s -> %s", run_id, run_dir)

    kube = Kube(context=cl.context, namespace=cl.namespace)
    pod_name = f"pgb-fileio-{int(time.time())}-{os.getpid() % 10000}"
    created = {"pod": False}
    sampler: Optional[deviceio.DeviceIoSampler] = None

    def _cleanup() -> None:
        # files first (needs the pod), then the pod itself; both idempotent.
        # keep_files leaves the test files for the next probe iteration.
        if created["pod"] and not dp.keep_files:
            try:
                kube.exec(pod_name, "fileio",
                          ["sh", "-c", f"rm -rf /pgdata/{PROBE_DIR}"],
                          timeout_s=120)
            except KubeError as exc:
                logger.warning("could not remove /pgdata/%s (%s) — reclaim "
                               "manually: kubectl exec into the primary and "
                               "rm -rf /pgdata/%s", PROBE_DIR, exc, PROBE_DIR)
        # ALWAYS attempt the pod delete, even if the apply looked like it
        # failed: a timed-out apply may still have created the pod, and a
        # leaked pod holds the RWO pgdata PVC — if the Postgres pod later
        # reschedules to another node the volume cannot attach.
        try:
            kube.run(["delete", "pod", pod_name, "--wait=false",
                      "--ignore-not-found"])
        except KubeError as exc:
            logger.warning("PROBE POD MAY BE LEAKED: could not delete pod %s "
                           "(%s). It mounts the pgdata PVC (RWO) — delete it "
                           "manually: kubectl -n %s delete pod %s",
                           pod_name, exc, cl.namespace, pod_name)
        created["pod"] = False

    def _on_signal(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, _on_signal)
    try:
        # primary + its node + its pgdata PVC
        _instances, leader, _view, _att = resolve_leader_resilient(
            kube, cl.cr_name, timeout_s=60, poll_s=3)
        raw = kube.json(["get", "pod", leader])
        node = (raw.get("spec") or {}).get("nodeName", "")
        pvc = ""
        for vol in (raw.get("spec") or {}).get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName", "")
            if claim:
                pvc = claim
                break
        if not node or not pvc:
            raise RunError(f"could not resolve node/PVC from primary pod {leader}")
        logger.info("primary %s on node %s, pgdata PVC %s", leader, node, pvc)
        deviceio.capture_storage_identity(spec, run_dir, logger)

        # Existence + geometry of any kept test files, checked on the LEADER
        # pod (same volume) BEFORE the guardrail: the relaxed space budget is
        # only safe when the files are really there, and reusing files of a
        # different geometry silently falsifies the evidence (sysbench accepts
        # larger-than-expected files without complaint). check=True so an exec
        # failure is a loud error, not a silent multi-minute re-prepare.
        expected_file_bytes = (int(dp.file_total_size_gb * 1024) * 1048576
                               ) // dp.file_num
        res = kube.exec(
            leader, "database",
            ["sh", "-c",
             f"stat -c %s /pgdata/{PROBE_DIR}/test_file.0 2>/dev/null "
             f"|| echo MISSING; "
             f"test -e /pgdata/{PROBE_DIR}/test_file.{dp.file_num - 1} "
             f"&& echo LAST-YES || echo LAST-NO"],
            timeout_s=20, check=True)
        first_size, last_ok = (res.stdout.split() + ["", ""])[:2]
        have_files = first_size != "MISSING"
        reuse = bool(dp.keep_files and have_files)
        if reuse and (last_ok != "LAST-YES"
                      or first_size != str(expected_file_bytes)):
            raise RunError(
                f"keep_files: existing test files in /pgdata/{PROBE_DIR} do "
                f"not match this spec's geometry (found test_file.0 of "
                f"{first_size} bytes, last file "
                f"{'present' if last_ok == 'LAST-YES' else 'missing'}; "
                f"expected {dp.file_num} files of {expected_file_bytes} "
                "bytes)",
                hint="keep file_num and file_total_size_gb identical across "
                     "kept-file iterations, or delete "
                     f"/pgdata/{PROBE_DIR} (or set keep_files: false) to "
                     "re-prepare with the new geometry.")

        # free-space guardrail: >= 2x file_total_size on /pgdata when the
        # files must be created; reused files are already allocated, so only
        # headroom is needed
        res = kube.exec(leader, "database",
                        ["df", "-Pk", "/pgdata"], timeout_s=20, check=True)
        avail_kb = int(res.stdout.strip().splitlines()[-1].split()[3])
        factor = 0.2 if reuse else 2.0
        need_kb = int(dp.file_total_size_gb * factor * 1048576)
        if avail_kb < need_kb:
            raise RunError(
                f"free space guardrail: {avail_kb / 1048576:.1f} GiB available on "
                f"/pgdata, need >= {need_kb / 1048576:.0f} GiB "
                f"({factor:g}x file_total_size_gb={dp.file_total_size_gb:g}"
                f"{', reusing kept files' if reuse else ''})",
                hint="shrink device_probe.file_total_size_gb or grow the volume.")

        kube.run(["apply", "-f", "-"],
                 input_text=_pod_manifest(spec, pod_name, node, pvc), check=True)
        created["pod"] = True
        deadline = time.monotonic() + 120
        while True:
            praw = kube.json(["get", "pod", pod_name])
            phase = (praw.get("status") or {}).get("phase")
            if phase == "Running":
                break
            if time.monotonic() >= deadline:
                raise RunError(f"probe pod never reached Running (phase {phase})")
            time.sleep(2)
        logger.info("probe pod %s Running on %s", pod_name, node)
        kube.exec(pod_name, "fileio",
                  ["mkdir", "-p", f"/pgdata/{PROBE_DIR}"], check=True)

        sampler = deviceio.DeviceIoSampler(spec, run_dir, logger)
        sampler.start()

        sh = lambda phase_args: ["sh", "-c", "cd /pgdata/" + PROBE_DIR + " && "
                                 + " ".join(fileio + phase_args)]
        if reuse:
            logger.info("reusing existing test files in /pgdata/%s "
                        "(keep_files: true — prepare skipped)", PROBE_DIR)
            atomic_write_text(run_dir / "raw" / "fileio_prepare.log",
                              "(skipped: reusing existing test files)\n")
        else:
            logger.info("fileio prepare (%.0fG in %d files) ...",
                        dp.file_total_size_gb, dp.file_num)
            _mark(run_dir, "fileio prepare",
                  f"{dp.file_total_size_gb:g}G in {dp.file_num} files "
                  "(sequential writes — a different IO regime than the run)")
            # a throttled/degraded volume can crawl: budget >= ~17 MB/s
            res = kube.exec(pod_name, "fileio", sh(["prepare"]),
                            timeout_s=max(3600.0,
                                          dp.file_total_size_gb * 60.0),
                            check=True)
            atomic_write_text(run_dir / "raw" / "fileio_prepare.log", res.stdout)
        t0 = utc_now_iso()
        logger.info("fileio run: %s for %ds x%d threads ...",
                    dp.test_mode, dp.duration_s, dp.threads)
        _mark(run_dir, "fileio run",
              f"{dp.test_mode} x{dp.threads} threads, backlog "
              f"{dp.async_backlog}, {dp.block_size_kb}K blocks; sysbench "
              "fsyncs all files at exit, so the tail of this window is a "
              "writeback flush, not steady-state random IO")
        # The load-driving exec stream can die (API blip, token refresh)
        # while sysbench completes fine in the pod — and the device series,
        # the actual ground truth, is intact either way. Salvage instead of
        # discarding the whole probe: keep partial output, let the summary
        # fall back to device counters, and mark the run partial.
        run_exec_failed = ""
        try:
            res = kube.exec(pod_name, "fileio",
                            sh([f"--time={dp.duration_s}",
                                "--report-interval=1", "run"]),
                            timeout_s=float(dp.duration_s + 300))
            run_out = res.stdout
            if not res.ok:
                run_exec_failed = (f"exec exited rc={res.returncode}: "
                                   f"{res.stderr[:300]}")
        except KubeError as exc:
            run_out, run_exec_failed = "", str(exc)
        atomic_write_text(run_dir / "raw" / "fileio_run.log",
                          run_out or f"(no output — {run_exec_failed})\n")
        _mark(run_dir, "fileio done", "load stopped; anything after is idle")
        if run_exec_failed:
            logger.warning("fileio run exec died (%s) — sysbench may have "
                           "completed in the pod anyway; deriving figures "
                           "from the device series", run_exec_failed)
        result = parse_fileio_result(run_out)
        if "iops" not in result:
            # sysbench build printed an unrecognized summary format (field
            # report: "fileio result: ?") — the device counters are the
            # ground truth anyway, so derive the figures from them
            result.update(_fill_from_device(
                run_dir, t0, utc_now_iso()))
        result.update({"started_utc": t0, "finished_utc": utc_now_iso(),
                       "test_mode": dp.test_mode, "io_mode": dp.io_mode,
                       "threads": dp.threads, "block_kb": dp.block_size_kb,
                       "file_total_size_gb": dp.file_total_size_gb})
        atomic_write_json(run_dir / "parsed" / "fileio_summary.json", result)
        logger.info("fileio result: %s", result.get("iops", "?"))
        if dp.keep_files:
            logger.info("keep_files: true — test files left in /pgdata/%s for "
                        "the next probe (run once with keep_files: false, or "
                        "rm -rf the directory, to reclaim %.0fG)",
                        PROBE_DIR, dp.file_total_size_gb)
            atomic_write_text(run_dir / "raw" / "fileio_cleanup.log",
                              "(skipped: keep_files)\n")
        else:
            res = kube.exec(pod_name, "fileio", sh(["cleanup"]), timeout_s=600)
            atomic_write_text(run_dir / "raw" / "fileio_cleanup.log",
                              res.stdout if res.ok else res.stderr)
        manifest.status = "partial" if run_exec_failed else "complete"
    except (Exception, KeyboardInterrupt) as exc:
        manifest.status = "failed"
        manifest.finished_utc = utc_now_iso()
        manifest.save(run_dir)
        logger.error("device-probe failed: %s", exc)
        raise exc if not isinstance(exc, KeyboardInterrupt) \
            else RunError("device-probe interrupted — cleanup performed")
    finally:
        if sampler:
            sampler.stop()
        _cleanup()
        signal.signal(signal.SIGTERM, old_term)
    manifest.finished_utc = utc_now_iso()
    manifest.save(run_dir)
    rows = deviceio.derive_device_series(run_dir)
    verdict = deviceio.compute_verdict(rows, spec.limits,
                                       deviceio.load_event_markers(run_dir))
    evidence.build_evidence(run_dir, spec, verdict)
    report_evidence.generate_evidence_report(run_dir)
    line = f"IOPS verdict: {verdict['finding'].upper()} — {verdict['detail']}"
    logger.info("%s", line)
    print(line, flush=True)
    print(f"device-probe report: {run_dir / 'report.html'}")
    return 0 if verdict["finding"] != "inconclusive" else 1
