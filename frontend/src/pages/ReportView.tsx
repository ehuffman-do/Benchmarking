import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { Me } from "../types";
import { InteractiveReport } from "./InteractiveReport";

type Tab = "interactive" | "classic";

// Two ways to read a run's report: an interactive in-app view (zoomable charts,
// CSV/print) and the classic self-contained HTML report (offline-portable),
// rendered inline. Available for past or in-flight runs — no download required.
export function ReportView({ me }: { me: Me }) {
  const { runId = "" } = useParams();
  const [tab, setTab] = useState<Tab>("interactive");
  const [bust, setBust] = useState(0);
  const [hasPg, setHasPg] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const src = `/runs/${encodeURIComponent(runId)}/report${bust ? `?regen=1&_=${bust}` : ""}`;

  useEffect(() => {
    api.get<{ pg: boolean }>(`/api/runs/${runId}/summary`).then((d) => setHasPg(d.pg)).catch(() => {});
  }, [runId]);

  function print() {
    if (tab === "classic" && frame.current?.contentWindow) frame.current.contentWindow.print();
    else window.print();
  }

  return (
    <>
      <div className="toolbar no-print">
        <div>
          <h1>Report</h1>
          <div className="subtle mono" style={{ fontSize: 12 }}>{runId}</div>
        </div>
        <div className="spacer" />
        <div className="seg">
          <button className={tab === "interactive" ? "on" : ""} onClick={() => setTab("interactive")}>Interactive</button>
          <button className={tab === "classic" ? "on" : ""} onClick={() => setTab("classic")}>Classic</button>
        </div>
        <Link className="btn" to={`/runs/${runId}`}>← Cockpit</Link>
        <button onClick={print}>Print / PDF</button>
        <details className="menu">
          <summary className="btn">Export CSV ▾</summary>
          <div className="menu-pop">
            <a href={`/runs/${runId}/csv?which=samples`}>Samples (sweep)</a>
            <a href={`/runs/${runId}/csv?which=timeseries`}>Timeseries (soak)</a>
            {hasPg && <a href={`/runs/${runId}/csv?which=pg`}>PostgreSQL metrics</a>}
          </div>
        </details>
        {tab === "classic" && <>
          <button onClick={() => setBust((b) => b + 1)}>Regenerate</button>
          <a className="btn" href={`/runs/${runId}/report/download`}>Download HTML</a>
          <a className="btn" href={`/runs/${runId}/report`} target="_blank" rel="noreferrer">Open raw ↗</a>
        </>}
      </div>

      {tab === "interactive"
        ? <InteractiveReport runId={runId} me={me} />
        : <div className="report-frame"><iframe ref={frame} title={`report ${runId}`} src={src} /></div>}
    </>
  );
}
