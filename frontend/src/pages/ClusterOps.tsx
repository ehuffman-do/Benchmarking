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

export function ValidationBadge({ t }: { t: KubeTarget }) {
  if (!t.last_validated_utc) return <span className="mono subtle">never</span>;
  const when = t.last_validated_utc.slice(0, 16).replace("T", " ");
  if (t.last_validation_ok === false)
    return <span className="badge failed" title={`validation failed at ${t.last_validated_utc}`}>✕ failed</span>;
  if (t.last_validation_ok === true)
    return <span className="mono" title={t.last_validated_utc}><span className="badge ok">✓</span> {when}</span>;
  // null with a timestamp: validated before the verdict column existed, or
  // the target was edited since — verdict unknown until the next validate.
  return <span className="mono subtle" title="target changed since — re-validate">{when} ?</span>;
}

type Mode = "keep" | "path" | "upload";

export function ClusterOps({ me }: { me: Me }) {
  const [targets, setTargets] = useState<KubeTarget[] | null>(null);
  const [form, setForm] = useState({ ...BLANK });
  const [editing, setEditing] = useState<KubeTarget | null>(null);
  const [mode, setMode] = useState<Mode>("path");
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

  function startEdit(t: KubeTarget) {
    setEditing(t);
    setMode("keep");
    setForm({
      name: t.name, kubeconfig_path: t.kubeconfig_path, kubeconfig_content: "",
      context: t.context, namespace: t.namespace, cr_kind: t.cr_kind,
      cr_name: t.cr_name, pguser_secret: t.pguser_secret,
      db_user: t.db_user, db_name: t.db_name,
    });
    setErr(null);
  }

  function cancelEdit() {
    setEditing(null);
    setMode("path");
    setForm({ ...BLANK });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null); setBusy(true);
    try {
      if (editing) {
        const payload: Record<string, unknown> = {
          context: form.context, namespace: form.namespace, cr_kind: form.cr_kind,
          cr_name: form.cr_name, pguser_secret: form.pguser_secret,
          db_user: form.db_user, db_name: form.db_name,
        };
        if (mode === "path") payload.kubeconfig_path = form.kubeconfig_path;
        if (mode === "upload") payload.kubeconfig_content = form.kubeconfig_content;
        await api.post(`/api/kube-targets/${editing.id}`, payload);
        const r = await api.post<{ job_id: number }>(`/api/kube-targets/${editing.id}/validate`, {});
        cancelEdit();
        load();
        watchValidation(r.job_id);
      } else {
        const payload: Record<string, unknown> = { ...form };
        if (mode === "upload") delete payload.kubeconfig_path;
        else delete payload.kubeconfig_content;
        const r = await api.post<{ id: number; validate_job_id: number }>("/api/kube-targets", payload);
        setForm({ ...BLANK });
        load();
        watchValidation(r.validate_job_id);
      }
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
                    {t.schedules_paused && <span className="badge failed" style={{ marginLeft: 6 }}>schedules paused</span>}
                    {(t.health_status === "warn" || t.health_status === "crit") &&
                      <span className="badge failed" style={{ marginLeft: 6 }}
                            title={`health check: ${t.health_status} (as of ${t.health_utc})`}>
                        health: {t.health_status}</span>}</td>
                  <td className="mono">{t.cr_kind}/{t.cr_name || "?"}</td>
                  <td className="mono">{t.namespace}</td>
                  <td className="mono">{t.api_server || "—"}</td>
                  <td><ValidationBadge t={t} /></td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn-sm" onClick={() => revalidate(t)}>Validate</button>{" "}
                    {isAdmin && <button className="btn-sm" onClick={() => startEdit(t)}>Edit</button>}{" "}
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
          <div className="card-head">
            <h2>{editing ? `Edit “${editing.name}”`
                 : isAdmin ? "Register a cluster" : "Register a cluster (admin only)"}</h2>
            {editing && <button className="btn-sm" type="button" onClick={cancelEdit}>cancel</button>}
          </div>
          <form onSubmit={submit}>
            <div className="row">
              <div className="field"><label>Name</label>
                <input required value={form.name} onChange={set("name")} placeholder="prod-doks-nyc1"
                       disabled={!isAdmin || !!editing} /></div>
              <div className="field"><label>Kubeconfig source</label>
                <select value={mode} onChange={(e) => setMode(e.target.value as Mode)} disabled={!isAdmin}>
                  {editing && <option value="keep">
                    keep current ({editing.kubeconfig_imported ? "imported copy" : "path"})</option>}
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
            ) : mode === "upload" ? (
              <div className="field"><label>Kubeconfig contents</label>
                <textarea required rows={6} value={form.kubeconfig_content} onChange={set("kubeconfig_content")}
                          placeholder={"apiVersion: v1\nkind: Config\n…"} disabled={!isAdmin} className="mono"
                          style={{ width: "100%" }} />
                <p className="subtle" style={{ margin: "4px 0 0" }}>Stored Fernet-encrypted in the secret
                  store; decrypted to a 0600 temp file only for the duration of each job.
                  {editing && " Pasting replaces the current kubeconfig."}</p>
              </div>
            ) : null}
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
              {busy ? "Saving…" : editing ? "Save & re-validate" : "Register & validate"}
            </button>
          </form>
        </div>
      </div>
    </>
  );
}
