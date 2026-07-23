import { useEffect, useMemo, useState } from "react";
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

export function relTime(iso: string | null): string {
  if (!iso) return "";
  const t = Date.parse(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** Health badge with freshness: a frozen 'crit' against a recovered cluster
 *  misdirected an incident response — the badge always says WHEN it was
 *  checked and links to the findings that produced it. */
export function HealthBadge({ t }: { t: KubeTarget }) {
  if (!t.health_status) return <span className="mono subtle">—</span>;
  const ageS = t.health_utc
    ? (Date.now() - Date.parse(t.health_utc.endsWith("Z") ? t.health_utc : t.health_utc + "Z")) / 1000
    : Infinity;
  const staleAfter = Math.max(2 * (t.auto_health_s || 0), 3600);
  const stale = ageS > staleAfter;
  const cls = t.health_status === "ok" ? "ok"
    : t.health_status === "info" ? "running" : "failed";
  return (
    <Link to={`/ops/targets/${t.id}`} title="open the health findings"
          style={{ textDecoration: "none", whiteSpace: "nowrap" }}>
      <span className={`badge ${cls}`}>
        {t.health_status === "ok" ? "✓ healthy" : `health: ${t.health_status}`}</span>
      <span className="mono subtle" style={{ fontSize: 11, marginLeft: 6 }}>
        {relTime(t.health_utc)}{stale ? " · stale" : ""}</span>
    </Link>
  );
}

function midEllipsis(s: string, max = 34): string {
  if (s.length <= max) return s;
  const half = Math.floor((max - 1) / 2);
  return `${s.slice(0, half)}…${s.slice(-half)}`;
}

function ApiServerCell({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  if (!url) return <span className="mono subtle">—</span>;
  return (
    <span className="mono" title={url} style={{ whiteSpace: "nowrap" }}>
      {midEllipsis(url)}
      <button className="btn-sm" title="copy full URL" onClick={() => {
        navigator.clipboard?.writeText(url).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}>{copied ? "✓" : "copy"}</button>
    </span>
  );
}

type Mode = "keep" | "path" | "upload";

const FIELD_HELP: Record<string, string> = {
  context: "Blank = the kubeconfig's current-context.",
  cr_name: "Blank = auto-discovered by the first validation.",
};

export function ClusterOps({ me }: { me: Me }) {
  const [targets, setTargets] = useState<KubeTarget[] | null>(null);
  const [form, setForm] = useState({ ...BLANK });
  const [editing, setEditing] = useState<KubeTarget | null>(null);
  const [formOpen, setFormOpen] = useState(false);
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
  // Keep the freshness column honest while the page sits open.
  useEffect(() => {
    const iv = setInterval(load, 60_000);
    return () => clearInterval(iv);
  }, []);

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
    setFormOpen(true);
    setMode("keep");
    setForm({
      name: t.name, kubeconfig_path: t.kubeconfig_path, kubeconfig_content: "",
      context: t.context, namespace: t.namespace, cr_kind: t.cr_kind,
      cr_name: t.cr_name, pguser_secret: t.pguser_secret,
      db_user: t.db_user, db_name: t.db_name,
    });
    setErr(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function cancelEdit() {
    setEditing(null);
    setFormOpen(false);
    setMode("path");
    setForm({ ...BLANK });
  }

  // ── inline validation ──
  const nameOk = form.name.trim().length > 0;
  const sourceOk = mode === "keep"
    || (mode === "path" ? form.kubeconfig_path.trim().length > 0
                        : form.kubeconfig_content.trim().length > 0);
  const canSubmit = isAdmin && !busy && nameOk && sourceOk;
  // The worker runs sandboxed (systemd ProtectHome/ProtectSystem): a path
  // under /root or /home is invisible to it. Flag it BEFORE the round-trip.
  const pathSuspicious = mode === "path" && /^\/(root|home)(\/|$)/.test(form.kubeconfig_path.trim());

  const derivedSecret = `${form.cr_name || "<cr>"}-pguser-${form.db_user || "<user>"}`;

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
        setFormOpen(false);
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

  const req = <span className="req" title="required"> *</span>;

  const emptyState = useMemo(() => targets !== null && targets.length === 0, [targets]);

  return (
    <>
      <div className="toolbar">
        <h1>Cluster Ops — Kube Targets</h1>
        <div className="spacer" />
        {isAdmin && !formOpen && (
          <button className="primary" onClick={() => { setFormOpen(true); setErr(null); }}>
            + Register a cluster</button>
        )}
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 16 }}>
        Kubernetes-hosted PostgreSQL clusters (Percona PG Operator) driven via kubeconfig.
        The kubeconfig reaches the worker as an environment variable only — its contents and
        the pguser password never touch the database, job specs, logs, streams, or artifacts.
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

      {(formOpen || editing) && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-head">
            <h2>{editing ? `Edit “${editing.name}”` : "Register a cluster"}</h2>
            <div className="spacer" />
            <button className="btn-sm" type="button" onClick={cancelEdit}>cancel</button>
          </div>
          <form onSubmit={submit}>
            <div className="reg-steps">
              <fieldset className="reg-step">
                <legend>1 · Connection</legend>
                <div className="field"><label>Name{req}</label>
                  <input required value={form.name} onChange={set("name")} placeholder="prod-doks-nyc1"
                         disabled={!isAdmin || !!editing} /></div>
                <div className="field"><label>Kubeconfig source{req}</label>
                  <select value={mode} onChange={(e) => setMode(e.target.value as Mode)} disabled={!isAdmin}>
                    {editing && <option value="keep">
                      keep current ({editing.kubeconfig_imported ? "imported copy" : "path"})</option>}
                    <option value="path">path on the app host</option>
                    <option value="upload">paste contents (stored encrypted)</option>
                  </select></div>
                {mode === "path" ? (
                  <div className="field"><label>Kubeconfig path{req}</label>
                    <input required value={form.kubeconfig_path} onChange={set("kubeconfig_path")}
                           placeholder="/var/lib/pgbench-harness/kubeconfigs/prod.yaml" disabled={!isAdmin} />
                    <p className={pathSuspicious ? "field-warn" : "field-help"}>
                      {pathSuspicious
                        ? "⚠ This path is under /root or /home — invisible to the sandboxed " +
                          "worker (systemd ProtectHome). Copy the file under the data dir's " +
                          "kubeconfigs/ directory, or paste the contents instead."
                        : <>Must live under the data dir&apos;s <code>kubeconfigs/</code> directory —
                            the worker is sandboxed and cannot read /root or /home.</>}
                    </p>
                  </div>
                ) : mode === "upload" ? (
                  <div className="field"><label>Kubeconfig contents{req}</label>
                    <textarea required rows={6} value={form.kubeconfig_content} onChange={set("kubeconfig_content")}
                              placeholder={"apiVersion: v1\nkind: Config\n…"} disabled={!isAdmin} className="mono"
                              style={{ width: "100%" }} />
                    <p className="field-help">Stored Fernet-encrypted in the secret store; decrypted to a
                      0600 temp file only for the duration of each job.
                      {editing && " Pasting replaces the current kubeconfig."}</p>
                  </div>
                ) : null}
                <div className="field"><label>Context</label>
                  <input value={form.context} onChange={set("context")}
                         placeholder="(current-context)" disabled={!isAdmin} />
                  <p className="field-help">{FIELD_HELP.context}</p></div>
                <div className="field"><label>Namespace</label>
                  <input value={form.namespace} onChange={set("namespace")} disabled={!isAdmin} /></div>
              </fieldset>

              <fieldset className="reg-step">
                <legend>2 · Target cluster CR</legend>
                <div className="field"><label>CR kind</label>
                  <select value={form.cr_kind} onChange={set("cr_kind")} disabled={!isAdmin} style={{ width: "100%" }}>
                    <option value="perconapgcluster">perconapgcluster (Percona v2)</option>
                    <option value="postgrescluster">postgrescluster (Crunchy)</option>
                  </select></div>
                <div className="field"><label>CR name</label>
                  <input value={form.cr_name} onChange={set("cr_name")}
                         placeholder="(auto-discover)" disabled={!isAdmin} />
                  <p className="field-help">{FIELD_HELP.cr_name} Validation lists the
                    PerconaPGClusters found in the namespace and fills this in when there is
                    exactly one.</p></div>
              </fieldset>

              <fieldset className="reg-step">
                <legend>3 · Database access</legend>
                <div className="field"><label>DB user</label>
                  <input value={form.db_user} onChange={set("db_user")} disabled={!isAdmin} /></div>
                <div className="field"><label>DB name</label>
                  <input value={form.db_name} onChange={set("db_name")} disabled={!isAdmin} /></div>
                <div className="field"><label>pguser secret</label>
                  <input value={form.pguser_secret} onChange={set("pguser_secret")}
                         placeholder={derivedSecret} disabled={!isAdmin} />
                  <p className="field-help">Blank uses the operator&apos;s default naming:{" "}
                    <code>{derivedSecret}</code>. Only set this when the Secret has a
                    different name.</p></div>
              </fieldset>
            </div>
            <div style={{ marginTop: 4 }}>
              <button className="primary" disabled={!canSubmit} type="submit"
                      title={canSubmit ? "" : "fill the required fields first"}>
                {busy ? "Saving…" : editing ? "Save & re-validate" : "Register & validate"}
              </button>
              {!canSubmit && !busy && isAdmin && (
                <span className="subtle" style={{ marginLeft: 10, fontSize: 12 }}>
                  {nameOk ? "provide the kubeconfig" : "name is required"}
                </span>
              )}
            </div>
          </form>
        </div>
      )}

      <div className="card">
        <div className="card-head"><h2>Registered clusters</h2></div>
        {emptyState ? (
          <div className="empty" style={{ padding: "28px 8px" }}>
            <p style={{ marginTop: 0 }}>No kube targets yet.</p>
            <p className="subtle">Register the cluster&apos;s kubeconfig to unlock topology
              discovery, health intelligence, the parameter map, guarded config applies,
              backups, and failover scenarios.</p>
            {isAdmin && !formOpen && (
              <button className="primary" onClick={() => setFormOpen(true)}>+ Register a cluster</button>
            )}
          </div>
        ) : (
          <table>
            <thead><tr><th>Name</th><th>Health</th><th>Cluster CR</th><th>Namespace</th>
              <th>API server</th><th>Validated</th><th /></tr></thead>
            <tbody>
              {targets === null ? (
                <tr><td colSpan={7} className="empty mono">loading…</td></tr>
              ) : targets.map((t) => (
                <tr key={t.id}>
                  <td><Link to={`/ops/targets/${t.id}`}><strong>{t.name}</strong></Link>
                    {t.schedules_paused && <span className="badge failed" style={{ marginLeft: 6 }}>schedules paused</span>}</td>
                  <td><HealthBadge t={t} /></td>
                  <td className="mono">{t.cr_kind}/{t.cr_name || "?"}</td>
                  <td className="mono">{t.namespace}</td>
                  <td><ApiServerCell url={t.api_server || ""} /></td>
                  <td><ValidationBadge t={t} /></td>
                  <td className="row-actions">
                    <button className="btn-sm" onClick={() => revalidate(t)}>Validate</button>
                    {isAdmin && <button className="btn-sm" onClick={() => startEdit(t)}>Edit</button>}
                    {isAdmin && <button className="btn-sm danger" onClick={() => remove(t)}>Delete</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

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
    </>
  );
}
