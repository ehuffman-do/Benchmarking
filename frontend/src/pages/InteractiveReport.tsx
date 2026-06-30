import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { LiveChart } from "../components/LiveChart";
import { AnnotatableChart, type Marker } from "../components/AnnotatableChart";
import { fmtCompact, fmtInt, fmtNum } from "../lib/format";
import type { Me } from "../types";

interface Level {
  threads: number;
  rep?: number;
  qps_avg: number | null;
  tps_avg: number | null;
  lat_p50?: number | null;
  lat_p95?: number | null;
  lat_p99?: number | null;
  errors?: number | null;
}
interface Setting { name: string; setting: string; unit: string; source: string; }
interface SummaryResp {
  mode: string;
  pg: boolean;
  manifest: Record<string, unknown> & { preflight?: Record<string, unknown> };
  summary: Record<string, unknown>;
  pg_settings?: { key: Setting[]; all: Setting[] } | null;
}

const C = { qps: "#6ea8fe", tps: "#2dd4bf", p50: "#3fb950", p95: "#e0a93b", p99: "#f85149",
  read: "#2e8b57", write: "#b9770e", other: "#7d5bbe", err: "#f85149", reconn: "#e0a93b" };

// Decimated soak series + markers from /api/runs/:id/timeseries (live or finished).
interface TimeseriesResp {
  available: boolean;
  terminal?: boolean;
  horizon_s?: number;
  t?: number[];
  tps?: (number | null)[]; qps?: (number | null)[]; lat_p99?: (number | null)[];
  err_s?: (number | null)[]; reconn_s?: (number | null)[];
  qps_r?: (number | null)[]; qps_w?: (number | null)[]; qps_o?: (number | null)[];
  markers?: Marker[];
  baseline_tps?: number | null;
}

export function InteractiveReport({ runId, me }: { runId: string; me: Me }) {
  const [data, setData] = useState<SummaryResp | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<SummaryResp>(`/api/runs/${runId}/summary`).then(setData).catch((e) => setErr(e.message));
  }, [runId]);

  if (err) return <div className="banner-err">{err}</div>;
  if (!data) return <div className="subtle mono" style={{ padding: 20 }}>loading…</div>;

  const pf = (data.manifest.preflight || {}) as Record<string, string>;
  const prov: [string, string][] = [
    ["Edition / size", `${data.manifest.edition || "—"} · ${data.manifest.tshirt_size || "—"}`],
    ["Server", String(pf.server_version || "—")],
    ["max_connections", String(pf.max_connections || "—")],
    ["sysbench", String(pf.sysbench_version || "—")],
    ["psql", String(pf.psql_version || "—")],
    ["tpcc", String(pf.tpcc_git_sha || "—")],
  ];
  const dataset = (pf.dataset && typeof pf.dataset === "object")
    ? (pf.dataset as unknown as { detail?: string }).detail : undefined;
  if (dataset) prov.push(["Dataset", dataset]);

  return data.mode === "soak"
    ? <SoakReport summary={data.summary} prov={prov} manifest={data.manifest} runId={runId} me={me} />
    : <SweepReport summary={data.summary} prov={prov} pgSettings={data.pg_settings ?? null} />;
}

function PgSettings({ s }: { s: { key: Setting[]; all: Setting[] } | null }) {
  if (!s || (!s.key.length && !s.all.length)) return null;
  const fmt = (r: Setting) => `${r.setting}${r.unit ? " " + r.unit : ""}`;
  const Row = (r: Setting) => (
    <tr key={r.name}>
      <td className="mono">{r.name}</td><td className="mono num">{fmt(r)}</td>
      <td className="subtle">{r.source}</td>
    </tr>
  );
  return (
    <div className="card">
      <div className="card-head"><h2>Database configuration</h2></div>
      <table>
        <thead><tr><th>Setting</th><th className="num">Value</th><th>Source</th></tr></thead>
        <tbody>{(s.key.length ? s.key : s.all).map(Row)}</tbody>
      </table>
      {s.all.length > s.key.length && (
        <details style={{ marginTop: 8 }}>
          <summary className="subtle" style={{ cursor: "pointer" }}>Show all {s.all.length} settings</summary>
          <table style={{ marginTop: 8 }}>
            <thead><tr><th>Setting</th><th className="num">Value</th><th>Source</th></tr></thead>
            <tbody>{s.all.map(Row)}</tbody>
          </table>
        </details>
      )}
    </div>
  );
}

function Provenance({ prov }: { prov: [string, string][] }) {
  return (
    <div className="card">
      <div className="card-head"><h2>Provenance</h2></div>
      <table><tbody>
        {prov.map(([k, v]) => (
          <tr key={k}><td style={{ width: 180, color: "var(--muted)" }}>{k}</td><td className="mono">{v}</td></tr>
        ))}
      </tbody></table>
    </div>
  );
}

function SweepReport({ summary, prov, pgSettings }:
  { summary: Record<string, unknown>; prov: [string, string][];
    pgSettings: { key: Setting[]; all: Setting[] } | null }) {
  const levels = ((summary.levels as Level[]) || []).filter((l) => l.qps_avg != null);
  // aggregate by thread count (mean across reps)
  const byThreads = new Map<number, Level[]>();
  for (const l of levels) (byThreads.get(l.threads) ?? byThreads.set(l.threads, []).get(l.threads)!).push(l);
  const threads = [...byThreads.keys()].sort((a, b) => a - b);
  const mean = (arr: Level[], k: keyof Level) => {
    const xs = arr.map((l) => l[k] as number).filter((n) => n != null && !Number.isNaN(n));
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;
  };
  const qps = threads.map((t) => mean(byThreads.get(t)!, "qps_avg"));
  const tps = threads.map((t) => mean(byThreads.get(t)!, "tps_avg"));
  const p50 = threads.map((t) => mean(byThreads.get(t)!, "lat_p50"));
  const p95 = threads.map((t) => mean(byThreads.get(t)!, "lat_p95"));
  const p99 = threads.map((t) => mean(byThreads.get(t)!, "lat_p99"));
  // index of max QPS without a spread (large ladders) and without -1 on empty
  const peakI = qps.length ? qps.reduce((bi, v, i, a) => (v > a[bi] ? i : bi), 0) : -1;
  const xt = (v: number) => `${v}t`;

  return (
    <>
      <div className="kpi-row" style={{ marginBottom: 16 }}>
        <div className="kpi"><div className="label">Peak QPS</div><div className="value">{qps.length ? fmtInt(qps[peakI]) : "—"}</div></div>
        <div className="kpi"><div className="label">Peak TPS</div><div className="value">{tps.length ? fmtInt(tps[peakI]) : "—"}</div></div>
        <div className="kpi"><div className="label">p99 @ peak</div><div className="value">{p99.length ? fmtInt(p99[peakI]) : "—"}<small> ms</small></div></div>
        <div className="kpi"><div className="label">Threads @ peak</div><div className="value">{threads.length ? threads[peakI] : "—"}</div></div>
      </div>
      <div className="grid2">
        <div className="card">
          <LiveChart title="QPS / TPS vs threads" xs={threads} xFormat={xt} yFormat={(v) => fmtCompact(v)}
            series={[{ label: "QPS", values: qps, stroke: C.qps }, { label: "TPS", values: tps, stroke: C.tps, scale: "y2" }]} />
        </div>
        <div className="card">
          <LiveChart title="Latency vs threads (ms)" xs={threads} xFormat={xt} yFormat={(v) => fmtCompact(v)}
            series={[
              { label: "p50", values: p50, stroke: C.p50 },
              { label: "p95", values: p95, stroke: C.p95 },
              { label: "p99", values: p99, stroke: C.p99 },
            ]} />
        </div>
      </div>
      <div className="card">
        <div className="card-head"><h2>Per-level</h2></div>
        <table>
          <thead><tr><th className="num">Threads</th><th className="num">QPS</th><th className="num">TPS</th>
            <th className="num">p50</th><th className="num">p95</th><th className="num">p99</th></tr></thead>
          <tbody>
            {threads.map((t, i) => (
              <tr key={t}>
                <td className="num">{t}</td><td className="num">{fmtInt(qps[i])}</td><td className="num">{fmtInt(tps[i])}</td>
                <td className="num">{fmtNum(p50[i])}</td><td className="num">{fmtNum(p95[i])}</td><td className="num">{fmtNum(p99[i])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <PgSettings s={pgSettings} />
      <Provenance prov={prov} />
    </>
  );
}

// h:mm:ss / m:ss axis + popup labels.
function hms(v: number): string {
  const s = Math.max(0, Math.round(v));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`;
}
const MARK_HUMAN: Record<string, string> = {
  failover: "Failover", scale_up: "Scale up", scale_down: "Scale down", note: "Note",
};
// null gaps -> NaN so AnnotatableChart's clean() renders them as breaks, not zeros.
const numArr = (a?: (number | null)[]): number[] => (a ?? []).map((v) => (v == null ? NaN : v));

function SoakReport({ summary, prov, manifest, runId, me }: {
  summary: Record<string, unknown>; prov: [string, string][];
  manifest: Record<string, unknown>; runId: string; me: Me;
}) {
  const events = (summary.events as Array<Record<string, unknown>>) || [];
  const rp = (summary.run_profile || {}) as Record<string, unknown>;
  const tps = (rp.tps || {}) as Record<string, number>;
  const lat = (rp.latency_ms || {}) as Record<string, number | null>;
  const m = (e: Record<string, unknown>) => (e.metrics || {}) as Record<string, number>;

  const canStamp = me.role === "operator" || me.role === "admin";
  const [ts, setTs] = useState<TimeseriesResp | null>(null);
  const [annotate, setAnnotate] = useState(false);
  const [pending, setPending] = useState<{ t: number; x: number; y: number } | null>(null);
  const [busy, setBusy] = useState(false);

  const fetchTs = useCallback(() => {
    api.get<TimeseriesResp>(`/api/runs/${runId}/timeseries`).then(setTs).catch(() => {});
  }, [runId]);
  useEffect(() => { fetchTs(); }, [fetchTs]);
  // Poll while the run is still in flight so freshly-stamped marks and new data
  // land on the chart without a manual refresh; stop the moment it's terminal.
  // Gate on terminal (not `available`) so a just-started soak with no samples yet
  // keeps polling until its first second arrives.
  const live = ts != null && ts.terminal === false;
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(fetchTs, 5000);
    return () => window.clearInterval(id);
  }, [live, fetchTs]);

  const onStamp = useCallback((t: number, x: number, y: number) => {
    setPending({ t, x, y });
  }, []);

  async function stamp(type: string, label: string) {
    if (!pending) return;
    setBusy(true);
    try {
      await api.post(`/api/runs/${runId}/mark`, { type, at_s: Math.round(pending.t), label });
      setPending(null);
      fetchTs();   // re-pull so the new marker shows immediately
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function choose(type: string) {
    if (type === "note") {
      const label = window.prompt("Event note (optional):", "");
      if (label === null) return;          // cancelled the prompt
      stamp("note", label);
    } else {
      stamp(type, MARK_HUMAN[type]);
    }
  }

  const xs = ts?.t ?? [];
  const hasSeries = xs.length > 0;
  const markers = ts?.markers ?? [];
  const budgetS = Number((manifest.soak as Record<string, unknown> | undefined)?.target_duration_s) || 0;
  const xMax = Math.max(ts?.horizon_s ?? 0, budgetS) || undefined;
  const stampProps = canStamp
    ? { annotate, onStamp }
    : {};

  return (
    <>
      {typeof summary.tldr === "string" && summary.tldr && (
        <div className="card" style={{ marginBottom: 16, fontWeight: 600 }}>{summary.tldr}</div>
      )}
      <div className="kpi-row" style={{ marginBottom: 16 }}>
        <div className="kpi"><div className="label">Median TPS</div><div className="value">{tps.median != null ? fmtInt(tps.median) : "—"}</div></div>
        <div className="kpi"><div className="label">p99 latency (run)</div><div className="value">{lat.p99 != null ? fmtNum(lat.p99) : "—"}<small> ms</small></div></div>
        <div className="kpi"><div className="label">TPS variability</div><div className="value">{tps.cov_pct != null ? fmtNum(tps.cov_pct) : "—"}<small> % CoV</small></div></div>
        <div className="kpi"><div className="label">Zero / gap</div><div className="value">{rp.zero_or_gap_seconds != null ? fmtInt(rp.zero_or_gap_seconds as number) : "—"}<small> s</small></div></div>
        <div className="kpi"><div className="label">Coverage</div><div className="value">{summary.coverage_pct != null ? fmtNum(summary.coverage_pct as number) : "—"}<small> %</small></div></div>
        <div className="kpi"><div className="label">Events</div><div className="value">{events.length}</div></div>
      </div>

      {hasSeries && (
        <div className="card">
          <div className="card-head">
            <h2>Throughput over the full run <span className="subtle">— events marked</span></h2>
            <div className="spacer" />
            {canStamp && (
              <button className={`btn${annotate ? " primary" : ""}`} onClick={() => { setAnnotate((a) => !a); setPending(null); }}>
                {annotate ? "✎ Annotating — click a point" : "✎ Annotate"}
              </button>
            )}
          </div>
          {annotate && (
            <div className="subtle" style={{ margin: "0 0 8px", fontSize: 12 }}>
              Click any point on the charts below to stamp an event there{ts?.terminal === false ? " (the run is still live)" : ""}. Press “✎ Annotating” again to stop.
            </div>
          )}
          <AnnotatableChart title="Throughput (TPS / QPS)" xs={xs} xMax={xMax} xFormat={hms}
            yFormat={(v) => fmtCompact(v)} markers={markers} baseline={ts?.baseline_tps ?? null} {...stampProps}
            series={[{ label: "TPS", values: numArr(ts?.tps), stroke: C.tps },
                     { label: "QPS", values: numArr(ts?.qps), stroke: C.qps, scale: "y2" }]} />
        </div>
      )}

      {hasSeries && (
        <div className="grid2">
          <div className="card">
            <AnnotatableChart title="QPS — read / write / other" xs={xs} xMax={xMax} xFormat={hms}
              yFormat={(v) => fmtCompact(v)} markers={markers} {...stampProps} height={200}
              series={[{ label: "read", values: numArr(ts?.qps_r), stroke: C.read },
                       { label: "write", values: numArr(ts?.qps_w), stroke: C.write },
                       { label: "other", values: numArr(ts?.qps_o), stroke: C.other }]} />
          </div>
          <div className="card">
            <AnnotatableChart title="p99 latency (ms) — per second" xs={xs} xMax={xMax} xFormat={hms}
              yFormat={(v) => fmtCompact(v)} markers={markers} {...stampProps} height={200}
              series={[{ label: "p99 ms", values: numArr(ts?.lat_p99), stroke: C.p99 }]} />
          </div>
        </div>
      )}

      {hasSeries && (
        <div className="card">
          <AnnotatableChart title="Errors & reconnects (per second)" xs={xs} xMax={xMax} xFormat={hms}
            yFormat={(v) => fmtCompact(v)} markers={markers} {...stampProps} height={180}
            series={[{ label: "errors/s", values: numArr(ts?.err_s), stroke: C.err },
                     { label: "reconnects/s", values: numArr(ts?.reconn_s), stroke: C.reconn, scale: "y2" }]} />
        </div>
      )}

      <div className="card">
        <div className="card-head"><h2>Disruption events <span className="subtle">— operator-marked</span></h2></div>
        {events.length === 0 ? <div className="empty">No events marked. The run profile and the timeline above characterize this run; mark events with the Annotate button.</div> : (
          <table>
            <thead><tr><th>At</th><th>Type</th><th>Label</th><th className="num">Downtime</th>
              <th className="num">TTR (95%)</th><th className="num">Full re-warm</th><th className="num">Min TPS</th></tr></thead>
            <tbody>
              {events.map((e, i) => (
                <tr key={i}>
                  <td className="num">{String(e.at_s ?? "—")}s</td><td>{String(e.type ?? "—")}</td><td>{String(e.label ?? "")}</td>
                  <td className="num">{m(e).hard_downtime_s != null ? `${fmtNum(m(e).hard_downtime_s)}s` : "—"}</td>
                  <td className="num">{m(e).ttr_s != null ? `${fmtNum(m(e).ttr_s)}s` : "—"}</td>
                  <td className="num">{m(e).full_recovery_s != null ? `${fmtNum(m(e).full_recovery_s)}s` : "—"}</td>
                  <td className="num">{m(e).min_tps != null ? fmtInt(m(e).min_tps) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="subtle" style={{ marginTop: 8, fontSize: 12 }}>
          Per-event zoom charts (full 1 Hz) are in the Classic report; the live cockpit streams the same timeline.
        </p>
      </div>
      <Provenance prov={prov} />

      {pending && (
        <>
          <div className="stamp-backdrop" onClick={() => setPending(null)} />
          <div className="stamp-pop" role="menu"
               style={{ left: Math.min(pending.x, window.innerWidth - 200), top: Math.min(pending.y, window.innerHeight - 180) }}>
            <div className="stamp-head">Stamp event at <b>{hms(pending.t)}</b></div>
            {(["failover", "scale_up", "scale_down", "note"]).map((tp) => (
              <button key={tp} className="stamp-item" disabled={busy} onClick={() => choose(tp)}>
                {MARK_HUMAN[tp]}
              </button>
            ))}
            <button className="stamp-item cancel" disabled={busy} onClick={() => setPending(null)}>✕ Cancel</button>
          </div>
        </>
      )}
    </>
  );
}
