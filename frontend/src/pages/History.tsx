import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Job, Me, OpsRun, Run } from "../types";
import { fmtInt, fmtWhen, relAge } from "../lib/format";

const STATUSES = ["", "complete", "partial", "running", "failed"];
const KINDS = [["", "All"], ["bench", "Benchmarks"], ["ops", "Cluster ops"]] as const;

// One row shape for the unified feed — benchmark runs and cluster-ops runs
// side by side, each linking into its own detail view.
interface FeedRow {
  id: string;
  kind: "bench" | "ops";
  type: string;                 // sweep/soak | backup/scenario/...
  label: string;
  where: string;                // DB host | kube target
  status: string;
  metric: string;               // peak QPS | ops headline summary
  created: string;
  to: string;
  report: string | null;
  bench?: Run;
}

function opsSummary(r: OpsRun): string {
  const h = r.headline ?? {};
  if (r.kind === "scenario") {
    return [h.case, h.downtime_ms != null ? `${(h.downtime_ms / 1000).toFixed(1)}s down` : "",
            h.kind].filter(Boolean).join(" · ");
  }
  if (r.kind === "backup") {
    return [h.type, h.path, h.duration_s ? `${h.duration_s}s` : ""].filter(Boolean).join(" · ");
  }
  if (r.kind === "cr-apply") {
    const n = h.changed ? Object.keys(h.changed).length : 0;
    return [h.action, h.dry_run ? "dry-run" : "", `${n} changed`].filter(Boolean).join(" · ");
  }
  if (r.kind === "monitor") return h.cycles ? `${h.cycles} cycles` : "";
  if (r.kind === "diag") return h.checks ? `${h.checks} checks` : "";
  return "";
}

export function History({ me }: { me: Me }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [opsRuns, setOpsRuns] = useState<OpsRun[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);
  const canRun = me.role === "operator" || me.role === "admin";

  async function load() {
    try {
      const [r, j, o] = await Promise.all([
        api.get<Run[]>("/api/runs"),
        api.get<Job[]>("/api/jobs?active=1"),
        api.get<OpsRun[]>("/api/ops/runs").catch(() => [] as OpsRun[]),
      ]);
      setRuns(r);
      setJobs(j);
      setOpsRuns(o);
      setErr(null);            // clear a stale error after a successful reload
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  // Keep the active-jobs panel live while anything is in flight.
  useEffect(() => {
    if (jobs.length === 0) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [jobs.length]);

  const feed = useMemo<FeedRow[]>(() => {
    const bench: FeedRow[] = (runs ?? []).map((r) => ({
      id: r.run_id, kind: "bench", type: r.mode || "run", label: r.label || "",
      where: r.target_host || "", status: r.status || "",
      metric: r.peak_qps ? `${fmtInt(r.peak_qps)} qps` : "",
      created: r.created_utc, to: `/runs/${r.run_id}`,
      report: `/runs/${r.run_id}/report`, bench: r,
    }));
    const ops: FeedRow[] = opsRuns.map((r) => ({
      id: r.op_run_id, kind: "ops", type: r.kind, label: r.label || "",
      where: r.kube_target_name || "", status: r.status || "",
      metric: opsSummary(r), created: r.created_utc,
      to: `/ops/runs/${r.op_run_id}`, report: null,
    }));
    return [...bench, ...ops].sort((a, b) => (b.created || "").localeCompare(a.created || ""));
  }, [runs, opsRuns]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return feed.filter((r) => {
      if (kind && r.kind !== kind) return false;
      if (status && r.status !== status) return false;
      if (!needle) return true;
      return [r.label, r.id, r.where, r.type, r.metric,
              r.bench?.tags, r.bench?.ticket, r.bench?.owner]
        .filter(Boolean)
        .some((s) => String(s).toLowerCase().includes(needle));
    });
  }, [feed, q, status, kind]);

  const kpis = useMemo(() => {
    return {
      total: (runs ?? []).length,
      ops: opsRuns.length,
      running: jobs.filter((j) => j.state === "running").length,
      queued: jobs.filter((j) => j.state === "queued").length,
      peak: (runs ?? []).reduce((m, r) => Math.max(m, r.peak_qps ?? 0), 0),
    };
  }, [runs, opsRuns, jobs]);

  async function cancel(id: number) {
    if (!confirm(`Cancel job ${id}?`)) return;
    try {
      await api.post(`/api/jobs/${id}/cancel`);
      load();
    } catch (e) {
      alert((e as Error).message);
    }
  }

  async function rerun(id: string) {
    if (!confirm(`Re-run ${id} with the same config?`)) return;
    try {
      const d = await api.post<{ needs_password: boolean }>(`/api/runs/${id}/rerun`);
      if (d.needs_password)
        alert("Re-run queued — but this run had no saved target, so it will fail without a password. "
          + "Use Clone to re-enter credentials, or save the cluster under Targets.");
      load();
    } catch (e) {
      alert((e as Error).message);
    }
  }

  function host(h?: string) {
    if (!h) return "—";
    return h.length > 30 ? h.slice(0, 14) + "…" + h.slice(-12) : h;
  }

  // Ops jobs index into ops runs; benchmark jobs into runs. One feed, two
  // detail views — send each link to the right one.
  const jobRunLink = (j: Job) =>
    j.kind.startsWith("ops_") ? `/ops/runs/${j.run_id}` : `/runs/${j.run_id}`;

  return (
    <>
      <div className="toolbar">
        <h1>Runs</h1>
        <div className="spacer" />
        {canRun && <Link className="btn primary" to="/new">＋ New run</Link>}
      </div>

      {err && <div className="banner-err">{err}</div>}

      <div className="kpi-row" style={{ marginBottom: 18 }}>
        <div className="kpi"><div className="label">Benchmark runs</div><div className="value">{fmtInt(kpis.total)}</div></div>
        <div className="kpi"><div className="label">Cluster ops runs</div><div className="value">{fmtInt(kpis.ops)}</div></div>
        <div className="kpi"><div className="label">Running</div><div className="value">{fmtInt(kpis.running)}</div></div>
        <div className="kpi"><div className="label">Queued</div><div className="value">{fmtInt(kpis.queued)}</div></div>
        <div className="kpi"><div className="label">Best peak QPS</div><div className="value">{kpis.peak ? fmtInt(kpis.peak) : "—"}</div></div>
      </div>

      {jobs.length > 0 && (
        <div className="card">
          <div className="card-head"><h2>Active &amp; queued</h2></div>
          <table>
            <thead>
              <tr><th>Job</th><th>Kind</th><th>State</th><th>Run</th><th>By</th><th>Scheduled</th><th></th></tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td className="mono">{j.id}</td>
                  <td>{j.kind}</td>
                  <td><span className={`badge ${j.state}`}>{j.state}</span></td>
                  <td>{j.run_id ? <Link className="mono" to={jobRunLink(j)}>{j.run_id}</Link> : "—"}</td>
                  <td className="mono">{j.requested_by}</td>
                  <td className="mono subtle">{j.scheduled_utc ? fmtWhen(j.scheduled_utc) : "now"}</td>
                  <td style={{ textAlign: "right" }}>
                    {canRun && ["queued", "running", "canceling"].includes(j.state) && (
                      <button className="ghost" onClick={() => cancel(j.id)}>Cancel</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="filters">
        <input
          placeholder="Search label, tag, ticket, owner, host, target…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <div className="seg" style={{ marginBottom: 0 }}>
          {KINDS.map(([v, l]) => (
            <button key={v} className={kind === v ? "on" : ""} onClick={() => setKind(v)}>{l}</button>
          ))}
        </div>
        <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 160 }}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s || "any status"}</option>
          ))}
        </select>
      </div>

      <div className="card table-scroll">
        <table>
          <thead>
            <tr>
              <th>Run</th><th>Type</th><th>Label</th><th>Where</th>
              <th>Status</th><th>Result</th><th>Created</th><th></th>
            </tr>
          </thead>
          <tbody>
            {runs === null ? (
              <tr><td colSpan={8} className="empty mono">loading…</td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={8} className="empty">No runs match. Start one from “New run”.</td></tr>
            ) : (
              filtered.map((r) => (
                <tr key={`${r.kind}-${r.id}`}>
                  <td><Link className="mono" to={r.to}>{r.id}</Link></td>
                  <td>
                    <span className={`chip kind-${r.kind}`}>{r.kind === "bench" ? "benchmark" : "cluster ops"}</span>
                    <span className="mono subtle" style={{ marginLeft: 6, fontSize: 12 }}>{r.type}</span>
                  </td>
                  <td>{r.label || "—"}</td>
                  <td className="mono" style={{ fontSize: 12 }} title={r.where}>{host(r.where)}</td>
                  <td><span className={`badge ${r.status}`}>{r.status || "—"}</span></td>
                  <td className="mono num" style={{ fontSize: 12 }}>{r.metric || "—"}</td>
                  <td className="mono subtle" title={r.created}>{relAge(r.created) || fmtWhen(r.created)}</td>
                  <td className="row-actions">
                    {r.kind === "bench" ? (
                      <>
                        <Link className="btn-sm" to={`${r.to}/report`}>Report</Link>
                        <a className="btn-sm" href={`/runs/${r.id}/spec`} target="_blank" rel="noreferrer">Spec</a>
                        {canRun && <Link className="btn-sm" to={`/new?from=${r.id}`}>Clone</Link>}
                        {canRun && <button className="btn-sm" onClick={() => rerun(r.id)}>Re-run</button>}
                      </>
                    ) : (
                      <>
                        <Link className="btn-sm" to={r.to}>Open</Link>
                        <a className="btn-sm" href={`/ops/runs/${r.id}/report`} target="_blank" rel="noreferrer">Report</a>
                      </>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
