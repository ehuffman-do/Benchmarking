import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Run } from "../types";
import { LiveChart } from "../components/LiveChart";
import { fmtCompact, fmtInt, fmtNum } from "../lib/format";
import {
  appendBatch, appendPg, emptyPg, emptySeries, openStream,
  type PgSeries, type Series,
} from "../lib/sse";

// Distinct hues, one per run, reused across every chart + the legend/timeline.
const RUN_COLORS = ["#2dd4bf", "#f59e0b", "#6ea8fe", "#f85149", "#3fb950", "#a371f7"];
const MAX_RUNS = RUN_COLORS.length;

interface RunMeta {
  label?: string; mode?: string; status?: string;
  startUtc?: string; budget?: number; elapsed?: number; live?: boolean;
}

function hms(v: number): string {
  const s = Math.max(0, Math.round(v));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`;
}

// Scatter a run's per-second series onto a shared axis: a sample at run-elapsed
// t lands at unified second (offset + t); everything else is NaN (a chart gap).
function aligned(len: number, offset: number, ts: number[], vals: number[]): number[] {
  const out = new Array(len).fill(NaN);
  for (let i = 0; i < ts.length; i++) {
    const x = offset + ts[i];
    if (x >= 0 && x < len) out[x] = vals[i];
  }
  return out;
}

const epoch = (iso?: string): number => {
  if (!iso) return NaN;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? NaN : t / 1000;
};

export function LiveCompare() {
  const [sp] = useSearchParams();
  const ids = useMemo(
    () => (sp.get("runs") || "").split(",").map((s) => s.trim()).filter(Boolean).slice(0, MAX_RUNS),
    [sp]);
  const [meta, setMeta] = useState<Record<string, RunMeta>>({});
  const seriesRef = useRef<Record<string, Series>>({});
  const pgRef = useRef<Record<string, PgSeries>>({});
  const [, setNonce] = useState(0);
  const bump = () => setNonce((n) => n + 1);

  useEffect(() => {
    ids.forEach((id) => {
      api.get<Run>(`/api/runs/${id}`)
        .then((r) => setMeta((m) => ({ ...m, [id]: { ...(m[id] || {}), label: r.label, mode: r.mode, status: m[id]?.status || r.status } })))
        .catch(() => {});
    });
  }, [ids]);

  useEffect(() => {
    const closers = ids.map((id) => {
      seriesRef.current[id] = emptySeries();
      pgRef.current[id] = emptyPg();
      const es = openStream(id, {
        onHello: (h) => {
          seriesRef.current[id] = emptySeries();
          pgRef.current[id] = emptyPg();
          setMeta((m) => ({ ...m, [id]: { ...(m[id] || {}), startUtc: h.start_utc, mode: h.mode, status: h.status, budget: h.budget_s, live: true } }));
          bump();
        },
        onSamples: (b) => {
          if (b.offset === 0) seriesRef.current[id] = emptySeries();
          appendBatch(seriesRef.current[id], b);
          bump();
        },
        onPg: (b) => {
          if (b.offset === 0) pgRef.current[id] = emptyPg();
          appendPg(pgRef.current[id], b);
          bump();
        },
        onProgress: (p) => setMeta((m) => ({ ...m, [id]: { ...(m[id] || {}), elapsed: p.elapsed_s } })),
        onDone: (d) => setMeta((m) => ({ ...m, [id]: { ...(m[id] || {}), status: d.status, live: false } })),
        onError: () => {},
      });
      return () => es.close();
    });
    return () => closers.forEach((c) => c());
  }, [ids]);

  if (ids.length < 2) {
    return <div className="banner-err">Live compare needs two or more runs — open it from Compare with soaks selected.</div>;
  }

  // ── alignment on a shared real-time axis ──────────────────────────────
  const per = ids.map((id, i) => {
    const s = seriesRef.current[id] || emptySeries();
    const pg = pgRef.current[id] || emptyPg();
    const lastT = s.t.length ? s.t[s.t.length - 1] : -1;
    const span = Math.max(lastT, meta[id]?.elapsed ?? 0, 0);
    return { id, i, color: RUN_COLORS[i], s, pg, span, start: epoch(meta[id]?.startUtc), m: meta[id] || {} };
  });
  const haveStarts = per.every((r) => Number.isFinite(r.start));
  const t0 = haveStarts ? Math.min(...per.map((r) => r.start)) : NaN;
  const laid = per.map((r) => {
    const offset = haveStarts ? Math.round(r.start - t0) : 0;
    return { ...r, offset, end: offset + r.span, plannedEnd: offset + Math.max(r.m.budget || 0, r.span) };
  });
  const unifiedLen = Math.max(1, ...laid.map((r) => r.end + 1));
  const xMax = Math.max(unifiedLen, ...laid.map((r) => r.plannedEnd)) || undefined;
  const xs = Array.from({ length: unifiedLen }, (_, k) => k);
  const short = (r: typeof laid[number]) => r.m.label || r.id.slice(-8);

  const mk = (get: (s: Series) => number[]) =>
    laid.map((r) => ({ label: short(r), values: aligned(unifiedLen, r.offset, r.s.t, get(r.s)), stroke: r.color }));
  const mkPg = (get: (p: PgSeries) => number[]) =>
    laid.map((r) => ({ label: short(r), values: aligned(unifiedLen, r.offset, r.pg.t, get(r.pg)), stroke: r.color }));

  // ── overlap detection (N-way) ─────────────────────────────────────────
  const byStart = [...laid].sort((a, b) => a.offset - b.offset);
  const firstStart = byStart[0], lastStart = byStart[byStart.length - 1];
  const allStart = Math.max(...laid.map((r) => r.offset));   // all running from here
  const allEnd = Math.min(...laid.map((r) => r.end));        // until the first one ends
  const allOverlap = Math.max(0, allEnd - allStart);
  const anyLive = laid.some((r) => r.m.live);
  const allTerminal = laid.every((r) => r.m.live === false);

  const startMsg = !haveStarts ? "waiting for every run to report its start time…"
    : lastStart.offset === firstStart.offset ? `All ${laid.length} runs started together.`
      : `${short(firstStart)} started first; ${short(lastStart)} started ${hms(lastStart.offset - firstStart.offset)} later.`;
  const overlapMsg = !haveStarts ? ""
    : allOverlap > 0 ? `All ${laid.length} overlapped for ${hms(allOverlap)}${anyLive ? " and counting" : ""}.`
      : "No window where all runs ran at once.";
  const finishMsg = allTerminal
    ? (() => { const f = [...laid].sort((a, b) => a.end - b.end); return `${short(f[0])} finished first; ${short(f[f.length - 1])} last (${hms(f[f.length - 1].end - f[0].end)} apart).`; })()
    : "";

  const last = (arr: number[]) => (arr.length ? arr[arr.length - 1] : null);
  const peak = (arr: number[]) => (arr.length ? Math.max(...arr) : null);
  const anyData = laid.some((r) => r.s.t.length > 0);
  const anyPg = laid.some((r) => r.pg.t.length > 0);
  const notSoak = laid.some((r) => r.m.mode && r.m.mode !== "soak");

  const KPIS: { label: string; get: (s: Series) => number | null; fmt: (n: number | null) => string; lowerBetter: boolean }[] = [
    { label: "Current TPS", get: (s) => last(s.tps), fmt: fmtInt, lowerBetter: false },
    { label: "Peak TPS", get: (s) => peak(s.tps), fmt: fmtInt, lowerBetter: false },
    { label: "Current p99 (ms)", get: (s) => last(s.p99), fmt: fmtNum, lowerBetter: true },
    { label: "Current QPS", get: (s) => last(s.qps), fmt: fmtInt, lowerBetter: false },
  ];

  return (
    <>
      <div className="toolbar">
        <div>
          <h1>Live compare</h1>
          <div className="subtle mono" style={{ fontSize: 12 }}>{ids.join("  ·  ")}</div>
        </div>
        <div className="spacer" />
        <Link className="btn" to="/compare">← Compare</Link>
      </div>

      {notSoak && <div className="banner-warn">Live compare aligns runs on wall-clock time and is designed for soaks; a sweep's per-level timeline won't line up cleanly.</div>}

      <div className="cmp-heads" style={{ gridTemplateColumns: `repeat(${Math.min(laid.length, 3)}, 1fr)` }}>
        {laid.map((r) => (
          <div key={r.id} className="cmp-head" style={{ borderLeftColor: r.color }}>
            <span className="dot" style={{ background: r.color }} />
            <b>{short(r)}</b>
            <span className={`badge ${r.m.status || ""}`}>{r.m.status || (r.m.live ? "running" : "—")}</span>
            <span className="subtle mono" style={{ fontSize: 12 }}>{r.m.mode || "—"} · {hms(r.span)}{r.m.budget ? ` / ${hms(r.m.budget)}` : ""}</span>
            <Link className="subtle" to={`/runs/${r.id}`} style={{ fontSize: 12, marginLeft: "auto" }}>open ↗</Link>
          </div>
        ))}
      </div>

      <div className="card overlap">
        <div className="card-head"><h2>Overlap</h2></div>
        <div>{startMsg}</div>
        {overlapMsg && <div>{overlapMsg}</div>}
        {finishMsg && <div>{finishMsg}</div>}
        {haveStarts && xMax && (
          <div className="tl-strip" style={{ marginTop: 10 }}>
            {allOverlap > 0 && (
              <div className="tl-overlap" style={{ left: `${(allStart / xMax) * 100}%`, width: `${(allOverlap / xMax) * 100}%` }} />
            )}
            {laid.map((r) => (
              <div key={r.id} className="tl-row">
                <span className="tl-label mono">{short(r)}</span>
                <div className="tl-track">
                  <div className="tl-bar" style={{ left: `${(r.offset / xMax) * 100}%`, width: `${(Math.max(1, r.end - r.offset) / xMax) * 100}%`, background: r.color, opacity: r.m.live ? 0.9 : 0.5 }} />
                </div>
              </div>
            ))}
            <div className="tl-axis"><span>0:00</span><span>{hms(xMax)}</span></div>
          </div>
        )}
      </div>

      {/* live KPI comparison — every run's value, best one marked */}
      <div className="grid2">
        {KPIS.map(({ label, get, fmt, lowerBetter }) => {
          const vals = laid.map((r) => get(r.s));
          const defined = vals.filter((v): v is number => v != null);
          const best = defined.length ? (lowerBetter ? Math.min(...defined) : Math.max(...defined)) : null;
          return (
            <div className="card kpi-compare" key={label}>
              <div className="label">{label}</div>
              <div className="cmp-vals">
                {laid.map((r, i) => {
                  const v = vals[i];
                  const isBest = v != null && best != null && v === best && defined.length > 1;
                  return (
                    <span key={r.id} title={short(r)} className={isBest ? "best" : ""} style={{ color: r.color }}>
                      {isBest ? "★ " : ""}{fmt(v)}{i < laid.length - 1 ? <span className="subtle sep"> · </span> : null}
                    </span>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>

      {!anyData ? (
        <div className="card"><div className="subtle mono">waiting for live samples…</div></div>
      ) : (
        <>
          <div className="card">
            <LiveChart title="Throughput — TPS (aligned on real time)" xs={xs} xMax={xMax} xFormat={hms}
              yFormat={(v) => fmtCompact(v)} series={mk((s) => s.tps)} />
          </div>
          <div className="card">
            <LiveChart title="QPS (aligned on real time)" xs={xs} xMax={xMax} xFormat={hms}
              yFormat={(v) => fmtCompact(v)} series={mk((s) => s.qps)} />
          </div>
          <div className="grid2">
            <div className="card">
              <LiveChart title="p99 latency (ms)" xs={xs} xMax={xMax} xFormat={hms} height={200}
                yFormat={(v) => fmtCompact(v)} series={mk((s) => s.p99)} />
            </div>
            <div className="card">
              <LiveChart title="Errors (per second)" xs={xs} xMax={xMax} xFormat={hms} height={200}
                yFormat={(v) => fmtCompact(v)} series={mk((s) => s.err)} />
            </div>
          </div>
        </>
      )}

      {anyPg && (
        <>
          <div className="section-label">PostgreSQL (engine-side) <span className="subtle">— scoped to each target DB; sampled every ~5s, so lines connect across the gaps</span></div>
          <div className="grid2">
            <div className="card">
              <LiveChart title="Cache hit % (interval)" xs={xs} xMax={xMax} xFormat={hms} height={200} spanGaps
                yFormat={(v) => `${Math.round(v)}`} series={mkPg((p) => p.cacheHit)} />
            </div>
            <div className="card">
              <LiveChart title="Lock contention — blocked queries" xs={xs} xMax={xMax} xFormat={hms} height={200} spanGaps
                yFormat={(v) => fmtCompact(v)} series={mkPg((p) => p.blockedQueries)} />
            </div>
            <div className="card">
              <LiveChart title="Deadlocks (per second)" xs={xs} xMax={xMax} xFormat={hms} height={200} spanGaps
                yFormat={(v) => fmtCompact(v)} series={mkPg((p) => p.deadlocksS)} />
            </div>
            <div className="card">
              <LiveChart title="WAL throughput (MB/s)" xs={xs} xMax={xMax} xFormat={hms} height={200} spanGaps
                yFormat={(v) => fmtCompact(v)} series={mkPg((p) => p.walMbs)} />
            </div>
            <div className="card">
              <LiveChart title="Active connections" xs={xs} xMax={xMax} xFormat={hms} height={200} spanGaps
                yFormat={(v) => fmtCompact(v)} series={mkPg((p) => p.active)} />
            </div>
          </div>
        </>
      )}

      <p className="subtle" style={{ fontSize: 12 }}>
        x-axis is elapsed since the earliest run started; each line breaks where that run wasn't producing samples.
        All streams are live — a run that starts, stops, or gaps shows up against the others in real time.
      </p>
    </>
  );
}
