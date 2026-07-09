import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Me, KubeTarget } from "../types";
import { openJobStream, CheckEvent } from "../lib/sse";

const BLANK = {
  name: "", kubeconfig_path: "", kubeconfig_content: "", context: "",
  namespace: "percona", cr_kind: "perconapgcluster", cr_name: "",
  pguser_secret: "", db_user: "doadmin", db_name: "defaultdb",
};

const ICON: Record<string, string> = { ok: "✓", warn: "!", fail: "✕", info: "·" };

export function CheckList({ checks }: { checks: CheckEvent[] }) {
  return (
    <div className="checklist">
      {checks.map((c, i) => (
        <div key={i} className={`check status-${c.status}`}>
          <span className="ci">{ICON[c.status] ?? "·"}</span>
          <span className="cn">{c.name}</span>
          <span className="cd mono">{c.detail}</span>
        </div>
      ))}
    </div>
  );
}

export function ClusterOps({ me }: { me: Me }) {
  const [targets, setTargets] = useState<KubeTarget[] | null>(null);
  const [form, setForm] = useState({ ...BLANK });
  const [mode, setMode] = useState<"path" | "upload">("path");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [checks, setChecks] = useState<CheckEvent[] | null>(null);
  const [checkState, setCheckState] = useState<string>("");
  const isAdmin = me.role === "admin";

  function load() {
    api.get<KubeTarget[]>("/api/kube-targets").then(setTargets).catch((e) => setErr(e.message));
  }
  useEffect(load, []);

  function watchValidation(jobId: number) {
    setChecks([]);
    setCheckState("running");
    openJobStream(jobId, {
      onCheck: (c) => setChecks((prev) => [...(prev ?? []), c]),
      onDone: (d) => { setCheckState(d.status); load(); },
      onError: () => setCheckState("failed"),
    });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null); setBusy(true);
    try {
      const payload: Record<string, unknown> = { ...form };
      if (mode === "path") delete payload.kubeconfig_content;
      else delete payload.kubeconfig_path;
      const r = await api.post<{ id: number; validate_job_id: number }>("/api/kube-targets", payload);
      setForm({ ...BLANK });
      load();
      watchValidation(r.validate_job_id);
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function revalidate(t: KubeTarget) {
    try {
      const r = await api.post<{ job_id: number }>(`/api/kube-targets/${t.id}/validate`, {});
      watchValidation(r.job_id);
    } catch (ex) { alert((ex as Error).message); }
  }

  async function remove(t: KubeTarget) {
    if (!confirm(`Delete kube target “${t.name}”? Its imported kubeconfig copy is erased too.`)) return;
    try { await api.del(`/api/kube-targets/${t.id}`); load(); }
    catch (ex) { alert((ex as Error).message); }
  }

  const set = (k: keyof typeof form) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
      setForm({ ...form, [k]: e.target.value });

  return (
    <>
      <div className="toolbar"><h1>Cluster Ops — Kube Targets</h1></div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 16 }}>
        Kubernetes-hosted PostgreSQL clusters (Percona PG Operator) driven via kubeconfig.
        The kubeconfig reaches the worker as an environment variable only — its contents and
        the pguser password never touch the database, job specs, logs, streams, or artifacts.
        The web tier never runs kubectl: validation and discovery are worker jobs.
      </p>

      {err && <div className="banner-err">{err}</div>}
      {targets?.some((t) => t.schedules_paused) && (
        <div className="banner-err">
          ⚠ Operator backup schedules are PAUSED on:{" "}
          {targets.filter((t) => t.schedules_paused).map((t) => (
            <Link key={t.id} to={`/ops/targets/${t.id}`} style={{ marginRight: 8 }}>{t.name}</Link>
          ))}
          — restore them when your test window ends.
        </div>
      )}

      <div className="grid2">
        <div className="card">
          <div className="card-head"><h2>Registered clusters</h2></div>
          <table>
            <thead><tr><th>Name</th><th>Cluster CR</th><th>Namespace</th><th>API server</th><th>Validated</th><th></th></tr></thead>
            <tbody>
              {targets === null ? (
                <tr><td colSpan={6} className="empty mono">loading…</td></tr>
              ) : targets.length === 0 ? (
                <tr><td colSpan={6} className="empty">No kube targets yet — register one to begin.</td></tr>
              ) : targets.map((t) => (
                <tr key={t.id}>
                  <td><Link to={`/ops/targets/${t.id}`}><strong>{t.name}</strong></Link>
                    {t.schedules_paused && <span className="badge failed" style={{ marginLeft: 6 }}>schedules paused</span>}</td>
                  <td className="mono">{t.cr_kind}/{t.cr_name || "?"}</td>
                  <td className="mono">{t.namespace}</td>
                  <td className="mono">{t.api_server || "—"}</td>
                  <td className="mono">{t.last_validated_utc ?? "never"}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn-sm" onClick={() => revalidate(t)}>Validate</button>{" "}
                    {isAdmin && <button className="btn-sm danger" onClick={() => remove(t)}>Delete</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {checks !== null && (
            <div style={{ marginTop: 16 }}>
              <div className="card-head">
                <h2>Validation</h2>
                <span className={`badge ${checkState === "running" ? "running" : checkState}`}>{checkState}</span>
              </div>
              <CheckList checks={checks} />
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head"><h2>{isAdmin ? "Register a cluster" : "Register a cluster (admin only)"}</h2></div>
          <form onSubmit={submit}>
            <div className="row">
              <div className="field"><label>Name</label>
                <input required value={form.name} onChange={set("name")} placeholder="prod-doks-nyc1" disabled={!isAdmin} /></div>
              <div className="field"><label>Kubeconfig source</label>
                <select value={mode} onChange={(e) => setMode(e.target.value as "path" | "upload")} disabled={!isAdmin}>
                  <option value="path">path on the app host</option>
                  <option value="upload">paste contents (stored encrypted)</option>
                </select></div>
            </div>
            {mode === "path" ? (
              <div className="field"><label>Kubeconfig path</label>
                <input required value={form.kubeconfig_path} onChange={set("kubeconfig_path")}
                       placeholder="/var/lib/pgbench-harness/kubeconfigs/prod.yaml" disabled={!isAdmin} />
                <p className="subtle" style={{ margin: "4px 0 0" }}>The worker runs sandboxed (systemd
                  ProtectHome/ProtectSystem): place the file under the data dir&apos;s
                  <code> kubeconfigs/</code> directory or it will be invisible to it.</p>
              </div>
            ) : (
              <div className="field"><label>Kubeconfig contents</label>
                <textarea required rows={6} value={form.kubeconfig_content} onChange={set("kubeconfig_content")}
                          placeholder={"apiVersion: v1\nkind: Config\n…"} disabled={!isAdmin} className="mono"
                          style={{ width: "100%" }} />
                <p className="subtle" style={{ margin: "4px 0 0" }}>Stored Fernet-encrypted in the secret
                  store; decrypted to a 0600 temp file only for the duration of each job.</p>
              </div>
            )}
            <div className="row">
              <div className="field"><label>Context (blank = current-context)</label>
                <input value={form.context} onChange={set("context")} disabled={!isAdmin} /></div>
              <div className="field"><label>Namespace</label>
                <input value={form.namespace} onChange={set("namespace")} disabled={!isAdmin} /></div>
            </div>
            <div className="row">
              <div className="field"><label>CR kind</label>
                <select value={form.cr_kind} onChange={set("cr_kind")} disabled={!isAdmin}>
                  <option value="perconapgcluster">perconapgcluster (Percona v2)</option>
                  <option value="postgrescluster">postgrescluster (Crunchy)</option>
                </select></div>
              <div className="field"><label>CR name</label>
                <input value={form.cr_name} onChange={set("cr_name")} placeholder="(blank = auto-discover)" disabled={!isAdmin} /></div>
            </div>
            <div className="row">
              <div className="field"><label>DB user</label>
                <input value={form.db_user} onChange={set("db_user")} disabled={!isAdmin} /></div>
              <div className="field"><label>DB name</label>
                <input value={form.db_name} onChange={set("db_name")} disabled={!isAdmin} /></div>
            </div>
            <div className="field"><label>pguser secret (blank = &lt;cr&gt;-pguser-&lt;user&gt;)</label>
              <input value={form.pguser_secret} onChange={set("pguser_secret")} placeholder="(auto)" disabled={!isAdmin} /></div>
            <button className="primary" disabled={!isAdmin || busy} type="submit">
              {busy ? "Registering…" : "Register & validate"}
            </button>
          </form>
        </div>
      </div>
    </>
  );
}
