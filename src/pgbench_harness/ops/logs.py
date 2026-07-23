"""Live log follower op — the capture backend for the console's Logs tab.

The web tier never runs kubectl, so log streaming follows the same contract
as every other cluster interaction: an enqueued worker job. This op attaches
``kubectl logs --follow`` to each selected pod/container, appends the lines
(with kubectl ``--timestamps``) to one file per source under ``raw/``, and
the webapp's SSE endpoint tails those files to the browser.

Design notes:
* streams auto-reattach when a pod dies (reusing the scenario LogStream
  contract — the pod dying is often exactly what the operator wants to see);
* a hard cap on concurrent streams keeps a big cluster from fanning out into
  dozens of kubectl children;
* the op runs until stopped (worker cancel) or ``max_duration_s`` elapses —
  it occupies the logs lane, never a benchmark slot.
"""

from __future__ import annotations

import time
from pathlib import Path

from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.oprun import EXIT_FAILED, EXIT_OK, OpsRun
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.ops.scenario import LogStream

MAX_STREAMS = 12

# Category order mirrors the console's source grouping.
CATEGORIES = ("postgres", "pgbackrest", "pgbouncer", "operator", "backup_jobs",
              "sidecars")


def discover_sources(kube: Kube, cr_name: str) -> list[dict[str, str]]:
    """Every followable pod/container for the target, grouped by category."""
    from pgbench_harness.ops.discover import classify_pods
    items = kube.json(["get", "pods"]).get("items") or []
    buckets = classify_pods(items, cr_name)
    sources: list[dict[str, str]] = []

    def add(pod: str, container: str, category: str) -> None:
        sources.append({"pod": pod, "container": container,
                        "category": category})

    for p in buckets["instances"]:
        for c in p.get("containers") or []:
            if c == "database":
                add(p["name"], c, "postgres")      # postgres + patroni interleaved
            elif "pgbackrest" in c:
                add(p["name"], c, "pgbackrest")
            else:
                add(p["name"], c, "sidecars")
    for p in buckets["pgbouncer"]:
        for c in p.get("containers") or []:
            add(p["name"], c, "pgbouncer" if "pgbouncer" in c else "sidecars")
    for p in buckets["backup_jobs"]:
        for c in p.get("containers") or []:
            add(p["name"], c, "backup_jobs")
    for p in buckets["other"]:
        name = str(p.get("name", ""))
        if "operator" in name:
            for c in p.get("containers") or []:
                add(name, c, "operator")
    return sources


class TailStream(LogStream):
    """LogStream with a caller-chosen --tail/--since on first attach."""

    def __init__(self, kube: Kube, run: OpsRun, pod: str, container: str,
                 out_name: str, tail: int, since: str) -> None:
        super().__init__(kube, run, pod, container, out_name)
        self.tail = tail
        self.since = since
        self._first = True

    def run(self) -> None:  # noqa: A003
        attach = 0
        while not self.stop_event.is_set():
            attach += 1
            with open(self.path, "a", encoding="utf-8") as fh:
                if attach > 1:
                    fh.write(f"[capture] reattach #{attach}\n")
                    fh.flush()
                args = ["logs", "-f", "--timestamps", self.pod,
                        "-c", self.container]
                if self._first:
                    # first attach honors the requested history window …
                    args += ([f"--since={self.since}"] if self.since
                             else [f"--tail={self.tail}"])
                    self._first = False
                else:
                    # … reattaches only need continuity
                    args += ["--since=2s"]
                try:
                    self.proc = self.kube.stream(args, stdout=fh)
                except OSError:
                    self.stop_event.wait(2)
                    continue
                while self.proc.poll() is None and not self.stop_event.is_set():
                    time.sleep(0.3)
                if self.proc.poll() is None:
                    self.proc.terminate()
            if not self.stop_event.is_set():
                self.stop_event.wait(1.5)


def run_logs(spec: OpsSpec, results_dir: Path) -> int:
    t = spec.target
    params = spec.params
    tail = max(10, min(int(params.get("tail") or 1000), 20000))
    since = str(params.get("since") or "")
    max_duration_s = float(params.get("max_duration_s") or 6 * 3600)
    wanted = params.get("sources")
    run = OpsRun(results_dir, "logs", spec.label,
                 target={"name": t.name, "namespace": t.namespace,
                         "cr_kind": t.cr_kind, "cr_name": t.cr_name},
                 params=params)
    log = run.get_logger()
    kube = Kube(context=t.context, namespace=t.namespace)
    threads: list[TailStream] = []
    try:
        available = discover_sources(kube, t.cr_name)
        if isinstance(wanted, list) and wanted:
            keys = {(str(w.get("pod") or ""), str(w.get("container") or ""))
                    for w in wanted if isinstance(w, dict)}
            selected = [s for s in available
                        if (s["pod"], s["container"]) in keys]
        else:
            # default: the postgres/patroni streams — the highest-signal set
            selected = [s for s in available if s["category"] == "postgres"]
        dropped = len(selected) - MAX_STREAMS
        if dropped > 0:
            run.event("logs", f"stream cap: following first {MAX_STREAMS} "
                      f"of {len(selected)} sources",
                      "narrow the selection to see the rest")
            selected = selected[:MAX_STREAMS]
        if not selected:
            run.finalize("failed", error="no matching pod/container sources "
                         "to follow")
            return EXIT_FAILED
        manifest = []
        for s in selected:
            out_name = f"logs_{s['pod']}_{s['container']}.log"
            manifest.append({**s, "file": out_name})
            threads.append(TailStream(kube, run, s["pod"], s["container"],
                                      out_name, tail, since))
        # the SSE endpoint reads this to label streams by category
        run.status_update(sources=manifest, phase="following")
        for th in threads:
            th.start()
        run.event("logs", f"following {len(threads)} container stream(s)",
                  ", ".join(f"{s['pod']}/{s['container']}" for s in selected)[:400]
                  + (f" (tail {tail})" if not since else f" (since {since})"))
        deadline = time.monotonic() + max_duration_s
        while time.monotonic() < deadline:
            time.sleep(1)
        run.event("logs", "max duration reached — stopping streams", "")
        run.finalize("complete", headline={"sources": len(threads)})
        return EXIT_OK
    except KeyboardInterrupt:
        run.finalize("canceled", headline={"sources": len(threads)})
        return EXIT_OK
    except KubeError as exc:
        log.error("logs op failed: %s", exc)
        run.finalize("failed", error=str(exc)[:500])
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — never leave the run stuck 'running'
        log.exception("logs op crashed")
        run.finalize("failed", error=f"internal error: {str(exc)[:300]}")
        return EXIT_FAILED
    finally:
        for th in threads:
            try:
                th.stop()
            except Exception:  # noqa: BLE001
                pass
        for th in threads:
            th.join(timeout=5)
