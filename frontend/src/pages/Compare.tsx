import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { Run } from "../types";
import { fmtInt, fmtWhen } from "../lib/format";

export function Compare() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [viewing, setViewing] = useState<string[] | null>(null);
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    api.get<Run[]>("/api/runs").then(setRuns).catch((e) => setErr(e.message));
  }, []);

  const filtered = useMemo(() => {
    if (!runs) return [];
    const n = q.trim().toLowerCase();
    return runs.filter((r) => !n || [r.label, r.run_id, r.tags, r.target_host]
      .filter(Boolean).some((s) => String(s).toLowerCase().includes(n)));
  }, [runs, q]);

  function toggle(id: string) {
    const next = new Set(sel);
    next.has(id) ? next.delete(id) : next.add(id);
    setSel(next);
  }

  // Compare is only meaningful within one run type (sweep-vs-sweep or soak-vs-soak).
  const selModes = useMemo(
    () => new Set((runs ?? []).filter((r) => sel.has(r.run_id)).map((r) => r.mode)),
    [runs, sel]);
  const mixed = selModes.size > 1;
  // Live compare overlays 2–6 same-mode runs on a shared real-time axis. Soaks
  // align cleanly (continuous wall-clock); sweeps still overlay but the live
  // view warns that their per-level timelines won't line up exactly. Mixing
  // modes on one axis is meaningless, so require a single mode.
  const canLive = sel.size >= 2 && sel.size <= 6 && selModes.size === 1;
  const liveReason = sel.size < 2 ? "Select 2–6 runs to overlay live"
    : sel.size > 6 ? "Live compare overlays at most 6 runs"
      : mixed ? "Select runs of a single mode (all sweep or all soak)"
        : "Overlay these runs live on a shared real-time axis"
          + ([...selModes][0] !== "soak" ? " (sweeps align approximately)" : "");

  if (viewing) {
    const q = viewing.map(encodeURIComponent).join(",");
    // Print the REPORT inside the iframe, not the console chrome around it — so
    // "Save as PDF" captures the whole comparison, every section, not one screen.
    const printReport = () => {
      const w = iframeRef.current?.contentWindow;
      if (w) { w.focus(); w.print(); } else { window.print(); }
    };
    return (
      <>
        <div className="toolbar no-print">
          <h1>Comparison</h1><div className="spacer" />
          <button onClick={() => setViewing(null)}>← Back to selection</button>
          <a className="btn" href={`/compare/download?runs=${q}`}>⬇ Download report (.html)</a>
          <a className="btn" href={`/compare/view?runs=${q}`} target="_blank" rel="noreferrer">Open in new tab ↗</a>
          <button onClick={printReport}>Print / PDF</button>
        </div>
        <div className="report-frame">
          <iframe ref={iframeRef} title="comparison" src={`/compare/view?runs=${q}`} />
        </div>
      </>
    );
  }

  return (
    <>
      <div className="toolbar">
        <h1>Compare runs</h1><div className="spacer" />
        <span className="subtle">{sel.size} selected</span>
        {mixed && <span className="subtle" style={{ color: "var(--bad, #c0392b)" }}>
          same type only (sweep or soak)</span>}
        <button disabled={!canLive} title={liveReason}
          onClick={() => navigate(`/compare/live?runs=${[...sel].map(encodeURIComponent).join(",")}`)}>
          ▶ Live compare
        </button>
        <a className={`btn ${sel.size < 2 || mixed ? "disabled" : ""}`}
           href={sel.size >= 2 && !mixed ? `/compare/download?runs=${[...sel].map(encodeURIComponent).join(",")}` : undefined}
           title="Download the full comparison as one shareable .html file"
           aria-disabled={sel.size < 2 || mixed}>⬇ Download</a>
        <button className="primary" disabled={sel.size < 2 || mixed} onClick={() => setViewing([...sel])}>
          Compare selected
        </button>
      </div>
      {err && <div className="banner-err">{err}</div>}
      <div className="filters"><input placeholder="Search label / host / tag…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <div className="card">
        <table>
          <thead><tr><th></th><th>Run</th><th>Label</th><th>Host</th><th>Mode</th><th>Status</th><th className="num">Peak QPS</th><th>Created</th></tr></thead>
          <tbody>
            {runs === null ? <tr><td colSpan={8} className="empty mono">loading…</td></tr>
              : filtered.length === 0 ? <tr><td colSpan={8} className="empty">No runs.</td></tr>
                : filtered.map((r) => (
                  <tr key={r.run_id}>
                    <td><input type="checkbox" checked={sel.has(r.run_id)} onChange={() => toggle(r.run_id)} style={{ width: "auto" }} /></td>
                    <td className="mono" style={{ fontSize: 12 }}>{r.run_id}</td>
                    <td>{r.label || "—"}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{r.target_host || "—"}</td>
                    <td>{r.mode}</td>
                    <td><span className={`badge ${r.status}`}>{r.status || "—"}</span></td>
                    <td className="num">{r.peak_qps ? fmtInt(r.peak_qps) : "—"}</td>
                    <td className="mono subtle">{fmtWhen(r.created_utc)}</td>
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
      <p className="subtle" style={{ fontSize: 12 }}>Select two or more runs, then “Compare selected”.</p>
    </>
  );
}
