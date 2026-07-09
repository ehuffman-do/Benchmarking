import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { OpsRun } from "../types";
import { OpsRunsTable } from "./KubeTargetView";

export function OpsRuns() {
  const nav = useNavigate();
  const [runs, setRuns] = useState<OpsRun[] | null>(null);
  const [kind, setKind] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);

  function load() {
    api.get<OpsRun[]>("/api/ops/runs").then(setRuns).catch((e) => setErr(e.message));
  }
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);   // ops jobs land asynchronously
    return () => clearInterval(t);
  }, []);

  const shown = (runs ?? []).filter((r) => !kind || r.kind === kind);
  const toggle = (id: string) => setSelected((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });

  return (
    <>
      <div className="toolbar">
        <h1>Cluster Ops — runs</h1>
        <div className="spacer" />
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">all kinds</option>
          {["scenario", "backup", "cr-apply", "monitor"].map((k) => <option key={k}>{k}</option>)}
        </select>
        <button className="primary" disabled={selected.size < 2 || selected.size > 8}
                onClick={() => nav(`/ops/compare?runs=${[...selected].join(",")}`)}>
          Compare scenarios ({selected.size})
        </button>
      </div>
      {err && <div className="banner-err">{err}</div>}
      <div className="card">
        <OpsRunsTable runs={shown} selectable selected={selected} onToggle={toggle} />
      </div>
    </>
  );
}
