import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { OpsRunDetail } from "../types";
import { LiveChart } from "../components/LiveChart";
import { LogConsole } from "../components/LogConsole";
import { openOpsStream, OpsEvent, OpsStatus } from "../lib/sse";

const TERMINAL = ["complete", "warning", "failed", "canceled", "aborted"];

// which sampler CSVs get a live chart, and which column to plot
const CHARTS: Record<string, { col: string; label: string; stroke: string }> = {
  "queue_depth.csv": { col: "ready_files", label: ".ready files", stroke: "#e06b5d" },
  "archiver.csv": { col: "archived_count", label: "archived_count", stroke: "#4c9f70" },
  "monitor.csv": { col: "archive_queue", label: "archive queue", stroke: "#e06b5d" },
};

interface CsvBuf { header: string[]; rows: string[][] }

export function OpsRunView() {
  const { opRunId } = useParams();
  const [detail, setDetail] = useState<OpsRunDetail | null>(null);
  const [events, setEvents] = useState<OpsEvent[]>([]);
  const [status, setStatus] = useState<OpsStatus>({});
  const [log, setLog] = useState("");
  const [csvs, setCsvs] = useState<Record<string, CsvBuf>>({});
  const [timeline, setTimeline] = useState<string>("");
  const [tab, setTab] = useState<"live" | "timeline" | "report">("live");
  const esRef = useRef<EventSource | null>(null);

  function loadDetail() {
    api.get<OpsRunDetail>(`/api/ops/runs/${opRunId}`).then((d) => {
      setDetail(d);
      if (TERMINAL.includes(d.meta.status) && d.files.includes("TIMELINE.txt")) {
        api.raw(`/api/ops/timeline/${opRunId}`).then((r) => r.text()).then(setTimeline)
          .catch(() => undefined);
      }
    }).catch(() => undefined);
  }

  useEffect(() => {
    loadDetail();
    setEvents([]); setLog(""); setCsvs({});
    const es = openOpsStream(opRunId!, {
      onLog: (chunk) => setLog((prev) => (prev + chunk).slice(-200_000)),
      onEvents: (b) => setEvents((prev) => b.offset === 0 ? b.items : [...prev, ...b.items]),
      onStatus: setStatus,
      onCsv: (b) => setCsvs((prev) => {
        const cur = b.offset === 0 || !prev[b.file]
          ? { header: b.header.split(","), rows: [] as string[][] }
          : prev[b.file];
        return { ...prev, [b.file]: { header: cur.header, rows: [...cur.rows, ...b.rows.map((r) => r.split(","))] } };
      }),
      onDone: () => loadDetail(),
    });
    esRef.current = es;
    return () => es.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opRunId]);

  const meta = detail?.meta ?? {};
  const headline: Record<string, any> = meta.headline ?? {};
  const terminal = TERMINAL.includes(meta.status);

  const charts = useMemo(() => Object.entries(csvs)
    .filter(([name, buf]) => CHARTS[name] && buf.rows.length > 1)
    .map(([name, buf]) => {
      const spec = CHARTS[name];
      const ti = buf.header.indexOf("epoch_s");
      const vi = buf.header.indexOf(spec.col);
      if (ti < 0 || vi < 0) return null;
      const t0 = Number(buf.rows[0][ti]);
      const xs = buf.rows.map((r) => Number(r[ti]) - t0);
      const values = buf.rows.map((r) => Number(r[vi]));
      return { name, xs, series: [{ label: spec.label, values, stroke: spec.stroke }] };
    }).filter(Boolean) as { name: string; xs: number[]; series: any[] }[], [csvs]);

  const kpis: [string, string][] = [];
  if (meta.op === "scenario") {
    kpis.push(["case", String(headline.case ?? "—")]);
    kpis.push(["downtime", headline.downtime_ms != null ? `${(headline.downtime_ms / 1000).toFixed(1)}s` : "—"]);
    kpis.push(["classification", headline.flip == null ? "—" : headline.flip ? "ELECTION" : "restart in place"]);
    kpis.push(["TL", headline.tl_before != null ? `${headline.tl_before} → ${headline.tl_after}` : "—"]);
    kpis.push(["backoff tail", headline.backoff_tail_ms ? `+${(headline.backoff_tail_ms / 1000).toFixed(1)}s` : "0"]);
    kpis.push(["full HA", headline.full_ha_recovery_s != null ? `${headline.full_ha_recovery_s}s` : "—"]);
  } else if (meta.op === "backup") {
    kpis.push(["label", String(headline.label ?? "—")]);
    kpis.push(["type/path", `${headline.type ?? "?"} / ${headline.path ?? "?"}`]);
    kpis.push(["source", `${headline.source_role ?? "?"}`]);
    kpis.push(["duration", headline.duration_s != null ? `${headline.duration_s}s` : "—"]);
    kpis.push(["peak queue", String(headline.peak_archive_queue ?? "—")]);
  } else if (meta.op === "cr-apply") {
    kpis.push(["action", String(headline.action ?? "—")]);
    kpis.push(["changed", String(Object.keys(headline.changed ?? {}).length)]);
    kpis.push(["verified", headline.dry_run ? "dry-run" : String(headline.verified ?? "—")]);
    if (headline.pending_restart?.length) kpis.push(["⚠ pending restart", headline.pending_restart.join(", ")]);
  } else if (meta.op === "monitor") {
    kpis.push(["cycles", String(headline.cycles ?? status.cycles ?? "—")]);
    kpis.push(["leader", String(status.leader ?? "—")]);
    kpis.push(["ready", String(status.ready ?? "—")]);
  }

  return (
    <>
      <div className="toolbar">
        <h1 className="mono" style={{ fontSize: 18 }}>{opRunId}</h1>
        <span className={`badge ${terminal ? meta.status : "running"}`}>{meta.status ?? "…"}</span>
        <div className="spacer" />
        <Link className="btn" to="/ops/runs">← ops runs</Link>
      </div>

      {!terminal && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="kpi-row">
            <div className="kpi"><div className="label">phase</div><div className="value mono">{String(status.phase ?? "…")}</div></div>
            <div className="kpi"><div className="label">leader</div><div className="value mono" style={{ fontSize: 13 }}>{String(status.leader ?? "…")}</div></div>
            <div className="kpi"><div className="label">members ready</div><div className="value mono">{String(status.ready ?? "…")}</div></div>
            <div className="kpi"><div className="label">timeline</div><div className="value mono">{String(status.timeline ?? "…")}</div></div>
          </div>
        </div>
      )}
      {kpis.length > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="kpi-row">
            {kpis.map(([l, v]) => (
              <div className="kpi" key={l}><div className="label">{l}</div>
                <div className="value mono" style={{ fontSize: v.length > 16 ? 13 : undefined }}>{v}</div></div>
            ))}
          </div>
        </div>
      )}

      <div className="tabs" style={{ marginBottom: 12 }}>
        <button className={tab === "live" ? "primary" : ""} onClick={() => setTab("live")}>Live</button>{" "}
        {timeline && <button className={tab === "timeline" ? "primary" : ""} onClick={() => setTab("timeline")}>Timeline</button>}{" "}
        {terminal && detail?.files.includes("report.html") !== false && meta.op !== "monitor" && (
          <button className={tab === "report" ? "primary" : ""} onClick={() => setTab("report")}>Report</button>)}
      </div>

      {tab === "timeline" && (
        <div className="card"><pre className="mono" style={{ fontSize: 12, overflow: "auto" }}>{timeline}</pre></div>
      )}
      {tab === "report" && (
        <div className="card" style={{ padding: 0 }}>
          <iframe title="ops report" src={`/ops/runs/${opRunId}/report`}
                  style={{ width: "100%", height: "80vh", border: 0, background: "#fff", borderRadius: 8 }} />
        </div>
      )}
      {tab === "live" && (
        <>
          {charts.map((c) => (
            <div className="card" key={c.name} style={{ marginBottom: 14 }}>
              <LiveChart title={c.name.replace(".csv", "")} xs={c.xs} series={c.series} height={200} />
            </div>
          ))}
          <div className="grid2">
            <div className="card">
              <div className="card-head"><h2>Event feed</h2></div>
              <table>
                <thead><tr><th>utc</th><th>type</th><th>event</th></tr></thead>
                <tbody>
                  {events.length === 0 ? <tr><td colSpan={3} className="empty">no events yet</td></tr>
                    : [...events].reverse().map((e, i) => (
                      <tr key={i}>
                        <td className="mono" style={{ whiteSpace: "nowrap" }}>{e.ts_utc.slice(11, 19)}</td>
                        <td><span className={`badge ${e.type === "fire" ? "failed" : "ok"}`}>{e.type}</span></td>
                        <td><strong>{e.label}</strong>{e.note && <div className="subtle" style={{ fontSize: 12 }}>{e.note}</div>}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
            <LogConsole text={log} />
          </div>
          {terminal && detail && (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="card-head"><h2>Artifacts</h2></div>
              <p className="mono" style={{ fontSize: 12 }}>
                {detail.files.map((f) => (
                  <a key={f} href={`/ops/runs/${opRunId}/file?name=${encodeURIComponent(f)}`}
                     style={{ marginRight: 12 }}>{f}</a>
                ))}
              </p>
              {detail.raw_files.length > 0 && (
                <p className="mono subtle" style={{ fontSize: 12 }}>
                  raw/: {detail.raw_files.map((f) => (
                    <a key={f} href={`/ops/runs/${opRunId}/file?name=${encodeURIComponent("raw/" + f)}`}
                       style={{ marginRight: 12 }}>{f}</a>
                  ))}
                </p>
              )}
            </div>
          )}
        </>
      )}
    </>
  );
}
