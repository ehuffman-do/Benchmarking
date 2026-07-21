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
        f"--file-total-size={dp.file_total_size_gb:g}G",
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
    rows = [r for r in derive_device_series(run_dir)
            if t0 <= r["t_epoch_ms"] <= t1]
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
                  "unrecognized — raw/fileio_run.log has the original)",
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
        run_id = f"{make_run_id(spec.run.label)}-{n}"
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
    pod_name = f"pgb-fileio-{int(time.time())}"
    created = {"pod": False}
    sampler: Optional[deviceio.DeviceIoSampler] = None

    def _cleanup() -> None:
        # files first (needs the pod), then the pod itself; both idempotent.
        # keep_files leaves the test files for the next probe iteration.
        if created["pod"]:
            if not dp.keep_files:
                try:
                    kube.exec(pod_name, "fileio",
                              ["sh", "-c", f"rm -rf /pgdata/{PROBE_DIR}"],
                              timeout_s=120)
                except KubeError:
                    pass
            try:
                kube.run(["delete", "pod", pod_name, "--wait=false"])
            except KubeError:
                pass
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

        # free-space guardrail: >= 2x file_total_size on /pgdata
        res = kube.exec(leader, "database",
                        ["df", "-Pk", "/pgdata"], timeout_s=20, check=True)
        avail_kb = int(res.stdout.strip().splitlines()[-1].split()[3])
        need_kb = int(dp.file_total_size_gb * 2 * 1048576)
        if dp.keep_files:
            # reused files are already allocated; only headroom is needed
            need_kb = int(dp.file_total_size_gb * 0.2 * 1048576)
        if avail_kb < need_kb:
            raise RunError(
                f"free space guardrail: {avail_kb / 1048576:.1f} GiB available on "
                f"/pgdata, need >= {need_kb / 1048576:.0f} GiB "
                f"(2x file_total_size_gb={dp.file_total_size_gb:g})",
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
        have_files = kube.exec(
            pod_name, "fileio",
            ["sh", "-c", f"ls /pgdata/{PROBE_DIR}/test_file.0"],
            timeout_s=20).ok
        if dp.keep_files and have_files:
            logger.info("reusing existing test files in /pgdata/%s "
                        "(keep_files: true — prepare skipped)", PROBE_DIR)
            atomic_write_text(run_dir / "raw" / "fileio_prepare.log",
                              "(skipped: reusing existing test files)\n")
        else:
            logger.info("fileio prepare (%.0fG in %d files) ...",
                        dp.file_total_size_gb, dp.file_num)
            res = kube.exec(pod_name, "fileio", sh(["prepare"]),
                            timeout_s=3600, check=True)
            atomic_write_text(run_dir / "raw" / "fileio_prepare.log", res.stdout)
        t0 = utc_now_iso()
        logger.info("fileio run: %s for %ds x%d threads ...",
                    dp.test_mode, dp.duration_s, dp.threads)
        res = kube.exec(pod_name, "fileio",
                        sh([f"--time={dp.duration_s}", "--report-interval=1",
                            "run"]),
                        timeout_s=float(dp.duration_s + 300), check=True)
        atomic_write_text(run_dir / "raw" / "fileio_run.log", res.stdout)
        result = parse_fileio_result(res.stdout)
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
        manifest.status = "complete"
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
    verdict = deviceio.compute_verdict(rows, spec.limits)
    evidence.build_evidence(run_dir, spec, verdict)
    report_evidence.generate_evidence_report(run_dir)
    line = f"IOPS verdict: {verdict['finding'].upper()} — {verdict['detail']}"
    logger.info("%s", line)
    print(line, flush=True)
    print(f"device-probe report: {run_dir / 'report.html'}")
    return 0 if verdict["finding"] != "inconclusive" else 1
