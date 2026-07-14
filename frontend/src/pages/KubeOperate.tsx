import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { KubeTarget, Me, Topology } from "../types";
import { Crumbs } from "../components/Crumbs";

/** Guided day-2 operations: every card is preflight → dry-run → confirm →
 *  execute with a live cockpit → verify. Nothing here needs kubectl. */
export function KubeOperate({ me }: { me: Me }) {
  const { targetId } = useParams();
  const nav = useNavigate();
  const [search] = useSearchParams();
  const [kt, setKt] = useState<KubeTarget | null>(null);
  const [topo, setTopo] = useState<Topology | null>(null);
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const isAdmin = me.role === "admin";

  // per-operation form state
  const [soType, setSoType] = useState<"switchover" | "failover">("switchover");
  const [soTarget, setSoTarget] = useState("");
  const [rsScope, setRsScope] = useState("cluster");
  const [scReplicas, setScReplicas] = useState(3);
  const [rzCpuR, setRzCpuR] = useState(""); const [rzMemR, setRzMemR] = useState("");
  const [rzCpuL, setRzCpuL] = useState(""); const [rzMemL, setRzMemL] = useState("");
  const [schFull, setSchFull] = useState(""); const [schDiff, setSchDiff] = useState("");
  const [schIncr, setSchIncr] = useState(""); const [retFull, setRetFull] = useState("");

  const load = useCallback(() => {
    api.get<KubeTarget>(`/api/kube-targets/${targetId}`).then(setKt).catch((e) => setErr(e.message));
    api.get<{ topology: Topology | null }>(`/api/kube-targets/${targetId}/topology`)
      .then((r) => {
        setTopo(r.topology);
        const n = r.topology?.pods?.instances?.length;
        if (n) setScReplicas(n);
        const sch = r.topology?.backups?.schedules?.[0]?.schedules ?? {};
        setSchFull(sch.full ?? ""); setSchDiff(sch.differential ?? "");
        setSchIncr(sch.incremental ?? "");
      })
      .catch(() => undefined);
  }, [targetId]);
  useEffect(load, [load]);

  async function launch(params: Record<string, unknown>, dry: boolean) {
    setErr(null);
    try {
      const body: Record<string, unknown> = {
        params: { ...params, ...(dry ? { dry_run: true } : {}) },
      };
      if (!dry) body.confirm = confirm;
      const r = await api.post<{ job_id: number }>(
        `/api/kube-targets/${targetId}/operate`, body);
      nav(`/ops/runs?job=${r.job_id}`);
    } catch (ex) {
      setErr((ex as Error).message);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  if (!kt) return <p className="subtle mono">{err ?? "loading…"}</p>;
  const members = topo?.patroni?.members ?? [];
  const leader = topo?.patroni?.leader ?? "";
  const replicas = members.filter((m) => m.role.toLowerCase() !== "leader");
  const focus = search.get("op");

  const Buttons = ({ params, danger }: { params: Record<string, unknown>; danger?: boolean }) => (
    <div style={{ marginTop: 8 }}>
      <button onClick={() => launch(params, true)}>Dry-run (plan only)</button>{" "}
      {isAdmin && <button className={danger ? "danger" : "primary"}
                          onClick={() => launch(params, false)}>Execute</button>}
    </div>
  );

  return (
    <>
      <Crumbs trail={[["Clusters", "/ops"], [kt.name, `/ops/targets/${targetId}`], ["Operations"]]} />
      <div className="toolbar">
        <h1>Operations — {kt.name}</h1>
        <span className="mono subtle">{kt.cr_kind}/{kt.cr_name || "?"} · leader {leader || "?"}</span>
        <div className="spacer" />
        <Link className="btn" to={`/ops/targets/${targetId}`}>← target</Link>
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 12 }}>
        Every operation follows the same guarded shape: preflight checks refuse bad requests,
        dry-run shows the exact patch or command, execution streams a live watch, and a verify
        step confirms the cluster settled. One destructive operation per cluster at a time.
      </p>
      {err && <div className="banner-err">{err}</div>}

      {isAdmin && (
        <div className="card" style={{ marginBottom: 12 }}>
          <span className="subtle">Executing (not dry-running) requires typing the cluster name:&nbsp;</span>
          <input value={confirm} onChange={(e) => setConfirm(e.target.value)}
                 placeholder={kt.cr_name || kt.name} className="mono" style={{ width: 180 }} />
        </div>
      )}

      <div className="grid2">
        <div className="card" style={focus === "restart" ? { outline: "2px solid var(--brand)" } : undefined}>
          <div className="card-head"><h2>⟳ Rolling restart</h2></div>
          <p className="subtle">Cluster-wide: the operator restarts replicas first, primary last
            (one brief failover). Single member: in-place via patronictl, no failover unless it
            is the leader. This is how <code>pending_restart</code> parameters take effect.</p>
          <div className="field"><label>Scope</label>
            <select value={rsScope} onChange={(e) => setRsScope(e.target.value)}>
              <option value="cluster">whole cluster (rolling)</option>
              {members.map((m) => <option key={m.name} value={m.name}>
                {m.name}{m.name === leader ? " (leader — will fail over)" : ""}</option>)}
            </select></div>
          <Buttons params={{ operation: "restart", scope: rsScope }} danger />
        </div>

        <div className="card">
          <div className="card-head"><h2>⇄ Switchover / failover</h2></div>
          <p className="subtle">Move the leader — planned (switchover, clean handoff) or forced
            (failover, for when the leader is unhealthy). Preflight refuses if no streaming
            replica can take over.</p>
          <div className="row">
            <div className="field"><label>Type</label>
              <select value={soType} onChange={(e) => setSoType(e.target.value as typeof soType)}>
                <option value="switchover">switchover (planned)</option>
                <option value="failover">failover (forced)</option>
              </select></div>
            <div className="field"><label>New leader</label>
              <select value={soTarget} onChange={(e) => setSoTarget(e.target.value)}>
                <option value="">best available replica</option>
                {replicas.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
              </select></div>
          </div>
          <Buttons params={{ operation: soType, ...(soTarget ? { target: soTarget } : {}) }} danger />
        </div>

        <div className="card">
          <div className="card-head"><h2>⇕ Scale replicas</h2></div>
          <p className="subtle">Grow or shrink the instance set. New members clone from a backup
            and start streaming; the watch waits until they do. <strong>Scaling down deletes the
            removed members' volumes.</strong></p>
          <div className="field" style={{ maxWidth: 160 }}><label>Replicas
            (now {topo?.pods?.instances?.length ?? "?"})</label>
            <input type="number" min={1} max={16} value={scReplicas}
                   onChange={(e) => setScReplicas(Number(e.target.value))} /></div>
          <Buttons params={{ operation: "scale", replicas: scReplicas }} />
        </div>

        <div className="card">
          <div className="card-head"><h2>▣ Vertical resize</h2></div>
          <p className="subtle">Change CPU/memory for the instance pods — rolling recreate,
            primary last. Preflight warns when the memory limit is too small for
            <code> shared_buffers</code> (the classic OOM loop).</p>
          <div className="row">
            <div className="field"><label>CPU request</label>
              <input value={rzCpuR} onChange={(e) => setRzCpuR(e.target.value)} placeholder="2" /></div>
            <div className="field"><label>Memory request</label>
              <input value={rzMemR} onChange={(e) => setRzMemR(e.target.value)} placeholder="4Gi" /></div>
          </div>
          <div className="row">
            <div className="field"><label>CPU limit</label>
              <input value={rzCpuL} onChange={(e) => setRzCpuL(e.target.value)} placeholder="4" /></div>
            <div className="field"><label>Memory limit</label>
              <input value={rzMemL} onChange={(e) => setRzMemL(e.target.value)} placeholder="8Gi" /></div>
          </div>
          <Buttons params={{
            operation: "resize", resources: {
              ...(rzCpuR || rzMemR ? { requests: { ...(rzCpuR ? { cpu: rzCpuR } : {}), ...(rzMemR ? { memory: rzMemR } : {}) } } : {}),
              ...(rzCpuL || rzMemL ? { limits: { ...(rzCpuL ? { cpu: rzCpuL } : {}), ...(rzMemL ? { memory: rzMemL } : {}) } } : {}),
            },
          }} />
        </div>

        <div className="card">
          <div className="card-head"><h2>🗓 Backup schedules &amp; retention</h2></div>
          <p className="subtle">Cron per backup type (blank = remove that schedule) and repo
            retention. Verified against the CR after apply.</p>
          <div className="row">
            <div className="field"><label>Full (cron)</label>
              <input className="mono" value={schFull} onChange={(e) => setSchFull(e.target.value)} placeholder="0 0 * * *" /></div>
            <div className="field"><label>Differential</label>
              <input className="mono" value={schDiff} onChange={(e) => setSchDiff(e.target.value)} placeholder="0 */6 * * *" /></div>
          </div>
          <div className="row">
            <div className="field"><label>Incremental</label>
              <input className="mono" value={schIncr} onChange={(e) => setSchIncr(e.target.value)} placeholder="0 * * * *" /></div>
            <div className="field"><label>Retention: full backups to keep</label>
              <input className="mono" value={retFull} onChange={(e) => setRetFull(e.target.value)} placeholder="4" /></div>
          </div>
          <Buttons params={{
            operation: "schedules", repo: "repo1",
            schedules: { full: schFull || null, differential: schDiff || null,
                         incremental: schIncr || null },
            ...(retFull ? { retention: { "repo1-retention-full": retFull } } : {}),
          }} />
        </div>

        <div className="card">
          <div className="card-head"><h2>More</h2></div>
          <p className="subtle">Backups, failover scenarios (measured), CR configuration bundles,
            and schedule pause/restore live on the <Link to={`/ops/targets/${targetId}`}>target
            page</Link>; every PostgreSQL/pgBackRest/pgBouncer parameter is click-appliable from
            the <Link to={`/ops/targets/${targetId}/params`}>parameter map</Link>. Restore/PITR
            and clone wizards are next on the roadmap.</p>
        </div>
      </div>
    </>
  );
}
