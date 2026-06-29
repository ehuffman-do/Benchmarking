import { useEffect, useState } from "react";
import { api } from "../api";
import { LiveChart } from "../components/LiveChart";
import { fmtInt, fmtNum } from "../lib/format";

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
interface SummaryResp {
  mode: string;
  pg: boolean;
  manifest: Record<string, unknown> & { preflight?: Record<string, unknown> };
  summary: Record<string, unknown>;
}

const C = { qps: "#6ea8fe", tps: "#2dd4bf", p50: "#3fb950", p95: "#e0a93b", p99: "#f85149" };

export function InteractiveReport({ runId }: { runId: string }) {
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
    ? <SoakReport summary={data.summary} prov={prov} />
    : <SweepReport summary={data.summary} prov={prov} />;
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

function SweepReport({ summary, prov }: { summary: Record<string, unknown>; prov: [string, string][] }) {
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
          <LiveChart title="QPS / TPS vs threads" xs={threads} xFormat={xt} yFormat={(v) => fmtInt(v)}
            series={[{ label: "QPS", values: qps, stroke: C.qps }, { label: "TPS", values: tps, stroke: C.tps, scale: "y2" }]} />
        </div>
        <div className="card">
          <LiveChart title="Latency vs threads (ms)" xs={threads} xFormat={xt} yFormat={(v) => fmtInt(v)}
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
      <Provenance prov={prov} />
    </>
  );
}

function SoakReport({ summary, prov }: { summary: Record<string, unknown>; prov: [string, string][] }) {
  const events = (summary.events as Array<Record<string, unknown>>) || [];
  const detected = (summary.detected as Array<Record<string, unknown>>) || [];
  const rp = (summary.run_profile || {}) as Record<string, unknown>;
  const tps = (rp.tps || {}) as Record<string, number>;
  const lat = (rp.latency_ms || {}) as Record<string, number | null>;
  const m = (e: Record<string, unknown>) => (e.metrics || {}) as Record<string, number>;
  const ev = (o: Record<string, unknown>) => (o.evidence || {}) as Record<string, unknown>;
  return (
    <>
      <div className="kpi-row" style={{ marginBottom: 16 }}>
        <div className="kpi"><div className="label">Median TPS</div><div className="value">{tps.median != null ? fmtInt(tps.median) : "—"}</div></div>
        <div className="kpi"><div className="label">p99 latency (run)</div><div className="value">{lat.p99 != null ? fmtNum(lat.p99) : "—"}<small> ms</small></div></div>
        <div className="kpi"><div className="label">TPS variability</div><div className="value">{tps.cov_pct != null ? fmtNum(tps.cov_pct) : "—"}<small> % CoV</small></div></div>
        <div className="kpi"><div className="label">Zero / gap</div><div className="value">{rp.zero_or_gap_seconds != null ? fmtInt(rp.zero_or_gap_seconds as number) : "—"}<small> s</small></div></div>
        <div className="kpi"><div className="label">Coverage</div><div className="value">{summary.coverage_pct != null ? fmtNum(summary.coverage_pct as number) : "—"}<small> %</small></div></div>
        <div className="kpi"><div className="label">Events</div><div className="value">{events.length}</div></div>
      </div>

      {detected.length > 0 && (
        <div className="card">
          <div className="card-head"><h2>Detected anomalies <span className="subtle">— automatic, unconfirmed</span></h2></div>
          <table>
            <thead><tr><th>Type</th><th className="num">At</th><th className="num">Window (s)</th>
              <th className="num">Confidence</th><th>Evidence</th></tr></thead>
            <tbody>
              {detected.map((c, i) => (
                <tr key={i}>
                  <td>{String(c.type)}</td>
                  <td className="num">{String(c.at_s)}s</td>
                  <td className="num">{(c.end_s as number) - (c.at_s as number) + 1}</td>
                  <td className="num">{Math.round((c.confidence as number) * 100)}%</td>
                  <td className="subtle mono" style={{ fontSize: 12 }}>
                    {Object.entries(ev(c)).map(([k, v]) => `${k}=${v}`).join(", ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card">
        <div className="card-head"><h2>Disruption events <span className="subtle">— confirmed</span></h2></div>
        {events.length === 0 ? <div className="empty">No confirmed events. The profile and detected anomalies above characterize this run.</div> : (
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
          Full per-second timeline and event zooms are in the Classic report and the live cockpit.
        </p>
      </div>
      <Provenance prov={prov} />
    </>
  );
}
