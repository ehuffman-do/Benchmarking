import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { DiagCheckInfo, KubeTarget, Me } from "../types";
import { Crumbs } from "../components/Crumbs";

const CATEGORY_LABEL: Record<string, string> = {
  sessions: "Sessions & locks",
  replication: "Replication & HA",
  maintenance: "Vacuum & wraparound",
  storage: "Storage & WAL",
  backups: "Backups",
  kubernetes: "Kubernetes",
};

export function KubeDiag({ me }: { me: Me }) {
  const { targetId } = useParams();
  const nav = useNavigate();
  const [search] = useSearchParams();
  const [kt, setKt] = useState<KubeTarget | null>(null);
  const [catalog, setCatalog] = useState<DiagCheckInfo[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(
    new Set((search.get("checks") ?? "").split(",").filter(Boolean)));
  const [watchS, setWatchS] = useState(0);
  const [intervalS, setIntervalS] = useState(2);
  const [err, setErr] = useState<string | null>(null);
  const canOp = me.role !== "viewer";

  const load = useCallback(() => {
    api.get<KubeTarget>(`/api/kube-targets/${targetId}`).then(setKt).catch((e) => setErr(e.message));
    api.get<{ checks: DiagCheckInfo[] }>("/api/ops/diag-catalog")
      .then((r) => setCatalog(r.checks)).catch((e) => setErr(e.message));
  }, [targetId]);
  useEffect(load, [load]);

  const byCategory = useMemo(() => {
    const m = new Map<string, DiagCheckInfo[]>();
    (catalog ?? []).forEach((c) => {
      m.set(c.category, [...(m.get(c.category) ?? []), c]);
    });
    return m;
  }, [catalog]);

  function toggle(key: string) {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(key)) n.delete(key); else n.add(key);
      return n;
    });
  }

  async function run(keys: string[]) {
    setErr(null);
    try {
      const params: Record<string, unknown> = { checks: keys };
      if (watchS > 0) { params.watch_s = watchS; params.interval_s = intervalS; }
      const r = await api.post<{ job_id: number }>(
        `/api/kube-targets/${targetId}/diag`, { params, label: `diag-${keys.length}-checks` });
      nav(`/ops/runs?job=${r.job_id}`);
    } catch (ex) {
      setErr((ex as Error).message);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  if (!kt || !catalog) return <p className="subtle mono">{err ?? "loading…"}</p>;
  const watchable = catalog.filter((c) => c.watch).map((c) => c.key);

  return (
    <>
      <Crumbs trail={[["Clusters", "/ops"], [kt.name, `/ops/targets/${targetId}`], ["Diagnostics"]]} />
      <div className="toolbar">
        <h1>Diagnostics — {kt.name}</h1>
        <span className="mono subtle">{kt.cr_kind}/{kt.cr_name || "?"} · ns {kt.namespace}</span>
        <div className="spacer" />
        <Link className="btn" to={`/ops/targets/${targetId}`}>← target</Link>
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 12 }}>
        Read-only checks — every result lands in a live cockpit as tables and charts, nothing
        touches the cluster. No kubectl or SQL knowledge needed: pick what you want to look at
        and run it.
      </p>

      {err && <div className="banner-err">{err}</div>}

      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-head">
          <h2>Run</h2>
          <span className="subtle">{selected.size} selected</span>
          <div className="spacer" />
          <label className="subtle">Watch&nbsp;
            <select value={watchS} onChange={(e) => setWatchS(Number(e.target.value))}>
              <option value={0}>once</option>
              <option value={60}>1 min</option>
              <option value={300}>5 min</option>
              <option value={900}>15 min</option>
            </select>
          </label>
          {watchS > 0 && (
            <label className="subtle">&nbsp;every&nbsp;
              <select value={intervalS} onChange={(e) => setIntervalS(Number(e.target.value))}>
                <option value={2}>2 s</option>
                <option value={5}>5 s</option>
                <option value={15}>15 s</option>
              </select>
            </label>
          )}
          &nbsp;
          {canOp && (
            <>
              <button className="primary" disabled={selected.size === 0}
                      onClick={() => run([...selected])}>Run selected</button>{" "}
              <button onClick={() => run(catalog.map((c) => c.key))}>Run everything</button>
            </>
          )}
        </div>
        {watchS > 0 && (
          <p className="subtle">Watch mode re-samples the live checks
            ({watchable.join(", ")}) on the interval — open the run to see the charts move.</p>
        )}
      </div>

      {[...byCategory.entries()].map(([category, checks]) => (
        <div className="card" style={{ marginBottom: 12 }} key={category}>
          <div className="card-head">
            <h2>{CATEGORY_LABEL[category] ?? category}</h2>
            <div className="spacer" />
            <button className="btn-sm" onClick={() =>
              setSelected((s) => new Set([...s, ...checks.map((c) => c.key)]))}>select all</button>
          </div>
          <table>
            <tbody>
              {checks.map((c) => (
                <tr key={c.key}>
                  <td style={{ width: 24 }}>
                    <input type="checkbox" checked={selected.has(c.key)}
                           onChange={() => toggle(c.key)} />
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <strong>{c.title}</strong>
                    {c.watch && <span className="badge ok" style={{ marginLeft: 6 }} title="live-watchable">live</span>}
                  </td>
                  <td className="subtle">{c.description}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {canOp && <button className="btn-sm" onClick={() => run([c.key])}>Run</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}
