import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { HealthDoc, HealthFinding, Job, KubeTarget, Me, OpsRun, Run, Topology } from "../types";
import { openJobStream, CheckEvent } from "../lib/sse";
import { CheckList } from "./ClusterOps";
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
  return null;
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
  ["node-loss", "Case C2 — cordon + delete the leader's node (EXPERIMENTAL)"],
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
        <Link className="btn" to={`/ops/targets/${targetId}/params`}>Parameter map</Link>{" "}
        <Link className="btn" to={`/ops/targets/${targetId}/diag`}>Diagnostics</Link>{" "}
        <Link className="btn" to="/ops">← targets</Link>
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
            {healthUtc && <span className="subtle mono" style={{ marginLeft: 8 }}>as of {healthUtc}</span>}
          </h2>
          <div className="spacer" />
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
              (h.pending_restart?.length ?? 0) > 0 ? "⚠ pending_restart" : ""].filter(Boolean).join(" · ");
    }
    if (r.kind === "monitor") return h.cycles ? `${h.cycles} cycles` : "";
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
