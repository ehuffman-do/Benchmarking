import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { Job, Me, Run } from "../types";
import { LiveChart } from "../components/LiveChart";
import { LogConsole } from "../components/LogConsole";
import { SeriesTable } from "../components/SeriesTable";
import { fmtCompact, fmtInt } from "../lib/format";
import {
  appendBatch, appendPg, emptyPg, emptySeries, openStream,
  type PgSeries, type Progress, type Series,
} from "../lib/sse";

function clock(s: number): string {
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

const PALETTE = {
  tps: "#2dd4bf",
  qps: "#6ea8fe",
  p99: "#e0a93b",
  err: "#f85149",
  reconn: "#a371f7",
  read: "#3fb950",
  write: "#e0a93b",
  other: "#a371f7",
};

export function RunDetail({ me }: { me: Me }) {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<Run | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [series, setSeries] = useState<Series>(emptySeries());
  const [pg, setPg] = useState<PgSeries>(emptyPg());
  const [log, setLog] = useState("");
  const [progress, setProgress] = useState<Progress | null>(null);
  const [streamState, setStreamState] = useState("connecting…");
  const [activeJob, setActiveJob] = useState<Job | null>(null);
  const [budget, setBudget] = useState(0);   // planned wall-clock budget (from hello)
  const [mode, setMode] = useState("");      // sweep | soak (from hello)
  const [verdict, setVerdict] = useState<{ finding: string; detail: string;
    peak_sustained_iops: number } | null>(null);
  const seriesRef = useRef<Series>(emptySeries());
  const pgRef = useRef<PgSeries>(emptyPg());
  const canRun = me.role === "operator" || me.role === "admin";

  useEffect(() => {
    api.get<Run>(`/api/runs/${runId}`).then(setRun).catch((e) => setErr(e.message));
    api.get<Job[]>(`/api/jobs?active=1`)
      .then((js) => setActiveJob(js.find((j) => j.run_id === runId) ?? null))
      .catch(() => {});
  }, [runId]);

  useEffect(() => {
    if (!run || !["complete", "partial", "failed"].includes(run.status || "")) return;
    api.get<{ verdict: { finding: string; detail: string;
                         peak_sustained_iops: number } | null }>(
      `/api/runs/${runId}/evidence`)
      .then((ev) => setVerdict(ev.verdict))
      .catch(() => setVerdict(null));
  }, [runId, run?.status]);

  useEffect(() => {
    const es = openStream(runId, {
      onHello: (h) => {
        seriesRef.current = emptySeries();
        pgRef.current = emptyPg();
        setSeries(seriesRef.current);
        setPg(pgRef.current);
        setLog("");
        setBudget(h.budget_s);
        setMode(h.mode);
        setStreamState("live");
      },
      onLog: (chunk) => setLog((prev) => prev + chunk),
      onSamples: (b) => {
        if (b.offset === 0) seriesRef.current = emptySeries();
        appendBatch(seriesRef.current, b);
        setSeries({ ...seriesRef.current });
      },
      onPg: (b) => {
        if (b.offset === 0) pgRef.current = emptyPg();
        appendPg(pgRef.current, b);
        setPg({ ...pgRef.current });
      },
      onProgress: (p) => setProgress(p),
      onDone: (d) => {
        setStreamState(`finished: ${d.status}`);
        setActiveJob(null);   // run is terminal -> hide Stop/Cancel
        api.get<Run>(`/api/runs/${runId}`).then(setRun).catch(() => {});
      },
      onError: () => setStreamState("reconnecting…"),
    });
    return () => es.close();
  }, [runId]);

  async function mark(type: string) {
    try {
      await api.post(`/api/runs/${runId}/mark`, { type, label: type });
      setStreamState(`marked ${type}`);
    } catch (e) {
      alert((e as Error).message);
    }
  }
  async function resume() {
    try {
      await api.post(`/api/runs/${runId}/resume`);
      window.location.href = "/ui";
    } catch (e) {
      alert((e as Error).message);
    }
  }
  async function stop() {
    if (!activeJob || !confirm(
      `Stop run ${runId}? sysbench is sent SIGTERM for a graceful partial finalize, `
      + `then SIGKILL if it doesn't exit in time.`)) return;
    try {
      await api.post(`/api/jobs/${activeJob.id}/stop`);
      setStreamState("stopping…");
    } catch (e) {
      alert((e as Error).message);
    }
  }
  async function del() {
    if (!confirm(`Permanently delete ${runId} and all of its data on disk `
      + `(results, report, raw logs)? This cannot be undone.`)) return;
    try {
      await api.del(`/api/runs/${runId}`);
      window.location.href = "/ui";
    } catch (e) {
      alert((e as Error).message);
    }
  }
  async function rerun() {
    if (!confirm("Re-run this run with the same config?")) return;
    try {
      const d = await api.post<{ needs_password: boolean }>(`/api/runs/${runId}/rerun`);
      if (d.needs_password)
        alert("Re-run queued — but this run had no saved target, so it needs a password. "
          + "Use Clone to re-enter credentials, or save the cluster under Targets.");
      window.location.href = "/ui";
    } catch (e) {
      alert((e as Error).message);
    }
  }

  if (err) return <div className="banner-err">{err}</div>;

  const last = series.t.length - 1;
  const curTps = last >= 0 ? series.tps[last] : null;
  const peakTps = series.tps.length ? Math.max(...series.tps) : null;
  const curP99 = last >= 0 ? series.p99[last] : null;
  const isSoak = run?.mode === "soak";
  const live = streamState === "live";
  const pct = progress && progress.budget_s
    ? Math.min(100, Math.round((progress.elapsed_s / progress.budget_s) * 100))
    : 0;
  // Anchor the x-axis to the planned budget ONLY for soaks (a fixed-duration window,
  // t=0..budget). Sweeps advance through levels and reset the per-second offset each
  // step, so a fixed budget axis is wrong there — let those charts auto-scale to data.
  const chartMax = mode === "soak" ? (budget || undefined) : undefined;

  return (
    <>
      <div className="toolbar">
        <div>
          <h1>{run?.label || runId}</h1>
          <div className="subtle mono" style={{ fontSize: 12 }}>{runId}</div>
        </div>
        <div className="spacer" />
        <span className={`badge ${run?.status || ""}`}>{run?.status || streamState}</span>
      </div>

      <div className="meta-row">
        {run?.target_host && <span className="chip mono">{run.target_host}</span>}
        {run?.edition && <span className="chip">{run.edition}</span>}
        {run?.tshirt_size && <span className="chip">{run.tshirt_size}</span>}
        <span className="chip">{run?.mode || "—"}</span>
        {run?.workload_type && <span className="chip mono">{run.workload_type}</span>}
        <span className="chip live-chip"><i className={live ? "dot on" : "dot"} /> {streamState}</span>
      </div>

      {verdict && (
        <div className={`banner-${verdict.finding === "exceeds" ? "ok" : "err"}`}
             style={{ marginBottom: 10 }}>
          <b>IOPS verdict: {verdict.finding.toUpperCase()}</b>
          {" — "}{verdict.detail}
        </div>
      )}
      <div className="actions">
        <Link className="btn primary" to={`/runs/${runId}/report`}>View report</Link>
        <a className="btn" href={`/runs/${runId}/report/download`}>Download</a>
        <a className="btn" href={`/runs/${runId}/spec`} target="_blank" rel="noreferrer">Spec</a>
        <a className="btn" href={`/runs/${runId}/artifact`}>Artifacts (.tar.gz)</a>
        {canRun && <Link className="btn" to={`/new?from=${runId}`}>Clone</Link>}
        {canRun && <button onClick={rerun}>Re-run</button>}
        {canRun && isSoak && <button onClick={() => mark("failover")}>⚑ Mark failover</button>}
        {canRun && isSoak && <button onClick={() => mark("scale_up")}>⚑ Mark scale-up</button>}
        {canRun && !isSoak && ["partial", "failed", "running"].includes(run?.status || "") && (
          <button onClick={resume}>Resume</button>
        )}
        {canRun && activeJob && <button className="btn danger" onClick={stop}>■ Stop run</button>}
        {canRun && !activeJob && <button className="btn danger" onClick={del}>Delete</button>}
      </div>

      {progress && (
        <div className="card">
          <div className="progress-line">
            <span className="mono">{clock(progress.elapsed_s)}</span>
            <div className="bar"><div className="fill" style={{ width: `${pct}%` }} /></div>
            <span className="mono subtle">{progress.budget_s ? `~${clock(progress.budget_s)}` : "—"}</span>
          </div>
          <div className="subtle" style={{ marginTop: 6, fontSize: 12.5 }}>
            {progress.levels_total > 0 && <>level {progress.current || "—"} · {progress.levels_done}/{progress.levels_total} levels · </>}
            {pct}% of planned budget
          </div>
        </div>
      )}

      <div className="kpi-row" style={{ marginBottom: 16 }}>
        <div className="kpi"><div className="label">Current TPS</div><div className="value">{curTps != null ? fmtInt(curTps) : "—"}</div></div>
        <div className="kpi"><div className="label">Peak TPS</div><div className="value">{peakTps ? fmtInt(peakTps) : "—"}</div></div>
        <div className="kpi"><div className="label">p99 latency</div><div className="value">{curP99 != null ? fmtInt(curP99) : "—"}<small> ms</small></div></div>
        <div className="kpi"><div className="label">Samples</div><div className="value">{fmtInt(series.t.length)}</div></div>
      </div>

      {/* Load generator (sysbench) — full-width, stacked, with a running time axis */}
      <div className="card">
        <LiveChart title="Throughput (TPS / QPS)" xs={series.t} xMax={chartMax} yFormat={(v) => fmtCompact(v)}
          series={[
            { label: "TPS", values: series.tps, stroke: PALETTE.tps },
            { label: "QPS", values: series.qps, stroke: PALETTE.qps, scale: "y2" },
          ]} />
      </div>
      <div className="card">
        <LiveChart title="QPS — read / write / other" xs={series.t} xMax={chartMax} yFormat={(v) => fmtCompact(v)}
          series={[
            { label: "read", values: series.qpsR, stroke: PALETTE.read },
            { label: "write", values: series.qpsW, stroke: PALETTE.write },
            { label: "other", values: series.qpsO, stroke: PALETTE.other },
          ]} />
      </div>
      <div className="card">
        <LiveChart title="p99 latency (ms) — per-second" xs={series.t} xMax={chartMax} yFormat={(v) => fmtCompact(v)}
          series={[{ label: "p99 ms", values: series.p99, stroke: PALETTE.p99 }]} />
      </div>
      <div className="card">
        <LiveChart title="Errors & reconnects (per second)" xs={series.t} xMax={chartMax} height={180}
          series={[
            { label: "errors/s", values: series.err, stroke: PALETTE.err },
            { label: "reconnects/s", values: series.reconn, stroke: PALETTE.reconn },
          ]} />
      </div>

      <SeriesTable series={series} />

      {pg.t.length > 0 && (
        <>
          <div className="section-label">PostgreSQL (engine-side) <span className="subtle">— the server's own pg_stat counters, scoped to the target database (rates use the server clock). Block I/O is logical (buffer cache), not device IOPS.</span></div>
          <div className="card">
            <LiveChart title="Transactions — commits / rollbacks (per second)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[
                { label: "commits/s", values: pg.commitsS, stroke: PALETTE.tps },
                { label: "rollbacks/s", values: pg.rollbacksS, stroke: PALETTE.err },
              ]} />
          </div>
          <div className="card">
            <LiveChart title="Tuples written (per second)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[
                { label: "inserted/s", values: pg.tupInsertedS, stroke: PALETTE.read },
                { label: "updated/s", values: pg.tupUpdatedS, stroke: PALETTE.write },
                { label: "deleted/s", values: pg.tupDeletedS, stroke: PALETTE.err },
              ]} />
          </div>
          <div className="card">
            <LiveChart title="Tuples read (per second)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[
                { label: "returned/s", values: pg.tupReturnedS, stroke: PALETTE.qps },
                { label: "fetched/s", values: pg.tupFetchedS, stroke: PALETTE.other },
              ]} />
          </div>
          <div className="card">
            <LiveChart title="Block I/O — buffer reads vs hits (blocks/s, logical)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[
                { label: "blks read/s (cache miss)", values: pg.blksReadS, stroke: PALETTE.err },
                { label: "blks hit/s (in cache)", values: pg.blksHitS, stroke: PALETTE.tps, scale: "y2" },
              ]} />
          </div>
          <div className="card">
            <LiveChart title="Cache hit % (interval)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => `${Math.round(v)}`}
              series={[{ label: "cache hit %", values: pg.cacheHit, stroke: PALETTE.tps }]} />
          </div>
          <div className="card">
            <LiveChart title="WAL throughput (MB/s)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[{ label: "WAL MB/s", values: pg.walMbs, stroke: PALETTE.p99 }]} />
          </div>
          <div className="card">
            <LiveChart title="Checkpoint activity (ms of work per second)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[
                { label: "write-time ms/s", values: pg.ckptWriteMsS, stroke: PALETTE.write },
                { label: "sync-time ms/s", values: pg.ckptSyncMsS, stroke: PALETTE.err },
              ]} />
          </div>
          <div className="card">
            <LiveChart title="Bgwriter buffers (per second)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[
                { label: "clean/s", values: pg.bgwCleanS, stroke: PALETTE.qps },
                { label: "alloc/s", values: pg.bgwAllocS, stroke: PALETTE.other },
              ]} />
          </div>
          <div className="card">
            <LiveChart title="Active connections" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[{ label: "active", values: pg.active, stroke: PALETTE.qps }]} />
          </div>
          <div className="card">
            <LiveChart title="Lock contention — blocked queries & max wait" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[
                { label: "blocked queries", values: pg.blockedQueries, stroke: PALETTE.err },
                { label: "max wait (s)", values: pg.lockWaitMaxS, stroke: PALETTE.p99, scale: "y2" },
              ]} />
          </div>
          <div className="card">
            <LiveChart title="Health — deadlocks/s & temp bytes/s" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
              series={[
                { label: "deadlocks/s", values: pg.deadlocksS, stroke: PALETTE.err },
                { label: "temp bytes/s", values: pg.tempBytesS, stroke: PALETTE.other, scale: "y2" },
              ]} />
          </div>
          {pg.replLagS.some((v) => !Number.isNaN(v)) && (
            <div className="card">
              <LiveChart title="Replication replay lag (s)" xs={pg.t} xMax={chartMax} height={180} yFormat={(v) => fmtCompact(v)}
                series={[{ label: "replay lag s", values: pg.replLagS, stroke: PALETTE.p99 }]} />
            </div>
          )}
        </>
      )}

      <LogConsole text={log} />
    </>
  );
}
