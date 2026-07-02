import { useEffect, useMemo, useState } from "react";
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
  // Live compare overlays runs on a shared real-time axis — soak-only (a sweep's
  // per-level timeline can't be wall-clock aligned), 2–6 runs.
  const canLive = sel.size >= 2 && sel.size <= 6 && selModes.size === 1 && [...selModes][0] === "soak";

  if (viewing) {
    return (
      <>
        <div className="toolbar no-print">
          <h1>Comparison</h1><div className="spacer" />
          <button onClick={() => setViewing(null)}>← Back to selection</button>
          <button onClick={() => window.print()}>Print / PDF</button>
        </div>
        <div className="report-frame">
          <iframe title="comparison" src={`/compare/view?runs=${viewing.map(encodeURIComponent).join(",")}`} />
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
        <button disabled={!canLive}
          title={canLive ? "Overlay these soaks live on a shared real-time axis" : "Select 2–6 soak runs"}
          onClick={() => navigate(`/compare/live?runs=${[...sel].map(encodeURIComponent).join(",")}`)}>
          ▶ Live compare
        </button>
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
