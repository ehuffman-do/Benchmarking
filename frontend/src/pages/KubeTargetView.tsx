import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { CrSnapshot, HealthDoc, HealthFinding, Job, KubeTarget, Me, OpsRun, Run, Topology } from "../types";
import { openJobStream, CheckEvent } from "../lib/sse";
import { CheckList, relTime } from "./ClusterOps";
import { Crumbs } from "../components/Crumbs";

const SEV_BADGE: Record<string, string> = {
  crit: "failed", warn: "failed", info: "running", ok: "ok",
};

function findingLink(targetId: string | undefined, f: HealthFinding): string | null {
  const a = f.action ?? {};
  if (a.type === "diag") {
    return `/ops/targets/${targetId}/diag${a.checks?.length ? `?checks=${a.checks.join(",")}` : ""}`;
  }
  if (a.type === "params") {
    return `/ops/targets/${targetId}/params${a.filter ? `?filter=${a.filter}` : ""}`;
  }
  if (a.type === "operate") {
    return `/ops/targets/${targetId}/operate${(a as { operation?: string }).operation ? `?op=${(a as { operation?: string }).operation}` : ""}`;
  }
  return null;
}

function Spark({ data, color }: { data: number[]; color: string }) {
  if (data.length < 2) return <span className="subtle mono">—</span>;
  const w = 110, h = 26;
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const pts = data.map((v, i) =>
    `${(i / (data.length - 1)) * w},${h - 2 - ((v - min) / span) * (h - 4)}`).join(" ");
  return (
    <svg width={w} height={h} style={{ display: "block" }}>
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.6" />
    </svg>
  );
}

function seriesFromCsv(text: string, valueOf: (cols: string[], f: string[]) => number | null,
                       maxPts = 60): number[] {
  const lines = text.trim().split("\n");
  if (lines.length < 2) return [];
  const cols = lines[0].split(",");
  const byEpoch = new Map<string, number>();
  for (const ln of lines.slice(1)) {
    const f = ln.split(",");
    const v = valueOf(cols, f);
    if (v === null || Number.isNaN(v)) continue;
    const key = f[0];
    byEpoch.set(key, Math.max(byEpoch.get(key) ?? -Infinity, v));
  }
  return [...byEpoch.values()].slice(-maxPts);
}

const PATRONI_BUNDLE = JSON.stringify({
  max_wal_size: "49152", min_wal_size: "2048", archive_timeout: "300",
  wal_keep_size: "2048", checkpoint_timeout: "900",
  checkpoint_completion_target: "0.9",
}, null, 2);
const PGBACKREST_BUNDLE = JSON.stringify({
  "process-max": "4", "archive-async": "y", "spool-path": "/pgdata",
}, null, 2);

const CASES = [
  ["switchover", "Case A — graceful switchover (patronictl --force). Ref: ~4.6s, TL+1"],
  ["pgkill", "Case B — kill -9 the postmaster. Ref: restart in place, ~12–16s, no TL change"],
  ["pod-delete", "Case C1 — force-delete the leader pod. Ref: 22–31s write downtime at low load"],
  ["node-loss", "Case C2 — hard node loss via sysrq kernel panic (privileged node-pinned pod; true crash, no signal to PG)"],
] as const;

export function KubeTargetView({ me }: { me: Me }) {
  const { targetId } = useParams();
  const nav = useNavigate();
  const [kt, setKt] = useState<KubeTarget | null>(null);
  const [topo, setTopo] = useState<Topology | null>(null);
  const [topoUtc, setTopoUtc] = useState<string | null>(null);
  const [runs, setRuns] = useState<OpsRun[]>([]);
  const [benchRuns, setBenchRuns] = useState<Run[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [checks, setChecks] = useState<CheckEvent[] | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [health, setHealth] = useState<HealthDoc | null>(null);
  const [healthUtc, setHealthUtc] = useState<string | null>(null);
  const [healthRunning, setHealthRunning] = useState(false);
  const [spark, setSpark] = useState<{ wal: number[]; disk: number[]; lag: number[] } | null>(null);
  const isAdmin = me.role === "admin";
  const canOp = me.role !== "viewer";

  // launcher state
  const [crAction, setCrAction] = useState<"patroni_params" | "pgbackrest_global">("patroni_params");
  const [crParams, setCrParams] = useState(PATRONI_BUNDLE);
  const [prepReset, setPrepReset] = useState(false);
  const [bkType, setBkType] = useState("full");
  const [bkPath, setBkPath] = useState("direct");
  const [bkSource, setBkSource] = useState("leader");
  const [bkLinked, setBkLinked] = useState("");
  const [scCase, setScCase] = useState<string>("switchover");
  const [scProbe, setScProbe] = useState("port-forward");
  const [scBaseline, setScBaseline] = useState(30);
  const [scSettle, setScSettle] = useState(180);
  const [monInterval, setMonInterval] = useState(60);
  const [pmmHost, setPmmHost] = useState(
    () => localStorage.getItem(`pmm-host-${targetId}`) ?? "");
  // pg_stat_statements is the default query source: pg_stat_monitor has a
  // known memory-growth issue under sustained load (field report 2026-07).
  const [pmmSource, setPmmSource] = useState<"pgstatmonitor" | "pgstatements">("pgstatements");
  const [snapshots, setSnapshots] = useState<CrSnapshot[]>([]);

  const load = useCallback(() => {
    api.get<KubeTarget>(`/api/kube-targets/${targetId}`).then(setKt).catch((e) => setErr(e.message));
    api.get<{ topology: Topology | null; collected_utc: string | null }>(
      `/api/kube-targets/${targetId}/topology`)
      .then((r) => { setTopo(r.topology); setTopoUtc(r.collected_utc); })
      .catch(() => undefined);
    api.get<{ health: HealthDoc | null; collected_utc: string | null }>(
      `/api/kube-targets/${targetId}/health`)
      .then((r) => { setHealth(r.health); setHealthUtc(r.collected_utc); })
      .catch(() => undefined);
    api.get<OpsRun[]>(`/api/ops/runs?target=${targetId}`).then(setRuns).catch(() => undefined);
    api.get<Run[]>("/api/runs").then((rs) =>
      setBenchRuns(rs.filter((r) => r.status && !["complete", "partial", "failed", "canceled"].includes(r.status)))
    ).catch(() => undefined);
    api.get<Job[]>("/api/jobs").then(setJobs).catch(() => undefined);
    api.get<{ snapshots: CrSnapshot[] }>(`/api/kube-targets/${targetId}/cr-snapshots`)
      .then((r) => setSnapshots(r.snapshots)).catch(() => undefined);
  }, [targetId]);
  useEffect(load, [load]);

  async function discover() {
    setDiscovering(true);
    setChecks([]);
    try {
      const r = await api.post<{ job_id: number }>(`/api/kube-targets/${targetId}/discover`, {});
      openJobStream(r.job_id, {
        onCheck: (c) => setChecks((prev) => [...(prev ?? []), c]),
        onDone: () => { setDiscovering(false); load(); },
        onError: () => setDiscovering(false),
      });
    } catch (ex) {
      setErr((ex as Error).message);
      setDiscovering(false);
    }
  }

  async function launch(path: string, body: Record<string, unknown>, goToRuns = true) {
    setErr(null);
    try {
      const r = await api.post<{ job_id: number }>(`/api/kube-targets/${targetId}/${path}`, body);
      if (goToRuns) nav(`/ops/runs?job=${r.job_id}`);
      else load();
      return r;
    } catch (ex) {
      setErr((ex as Error).message);
      window.scrollTo({ top: 0, behavior: "smooth" });
      return null;
    }
  }

  function parsedCrParams(): Record<string, string> | null {
    try { return JSON.parse(crParams); } catch { return null; }
  }

  // Sparklines feed from the newest monitor run's parsed CSVs (live or done).
  useEffect(() => {
    const mon = runs.find((r) => r.kind === "monitor");
    if (!mon) return;
    const base = `/ops/runs/${mon.op_run_id}/file?name=parsed/`;
    const get = (f: string) => fetch(base + f, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : "")).catch(() => "");
    Promise.all([get("monitor.csv"), get("disk.csv"), get("replication.csv")])
      .then(([mcsv, dcsv, rcsv]) => {
        const walTotals = seriesFromCsv(mcsv, (cols, f) => {
          const i = cols.indexOf("wal_bytes");
          return i >= 0 ? Number(f[i]) : null;
        }, 61);
        const wal = walTotals.slice(1).map((v, i) => Math.max(0, v - walTotals[i]));
        const disk = seriesFromCsv(dcsv, (cols, f) => {
          const i = cols.indexOf("pgdata_use_pct");
          return i >= 0 ? Number(f[i]) : null;
        });
        const lag = seriesFromCsv(rcsv, (cols, f) => {
          const i = cols.indexOf("lag_bytes");
          return i >= 0 ? Number(f[i]) : null;
        });
        setSpark({ wal, disk, lag });
      });
  }, [runs]);

  const activeMonitor = jobs.find((j) =>
    j.kind === "ops_monitor" && ["queued", "running"].includes(j.state) &&
    (j as unknown as { kube_target_id: number | null }).kube_target_id === Number(targetId));

  if (!kt) return <p className="subtle mono">{err ?? "loading…"}</p>;
  const members = topo?.patroni?.members ?? [];

  return (
    <>
      <Crumbs trail={[["Clusters", "/ops"], [kt.name]]} />
      <div className="toolbar">
        <h1>{kt.name}</h1>
        <span className="mono subtle">{kt.cr_kind}/{kt.cr_name || "?"} · ns {kt.namespace} · {kt.api_server || "API server unknown"}</span>
        <div className="spacer" />
        <Link className="btn primary" to={`/ops/targets/${targetId}/operate`}>Operations</Link>{" "}
        <Link className="btn" to={`/ops/targets/${targetId}/params`}>Parameter map</Link>{" "}
        <Link className="btn" to={`/ops/targets/${targetId}/diag`}>Diagnostics</Link>{" "}
        <Link className="btn" to={`/ops/targets/${targetId}/logs`}>Logs</Link>{" "}
        <Link className="btn" to="/ops">← targets</Link>
      </div>

      <div className="kpi-row" style={{ marginBottom: 16 }}>
        <div className="kpi"><div className="label">Leader</div>
          <div className="value" style={{ fontSize: 15 }}>
            {topo?.patroni?.leader ? "👑 …" + topo.patroni.leader.slice(-9) : "—"}</div></div>
        <div className="kpi"><div className="label">Members healthy</div>
          <div className="value">
            {members.length ? `${members.filter((m) =>
              ["running", "streaming"].includes(m.state)).length}/${members.length}` : "—"}
          </div></div>
        <div className="kpi"><div className="label">Timeline</div>
          <div className="value">{topo?.patroni?.timeline ?? "—"}</div></div>
        <div className="kpi"><div className="label">Health</div>
          <div className="value" style={{ fontSize: 14, marginTop: 8 }}>
            {health ? <span className={`badge ${SEV_BADGE[health.status] ?? "ok"}`}>
              {health.status === "ok" ? "✓ healthy" : health.status}</span> : "—"}</div></div>
        <div className="kpi"><div className="label">WAL rate / cycle</div>
          <Spark data={spark?.wal ?? []} color="var(--live)" />
          <div className="subtle mono" style={{ fontSize: 11 }}>
            {spark?.wal.length ? `${(spark.wal[spark.wal.length - 1] / 1048576).toFixed(1)} MB` : "start the monitor"}</div></div>
        <div className="kpi"><div className="label">Data volume</div>
          <Spark data={spark?.disk ?? []} color="var(--warn)" />
          <div className="subtle mono" style={{ fontSize: 11 }}>
            {spark?.disk.length ? `${spark.disk[spark.disk.length - 1].toFixed(0)}% used (max pod)` : "—"}</div></div>
        <div className="kpi"><div className="label">Replica lag</div>
          <Spark data={spark?.lag ?? []} color="var(--brand)" />
          <div className="subtle mono" style={{ fontSize: 11 }}>
            {spark?.lag.length ? `${(spark.lag[spark.lag.length - 1] / 1024).toFixed(0)} KiB` : "—"}</div></div>
      </div>

      {err && <div className="banner-err">{err}</div>}
      {kt.schedules_paused && (
        <div className="banner-err">
          ⚠ Operator backup schedules are PAUSED on this cluster (since {kt.schedules_paused_utc}).
          {isAdmin && (
            <> Type the cluster name and restore:&nbsp;
              <input value={confirm} onChange={(e) => setConfirm(e.target.value)}
                     placeholder={kt.cr_name} style={{ width: 140 }} />
              <button className="btn-sm" onClick={() =>
                launch("schedules/restore", { confirm }, false)}>Restore schedules</button></>
          )}
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <h2>Health
            {health && <span className={`badge ${SEV_BADGE[health.status] ?? "ok"}`} style={{ marginLeft: 8 }}>
              {health.status === "ok" ? "✓ healthy" : health.status}</span>}
            {healthUtc && <span className="subtle mono" style={{ marginLeft: 8 }}
                                title={healthUtc}>checked {relTime(healthUtc)}</span>}
          </h2>
          <div className="spacer" />
          {isAdmin && (
            <label className="subtle" style={{ fontSize: 12 }}>auto-check&nbsp;
              <select value={kt.auto_health_s} onChange={async (e) => {
                try {
                  await api.post(`/api/kube-targets/${targetId}`,
                                 { auto_health_s: Number(e.target.value) });
                  load();
                } catch (ex) { setErr((ex as Error).message); }
              }}>
                <option value={0}>off</option>
                <option value={900}>every 15 min</option>
                <option value={3600}>hourly</option>
                <option value={21600}>every 6 h</option>
              </select>
            </label>
          )}
          {canOp && <button disabled={healthRunning} onClick={async () => {
            setHealthRunning(true);
            try {
              const r = await api.post<{ job_id: number }>(`/api/kube-targets/${targetId}/health`, {});
              openJobStream(r.job_id, {
                onDone: () => { setHealthRunning(false); load(); },
                onError: () => setHealthRunning(false),
              });
            } catch (ex) { setErr((ex as Error).message); setHealthRunning(false); }
          }}>{healthRunning ? "Checking…" : "Run health check"}</button>}
        </div>
        {!health ? (
          <p className="empty">No health check yet — run one to evaluate connection saturation,
            replication, slots, disk, wraparound, autovacuum, backups, and pod state in one pass.</p>
        ) : health.findings.length === 0 ? (
          <p className="subtle">✓ {health.checked} checks evaluated — nothing needs attention.</p>
        ) : (
          <table>
            <thead><tr><th /><th>Finding</th><th>Value</th><th>Why it matters / what to do</th><th /></tr></thead>
            <tbody>
              {health.findings.map((f) => {
                const link = findingLink(targetId, f);
                return (
                  <tr key={f.id}>
                    <td><span className={`badge ${SEV_BADGE[f.severity] ?? "ok"}`}>{f.severity}</span></td>
                    <td><strong>{f.title}</strong></td>
                    <td className="mono">{f.value}</td>
                    <td className="subtle" style={{ fontSize: 12 }}>
                      {f.detail}{f.remediation ? <> <strong>→ {f.remediation}</strong></> : null}
                    </td>
                    <td>{link && <Link className="btn-sm" to={link}>inspect</Link>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-head">
          <h2>Topology {topoUtc ? <span className="subtle mono">as of {topoUtc}</span> : null}</h2>
          <div className="spacer" />
          {canOp && <button onClick={discover} disabled={discovering}>
            {discovering ? "Discovering…" : "Refresh topology"}</button>}
        </div>
        {checks && checks.length > 0 && discovering && <CheckList checks={checks} />}
        {!topo ? (
          <p className="empty">No topology captured yet — run discovery.</p>
        ) : (
          <div className="grid2">
            <div>
              <h3 className="section-label">Patroni members {topo.patroni?.leader &&
                <span className="mono subtle">· leader {topo.patroni.leader} · TL {topo.patroni.timeline}</span>}</h3>
              <table>
                <thead><tr><th>Member</th><th>Role</th><th>State</th><th className="num">TL</th><th className="num">Lag MB</th></tr></thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.name}>
                      <td className="mono">{m.role.toLowerCase() === "leader" ? "👑 " : ""}{m.name}</td>
                      <td>{m.role}</td>
                      <td><span className={`badge ${["running", "streaming"].includes(m.state) ? "ok" : "failed"}`}>{m.state}</span></td>
                      <td className="num mono">{m.timeline ?? "—"}</td>
                      <td className="num mono">{m.lag_mb ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3 className="section-label">Pods</h3>
              <table>
                <thead><tr><th>Pod</th><th>Phase</th><th>Ready</th><th>Node</th></tr></thead>
                <tbody>
                  {[...(topo.pods?.instances ?? []), ...(topo.pods?.pgbouncer ?? [])].map((p) => (
                    <tr key={p.name}>
                      <td className="mono">{p.name}</td>
                      <td><span className={`badge ${p.phase === "Running" ? "ok" : "failed"}`}>{p.phase}</span></td>
                      <td className="mono">{p.ready}</td>
                      <td className="mono">{p.node}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div>
              <h3 className="section-label">Backup configuration</h3>
              <table>
                <thead><tr><th>Repo</th><th>Schedule</th><th className="mono">cron</th></tr></thead>
                <tbody>
                  {(topo.backups?.schedules ?? []).flatMap((s) =>
                    Object.entries(s.schedules).map(([k, v]) => (
                      <tr key={`${s.repo}-${k}`}><td className="mono">{s.repo}</td><td>{k}</td><td className="mono">{v}</td></tr>
                    )))}
                  {(topo.backups?.schedules ?? []).every((s) => Object.keys(s.schedules).length === 0) && (
                    <tr><td colSpan={3} className="empty">no schedules in the CR{kt.schedules_paused ? " (paused)" : ""}</td></tr>
                  )}
                </tbody>
              </table>
              {topo.backups?.global && Object.keys(topo.backups.global).length > 0 && (
                <>
                  <h3 className="section-label">pgBackRest global options</h3>
                  <table><tbody>
                    {Object.entries(topo.backups.global).map(([k, v]) => (
                      <tr key={k}><td className="mono">{k}</td><td className="mono">{String(v)}</td></tr>
                    ))}
                  </tbody></table>
                </>
              )}
              {topo.pgbackrest_info && (
                <>
                  <h3 className="section-label">pgBackRest repo</h3>
                  <pre className="mono" style={{ fontSize: 11, maxHeight: 260, overflow: "auto" }}>{topo.pgbackrest_info}</pre>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {isAdmin && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-head"><h2>Operations</h2>
            <span className="subtle">destructive actions require typing the cluster name:&nbsp;</span>
            <input value={confirm} onChange={(e) => setConfirm(e.target.value)}
                   placeholder={kt.cr_name || kt.name} className="mono" style={{ width: 180 }} />
          </div>
          <div className="grid2">
            <div>
              <h3 className="section-label">CR configuration</h3>
              <div className="field"><label>Bundle</label>
                <select value={crAction} onChange={(e) => {
                  const v = e.target.value as typeof crAction;
                  setCrAction(v);
                  setCrParams(v === "patroni_params" ? PATRONI_BUNDLE : PGBACKREST_BUNDLE);
                }}>
                  <option value="patroni_params">Patroni dynamicConfiguration parameters</option>
                  <option value="pgbackrest_global">pgBackRest global options</option>
                </select></div>
              <div className="field"><label>Parameters (JSON, editable)</label>
                <textarea rows={8} className="mono" style={{ width: "100%" }}
                          value={crParams} onChange={(e) => setCrParams(e.target.value)} /></div>
              {crAction === "patroni_params" && (
                <div className="field"><label>
                  <input type="checkbox" checked={prepReset} onChange={(e) => setPrepReset(e.target.checked)} />
                  &nbsp;prep: reset checkpointer stats after verify</label></div>
              )}
              <button onClick={() => {
                const p = parsedCrParams();
                if (!p) { setErr("parameters are not valid JSON"); return; }
                launch("cr-apply", { params: { action: crAction, dry_run: true,
                  [crAction === "patroni_params" ? "parameters" : "global"]: p } });
              }}>Dry-run (show patch + diff)</button>{" "}
              <button className="primary" onClick={() => {
                const p = parsedCrParams();
                if (!p) { setErr("parameters are not valid JSON"); return; }
                launch("cr-apply", { confirm, params: { action: crAction,
                  [crAction === "patroni_params" ? "parameters" : "global"]: p,
                  prep: prepReset ? { reset_checkpointer: true } : {} } });
              }}>Apply & verify</button>
              <h3 className="section-label" style={{ marginTop: 24 }}>Backup schedules</h3>
              <p className="subtle">Pause the operator&apos;s schedules for a test window (snapshot kept;
                the UI nags until restored).</p>
              <button onClick={() => launch("schedules/pause", { confirm }, false)}
                      disabled={kt.schedules_paused}>Pause schedules</button>{" "}
              <button onClick={() => launch("schedules/restore", { confirm }, false)}
                      disabled={!kt.schedules_paused}>Restore schedules</button>
            </div>
            <div>
              <h3 className="section-label">Backup</h3>
              <div className="row">
                <div className="field"><label>Type</label>
                  <select value={bkType} onChange={(e) => setBkType(e.target.value)}>
                    <option value="full">full</option><option value="diff">diff</option><option value="incr">incr</option>
                  </select></div>
                <div className="field"><label>Trigger path</label>
                  <select value={bkPath} onChange={(e) => setBkPath(e.target.value)}>
                    <option value="direct" disabled={bkSource !== "leader"}>
                      direct exec (pgbackrest in pod{bkSource !== "leader" ? " — leader only" : ""})</option>
                    <option value="operator">operator (manual: block + Job)</option>
                  </select></div>
              </div>
              <div className="row">
                <div className="field"><label>Source</label>
                  <select value={bkSource} onChange={(e) => {
                    const v = e.target.value;
                    setBkSource(v);
                    // In PGO, exec'ing pgbackrest inside a replica pod only sees
                    // that pod's local standby (rc=56); replica-offloaded backups
                    // must go through the operator's repo-host path.
                    if (v !== "leader") setBkPath("operator");
                  }}>
                    <option value="leader">leader</option>
                    <option value="replica">any replica (backup-standby)</option>
                    {members.filter((m) => m.role.toLowerCase() !== "leader").map((m) =>
                      <option key={m.name} value={m.name}>{m.name}</option>)}
                  </select></div>
                <div className="field"><label>Link to live benchmark run (overlay report)</label>
                  <select value={bkLinked} onChange={(e) => setBkLinked(e.target.value)}>
                    <option value="">none</option>
                    {benchRuns.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id}</option>)}
                  </select></div>
              </div>
              {bkSource !== "leader" && (
                <p className="subtle" style={{ margin: "4px 0 8px" }}>
                  Replica-sourced backups run through the operator's repo-host path and need{" "}
                  <code>backup-standby: "y"</code> in the CR's pgBackRest global options
                  {topo?.backups?.global?.["backup-standby"] === "y"
                    ? <> — <span className="badge ok">already set</span></>
                    : <>
                        {" — "}<span className="badge failed">not set</span>
                        {isAdmin && <>{" "}
                          <button className="btn-sm" onClick={() =>
                            launch("cr-apply", { confirm, params: { action: "pgbackrest_global",
                              global: { "backup-standby": "y" } } })}>
                            Enable backup-standby now</button></>}
                      </>}.
                  {" "}The standby copies the files; the primary only coordinates start/stop.
                </p>
              )}
              <button className="primary" onClick={() =>
                launch("backup", { confirm, params: { type: bkType, path: bkPath, source: bkSource,
                  ...(bkLinked ? { linked_run_id: bkLinked } : {}) } })}>
                Run backup (preflight first)</button>

              <h3 className="section-label" style={{ marginTop: 24 }}>Failover scenario</h3>
              <div className="field"><label>Case</label>
                <select value={scCase} onChange={(e) => setScCase(e.target.value)}>
                  {CASES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select></div>
              <div className="row">
                <div className="field"><label>Baseline s</label>
                  <input type="number" value={scBaseline} onChange={(e) => setScBaseline(Number(e.target.value))} /></div>
                <div className="field"><label>Settle s</label>
                  <input type="number" value={scSettle} onChange={(e) => setScSettle(Number(e.target.value))} /></div>
                <div className="field"><label>Probe</label>
                  <select value={scProbe} onChange={(e) => setScProbe(e.target.value)}>
                    <option value="port-forward">port-forward → pgBouncer</option>
                    <option value="direct">direct (service reachable)</option>
                    <option value="off">off</option>
                  </select></div>
              </div>
              <button className="danger" onClick={() =>
                launch("scenario", { confirm, params: { case: scCase, baseline_s: scBaseline,
                  settle_s: scSettle, probe: { mode: scProbe },
                  ...(bkLinked ? { linked_run_id: bkLinked } : {}) } })}>
                FIRE scenario</button>
              <p className="subtle" style={{ marginTop: 6 }}>Refuses to fire if a backup holds the
                stanza lock or another destructive op is active on this target.</p>
            </div>
          </div>
        </div>
      )}

      {canOp && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-head"><h2>CR snapshots</h2>
            <span className="subtle" style={{ fontSize: 12 }}>
              every apply/PMM/schedules op snapshots the CR before patching</span>
            <div className="spacer" />
            <button onClick={() => launch("cr-snapshot", {}, false)}>Snapshot now</button>
          </div>
          {snapshots.length === 0 ? (
            <p className="empty">No CR snapshots yet — they appear here after the first
              config op (or click “Snapshot now”).</p>
          ) : (
            <table>
              <thead><tr><th>Taken by</th><th>When</th><th>Diff vs latest</th><th /></tr></thead>
              <tbody>
                {snapshots.map((s) => (
                  <tr key={s.op_run_id}>
                    <td><Link className="mono" to={`/ops/runs/${s.op_run_id}`}>
                      {s.action || s.op}</Link></td>
                    <td className="mono">{relTime(s.created_utc)}</td>
                    <td className="subtle" style={{ fontSize: 12 }}>{s.diff_summary}</td>
                    <td>{isAdmin && s.diff_total > 0 && (
                      <button className="btn-sm" onClick={() => {
                        if (!confirm) { setErr("type the cluster name in the Operations box to revert"); return; }
                        if (!window.confirm(`Revert the managed CR sections to this snapshot (${s.diff_total} field(s))? A snapshot of the current CR is taken first, so this is undoable.`)) return;
                        launch("cr-revert", { confirm, params: { snapshot_of: s.op_run_id } });
                      }}>Revert…</button>)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {isAdmin && <p className="subtle" style={{ margin: "8px 0 0", fontSize: 12 }}>
            Revert builds a targeted patch from the diff (managed sections only:
            Patroni parameters, pgBackRest/pgBouncer globals) — never a blind apply of
            the whole document. Type the cluster name in the Operations box first.</p>}
        </div>
      )}

      {canOp && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-head"><h2>Telemetry monitor</h2>
            {activeMonitor && <span className="badge running">running (job {activeMonitor.id})</span>}
          </div>
          {activeMonitor ? (
            <>
              {activeMonitor.run_id && <Link className="btn" to={`/ops/runs/${activeMonitor.run_id}`}>Open live panel</Link>}{" "}
              <button className="danger" onClick={async () => {
                await api.post(`/api/jobs/${activeMonitor.id}/stop`, {});
                setTimeout(load, 800);
              }}>Stop monitor</button>
            </>
          ) : (
            <>
              <div className="field" style={{ maxWidth: 220 }}><label>Interval seconds</label>
                <input type="number" value={monInterval} onChange={(e) => setMonInterval(Number(e.target.value))} /></div>
              <button onClick={() => launch("monitor", { params: { interval_s: monInterval } })}>
                Start monitor</button>
              <span className="subtle"> — WAL rate, checkpoints, archive queue, replication lag,
                per-member disk; leader re-detected every cycle.</span>
            </>
          )}
        </div>
      )}

      {canOp && (() => {
        const pmmExt = pmmSource === "pgstatmonitor" ? "pg_stat_monitor" : "pg_stat_statements";
        const instPods = topo?.pods?.instances ?? [];
        const pmmPods = instPods.filter((p) => (p.containers ?? []).includes("pmm-client")).length;
        const lastPmm = runs.find((r) => ["pmm-enable", "pmm-status", "pmm-disable"].includes(r.kind));
        const pmmParams = { server_host: pmmHost.trim(), query_source: pmmSource, extension: pmmExt };
        return (
          <div className="card" style={{ marginTop: 16 }}>
            <div className="card-head"><h2>PMM monitoring</h2>
              {instPods.length > 0 && (
                <span className={`badge ${pmmPods === instPods.length ? "ok" : pmmPods > 0 ? "running" : "failed"}`}>
                  {pmmPods === instPods.length ? "✓ sidecar on all pods"
                    : pmmPods > 0 ? `sidecar on ${pmmPods}/${instPods.length} pods` : "not enabled"}
                </span>
              )}
              {lastPmm && (
                <Link className="mono subtle" style={{ fontSize: 12, marginLeft: 8 }}
                      to={`/ops/runs/${lastPmm.op_run_id}`}>
                  last: {lastPmm.kind} · {lastPmm.status}</Link>
              )}
            </div>
            <p className="subtle" style={{ marginTop: 0 }}>
              One-click Percona Monitoring &amp; Management 3.x: state backup → PMM3 secret →
              CR patch → rolling restart → <code>CREATE EXTENSION {pmmExt}</code> on the primary →
              validation report + server-side inventory check. Existing{" "}
              <code>shared_preload_libraries</code> are auto-detected and preserved — the extension
              is appended, never a replacement. The API token is read from the worker&apos;s
              environment (<code>PGB_PMM_TOKEN</code>, see OPERATIONS.md “Worker secrets”) — it is
              never entered or stored here.
            </p>
            <div className="row">
              <div className="field" style={{ minWidth: 260 }}><label>PMM server host</label>
                <input className="mono" value={pmmHost} placeholder="pmm.example.com"
                       onChange={(e) => {
                         setPmmHost(e.target.value);
                         localStorage.setItem(`pmm-host-${targetId}`, e.target.value);
                       }} /></div>
              <div className="field"><label>Query source (extension is paired automatically)</label>
                <select value={pmmSource} onChange={(e) => setPmmSource(e.target.value as typeof pmmSource)}>
                  <option value="pgstatements">pg_stat_statements (built-in — recommended)</option>
                  <option value="pgstatmonitor">pg_stat_monitor (richer QAN — ⚠ memory-growth issue)</option>
                </select></div>
            </div>
            {pmmSource === "pgstatmonitor" && (
              <p className="subtle" style={{ marginTop: 0 }}>
                ⚠ <strong>pg_stat_monitor has a known memory-growth issue under sustained
                load</strong> (verified 2026-07 on long TPC-C runs — it took down multi-hour
                tests). Prefer pg_stat_statements for anything longer than an hour. Already
                running pg_stat_monitor? Switch by re-enabling PMM with pg_stat_statements
                selected: the agent query source is re-registered and the extension created;
                then remove <code>pg_stat_monitor</code> from{" "}
                <code>shared_preload_libraries</code> in the parameter map (restart-required)
                and <code>DROP EXTENSION pg_stat_monitor</code>.
              </p>
            )}
            <button disabled={!pmmHost.trim()} onClick={() =>
              launch("pmm/status", { params: pmmParams })}>
              Check status (read-only)</button>{" "}
            {isAdmin && <>
              <button disabled={!pmmHost.trim()} onClick={() =>
                launch("pmm/enable", { params: { ...pmmParams, dry_run: true } })}>
                Dry-run (show plan)</button>{" "}
              <button className="primary" disabled={!pmmHost.trim()} onClick={() =>
                launch("pmm/enable", { confirm, params: pmmParams })}>
                Enable PMM (rolls all pods)</button>{" "}
              <button className="danger" onClick={() =>
                launch("pmm/disable", { confirm, params: {} })}>
                Disable (restore pre-PMM CR)</button>
            </>}
            {isAdmin && (
              <p className="subtle" style={{ marginTop: 6 }}>
                Enable/disable are destructive (every instance pod restarts, HA-preserving) —
                type the cluster name in the Operations box above to confirm. Disable restores
                the CR snapshot taken by the latest enable run.
              </p>
            )}
          </div>
        );
      })()}

      {canOp && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-head"><h2>IOPS evidence</h2></div>
          <p className="subtle" style={{ marginTop: 0 }}>
            Can this cluster&apos;s pgdata volume exceed the standard 10K IOPS
            throttle? Launch an evidence run with this cluster pre-attached —
            storage identity (PVC/PV/StorageClass) and a 1s device-IOPS series
            are captured automatically, and the run page shows the
            capped / exceeds / inconclusive verdict with the full bundle
            downloadable for independent review.
          </p>
          <Link className="btn primary" to={`/new?cluster=${targetId}&mode=suite`}>
            Evidence suite (full matrix)</Link>{" "}
          <Link className="btn" to={`/new?cluster=${targetId}&mode=rate-steps`}>
            Rate-stepped knee finder</Link>
          {isAdmin ? (
            <>
              <div style={{ marginTop: 10 }}>
                <span className="subtle" style={{ fontSize: 12, marginRight: 8 }}>
                  Direct device probe (sysbench fileio on the pgdata volume —
                  TEST CLUSTERS ONLY; keeps test files between probes for fast
                  iteration):</span>
              </div>
              <Link className="btn" to={`/new?cluster=${targetId}&mode=device-probe&variant=rndrw`}>
                Probe: mixed (rndrw)</Link>{" "}
              <Link className="btn" to={`/new?cluster=${targetId}&mode=device-probe&variant=rndrd`}>
                Probe: read ceiling (rndrd)</Link>{" "}
              <Link className="btn" to={`/new?cluster=${targetId}&mode=device-probe&variant=rndwr`}>
                Probe: write ceiling (rndwr)</Link>{" "}
              <Link className="btn primary" to={`/new?cluster=${targetId}&mode=evidence-pack`}>
                Evidence pack (core 4 + narrative, ~45 min)</Link>
            </>
          ) : (
            <span className="subtle" style={{ fontSize: 12 }}>
              {" "}The direct device probe (sysbench fileio on the pgdata
              volume) is admin-only — test clusters only.</span>
          )}
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-head"><h2>Op runs on this target</h2>
          <div className="spacer" /><Link className="btn" to="/ops/runs">all ops runs →</Link></div>
        <OpsRunsTable runs={runs} />
      </div>
    </>
  );
}

export function OpsRunsTable({ runs, selectable, selected, onToggle }: {
  runs: OpsRun[];
  selectable?: boolean;
  selected?: Set<string>;
  onToggle?: (id: string) => void;
}) {
  const summarize = (r: OpsRun): string => {
    const h = r.headline ?? {};
    if (r.kind === "scenario") {
      const dt = h.downtime_ms != null ? `${(h.downtime_ms / 1000).toFixed(1)}s down` : "";
      const cls = h.kind ? String(h.kind) : "";
      return [h.case, dt, cls].filter(Boolean).join(" · ");
    }
    if (r.kind === "backup") {
      return [h.type, h.path, h.label, h.duration_s ? `${h.duration_s}s` : ""].filter(Boolean).join(" · ");
    }
    if (r.kind === "cr-apply") {
      const n = h.changed ? Object.keys(h.changed).length : 0;
      return [h.action, h.dry_run ? "dry-run" : "", `${n} changed`,
              h.outcome ? String(h.outcome) : "",
              (h.pending_restart?.length ?? 0) > 0 ? "⚠ pending_restart" : ""].filter(Boolean).join(" · ");
    }
    if (r.kind === "monitor") return h.cycles ? `${h.cycles} cycles` : "";
    if (r.kind.startsWith("pmm-")) {
      return [h.dry_run ? "dry-run" : "",
              h.qan != null ? (h.qan ? "QAN ✓" : "QAN pending") : "",
              h.inventory_nodes != null ? `${h.inventory_nodes} in inventory` : "",
              h.healthy === false ? "⚠ see report" : "",
              h.restored_from ? `restored from ${String(h.restored_from).slice(0, 28)}…` : "",
              h.reason ? String(h.reason) : ""].filter(Boolean).join(" · ");
    }
    return "";
  };
  return (
    <table>
      <thead><tr>
        {selectable && <th />}
        <th>Run</th><th>Kind</th><th>Target</th><th>Status</th><th>Summary</th><th>Started</th>
      </tr></thead>
      <tbody>
        {runs.length === 0 ? (
          <tr><td colSpan={selectable ? 7 : 6} className="empty">no op runs yet</td></tr>
        ) : runs.map((r) => (
          <tr key={r.op_run_id}>
            {selectable && (
              <td><input type="checkbox" disabled={r.kind !== "scenario"}
                         checked={selected?.has(r.op_run_id) ?? false}
                         onChange={() => onToggle?.(r.op_run_id)} /></td>
            )}
            <td><Link className="mono" to={`/ops/runs/${r.op_run_id}`}>{r.op_run_id}</Link></td>
            <td>{r.kind}</td>
            <td className="mono">{r.kube_target_name}</td>
            <td><span className={`badge ${r.status}`}>{r.status}</span></td>
            <td className="mono" style={{ fontSize: 12 }}>{summarize(r)}</td>
            <td className="mono">{r.created_utc}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
