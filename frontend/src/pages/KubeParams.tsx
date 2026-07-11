import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { KubeTarget, Me, PgParam, PgParamsCatalog } from "../types";
import { openJobStream, CheckEvent } from "../lib/sse";
import { CheckList } from "./ClusterOps";

interface SidecarOption {
  name: string;
  section: string;
  type: string;
  default: string | null;
  allowed: string[] | string | null;
  description: string;
  percona_path: string;
  crunchy_path: string;
}
type SidecarCatalog = Record<string, SidecarOption[]>;

const TABS = [
  ["pg", "PostgreSQL"],
  ["pgbackrest", "pgBackRest"],
  ["patroni", "Patroni DCS"],
  ["pgbouncer", "pgBouncer"],
] as const;
type Tab = (typeof TABS)[number][0];

const CHANNEL_HELP: Record<string, string> = {
  cr: "applied via the CR (spec.patroni.dynamicConfiguration) — Patroni reloads it",
  "dcs-coordinated": "Patroni coordinates this cluster-wide through DCS — expect a rolling restart",
  "patroni-locked": "Patroni owns this parameter and overrides any value you set",
  "operator-managed": "the operator owns this (TLS, archiving, recovery plumbing) — reverted on reconcile",
  readonly: "compiled into the server — display only",
};

// postgresql.org doc pages by the first segment of pg_settings.category;
// per-GUC anchors are #GUC-<NAME-WITH-DASHES>.
const DOC_PAGE: Record<string, string> = {
  "File Locations": "runtime-config-file-locations",
  "Connections and Authentication": "runtime-config-connection",
  "Resource Usage": "runtime-config-resource",
  "Write-Ahead Log": "runtime-config-wal",
  "Replication": "runtime-config-replication",
  "Query Tuning": "runtime-config-query",
  "Reporting and Logging": "runtime-config-logging",
  "Statistics": "runtime-config-statistics",
  "Autovacuum": "runtime-config-autovacuum",
  "Client Connection Defaults": "runtime-config-client",
  "Lock Management": "runtime-config-locks",
  "Version and Platform Compatibility": "runtime-config-compatible",
  "Error Handling": "runtime-config-error-handling",
  "Preset Options": "runtime-config-preset",
  "Customized Options": "runtime-config-custom",
  "Developer Options": "runtime-config-developer",
};

function docUrl(p: PgParam, pgVersion: string): string {
  const major = (pgVersion || "17").split(".")[0];
  const seg = (p.category || "").split(" / ")[0];
  const page = DOC_PAGE[seg];
  if (!page) {
    return `https://www.postgresql.org/search/?u=%2Fdocs%2F${major}%2F&q=${p.name}`;
  }
  return `https://www.postgresql.org/docs/${major}/${page}.html#GUC-${p.name.toUpperCase().replace(/_/g, "-")}`;
}

const QUICK_FILTERS = [
  ["all", "All"],
  ["modified", "Non-default"],
  ["cr", "CR-managed"],
  ["pending", "Pending restart"],
  ["restart", "Restart-required"],
  ["staged", "Staged"],
] as const;

/** Human hint for a raw value in the parameter's native unit ("16384 × 8kB = 128 MB"). */
function unitHint(p: PgParam, raw: string): string {
  if (!p.unit || raw === "" || Number.isNaN(Number(raw))) return "";
  const n = Number(raw);
  const m = /^(\d+)?\s*(kB|MB|GB|B|ms|s|min|h|d)$/.exec(p.unit);
  if (!m) return "";
  const mult = m[1] ? Number(m[1]) : 1;
  const base = m[2];
  if (base === "kB" || base === "MB" || base === "GB" || base === "B") {
    const kb = n * mult * (base === "B" ? 1 / 1024 : base === "kB" ? 1 : base === "MB" ? 1024 : 1048576);
    if (kb >= 1048576) return `= ${(kb / 1048576).toFixed(1)} GB`;
    if (kb >= 1024) return `= ${(kb / 1024).toFixed(1)} MB`;
    return `= ${kb.toFixed(0)} kB`;
  }
  const sec = n * mult * (base === "ms" ? 0.001 : base === "s" ? 1 : base === "min" ? 60 : base === "h" ? 3600 : 86400);
  if (sec >= 3600) return `= ${(sec / 3600).toFixed(1)} h`;
  if (sec >= 60) return `= ${(sec / 60).toFixed(1)} min`;
  return `= ${sec.toFixed(sec < 1 ? 3 : 0)} s`;
}

function validate(p: PgParam, v: string): string | null {
  if (v.trim() === "") return "value required";
  if (p.vartype === "bool") {
    return ["on", "off"].includes(v) ? null : "on or off";
  }
  if (p.vartype === "enum") {
    return p.enumvals.includes(v) ? null : `one of: ${p.enumvals.join(", ")}`;
  }
  if (p.vartype === "integer" || p.vartype === "real") {
    const n = Number(v);
    if (Number.isNaN(n)) return "must be a number";
    if (p.vartype === "integer" && !Number.isInteger(n)) return "must be an integer";
    if (p.min_val !== null && n < Number(p.min_val)) return `min ${p.min_val}`;
    if (p.max_val !== null && n > Number(p.max_val)) return `max ${p.max_val}`;
  }
  return null;
}

function Editor({ p, value, onChange }: { p: PgParam; value: string; onChange: (v: string) => void }) {
  if (p.vartype === "bool") {
    return (
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="on">on</option><option value="off">off</option>
      </select>
    );
  }
  if (p.vartype === "enum") {
    return (
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {p.enumvals.map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
    );
  }
  return (
    <input className="mono" style={{ width: 140 }} value={value}
           onChange={(e) => onChange(e.target.value)}
           placeholder={p.setting ?? ""} />
  );
}

export function KubeParams({ me }: { me: Me }) {
  const { targetId } = useParams();
  const nav = useNavigate();
  const [search] = useSearchParams();
  const [kt, setKt] = useState<KubeTarget | null>(null);
  const [cat, setCat] = useState<PgParamsCatalog | null>(null);
  const [collectedUtc, setCollectedUtc] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [quick, setQuick] = useState<string>(search.get("filter") === "pending" ? "pending" : "all");
  const [staged, setStaged] = useState<Record<string, string>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("pg");
  const [sidecar, setSidecar] = useState<SidecarCatalog | null>(null);
  const [stagedBk, setStagedBk] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [checks, setChecks] = useState<CheckEvent[] | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const isAdmin = me.role === "admin";
  const canOp = me.role !== "viewer";

  const load = useCallback(() => {
    api.get<KubeTarget>(`/api/kube-targets/${targetId}`).then(setKt).catch((e) => setErr(e.message));
    api.get<{ catalog: PgParamsCatalog | null; collected_utc: string | null }>(
      `/api/kube-targets/${targetId}/pg-params`)
      .then((r) => { setCat(r.catalog); setCollectedUtc(r.collected_utc); })
      .catch((e) => setErr(e.message));
    api.get<SidecarCatalog>("/api/ops/sidecar-catalog").then(setSidecar).catch(() => undefined);
  }, [targetId]);
  useEffect(load, [load]);

  async function refresh() {
    setErr(null); setRefreshing(true); setChecks([]);
    try {
      const r = await api.post<{ job_id: number }>(`/api/kube-targets/${targetId}/pg-params`, {});
      openJobStream(r.job_id, {
        onCheck: (c) => setChecks((prev) => [...(prev ?? []), c]),
        onDone: () => { setRefreshing(false); setChecks(null); load(); },
        onError: () => setRefreshing(false),
      });
    } catch (ex) {
      setErr((ex as Error).message); setRefreshing(false);
    }
  }

  const categories = useMemo(() => {
    const set = new Set<string>();
    cat?.params.forEach((p) => p.category && set.add(p.category));
    return [...set].sort();
  }, [cat]);

  const visible = useMemo(() => {
    if (!cat) return [];
    const needle = q.trim().toLowerCase();
    return cat.params.filter((p) => {
      if (category && p.category !== category) return false;
      if (quick === "modified" && (p.source ?? "default") === "default") return false;
      if (quick === "cr" && p.cr_value === null) return false;
      if (quick === "pending" && !p.pending_restart) return false;
      if (quick === "restart" && !p.restart_required) return false;
      if (quick === "staged" && !(p.name in staged)) return false;
      if (needle &&
          !p.name.toLowerCase().includes(needle) &&
          !p.short_desc.toLowerCase().includes(needle) &&
          !p.category.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [cat, q, category, quick, staged]);

  function stage(p: PgParam) {
    const v = drafts[p.name] ?? p.cr_value ?? p.setting ?? "";
    if (validate(p, v)) return;
    setStaged((s) => ({ ...s, [p.name]: v }));
    setExpanded(null);
  }

  function unstage(name: string) {
    setStaged((s) => {
      const n = { ...s };
      delete n[name];
      return n;
    });
  }

  async function apply(dryRun: boolean) {
    setErr(null);
    try {
      const body: Record<string, unknown> = {
        params: { action: "patroni_params", parameters: staged,
                  ...(dryRun ? { dry_run: true } : {}) },
        label: `params-${Object.keys(staged).length}-changes`,
      };
      if (!dryRun) body.confirm = confirm;
      const r = await api.post<{ job_id: number }>(
        `/api/kube-targets/${targetId}/cr-apply`, body);
      nav(`/ops/runs?job=${r.job_id}`);
    } catch (ex) {
      setErr((ex as Error).message);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  async function applyBk(dryRun: boolean) {
    setErr(null);
    try {
      const body: Record<string, unknown> = {
        params: { action: "pgbackrest_global", global: stagedBk,
                  ...(dryRun ? { dry_run: true } : {}) },
        label: `pgbackrest-${Object.keys(stagedBk).length}-changes`,
      };
      if (!dryRun) body.confirm = confirm;
      const r = await api.post<{ job_id: number }>(
        `/api/kube-targets/${targetId}/cr-apply`, body);
      nav(`/ops/runs?job=${r.job_id}`);
    } catch (ex) {
      setErr((ex as Error).message);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  if (!kt) return <p className="subtle mono">{err ?? "loading…"}</p>;
  const stagedNames = Object.keys(staged);
  const dcsStaged = stagedNames.filter((n) =>
    cat?.params.find((p) => p.name === n)?.channel === "dcs-coordinated" ||
    cat?.params.find((p) => p.name === n)?.restart_required);

  return (
    <>
      <div className="toolbar">
        <h1>Parameter map — {kt.name}</h1>
        <span className="mono subtle">
          {cat ? `${cat.params.length} parameters · PG ${cat.pg_version} · leader ${cat.leader}` : ""}
          {collectedUtc ? ` · snapshot ${collectedUtc.slice(0, 16).replace("T", " ")}` : ""}
        </span>
        <div className="spacer" />
        {canOp && <button onClick={refresh} disabled={refreshing}>
          {refreshing ? "Snapshotting…" : cat ? "Refresh snapshot" : "Take snapshot"}</button>}{" "}
        <Link className="btn" to={`/ops/targets/${targetId}`}>← target</Link>
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 12 }}>
        {tab === "pg"
          ? <>Every parameter below is introspected live from <code>pg_settings</code> on the
              leader — names, types, units, ranges and enum values come from the server itself,
              so a typo'd name or out-of-range value cannot be staged. Changes apply through the
              operator CR (Patroni dynamicConfiguration) with an automatic verify loop.</>
          : <>Sidecar options cannot be introspected from a live server, so this catalog is
              research-curated (types, defaults, allowed values) and every option shows the exact
              CR path it applies through on your operator ({kt.cr_kind}).</>}
      </p>

      <div style={{ marginBottom: 12 }}>
        {TABS.map(([k, l]) => (
          <button key={k} className={`btn-sm ${tab === k ? "primary" : ""}`}
                  style={{ marginRight: 6 }} onClick={() => setTab(k)}>
            {l}{sidecar && k !== "pg" ? ` (${(sidecar[k] ?? []).length})` : ""}
          </button>
        ))}
      </div>

      {err && <div className="banner-err">{err}</div>}
      {checks && checks.length > 0 && <div className="card"><CheckList checks={checks} /></div>}

      {tab !== "pg" && (
        <SidecarPanel kind={tab} options={sidecar?.[tab] ?? []} crKind={kt.cr_kind}
                      isAdmin={isAdmin} live={cat?.pgbackrest_global ?? {}}
                      staged={stagedBk} setStaged={setStagedBk}
                      confirm={confirm} setConfirm={setConfirm}
                      confirmName={kt.cr_name || kt.name} onApply={applyBk} />
      )}

      {tab === "pg" && stagedNames.length > 0 && (
        <div className="card" style={{ marginBottom: 12 }}>
          <div className="card-head"><h2>Staged changes ({stagedNames.length})</h2></div>
          <table>
            <thead><tr><th>Parameter</th><th>Current</th><th>New</th><th /></tr></thead>
            <tbody>
              {stagedNames.map((n) => {
                const p = cat?.params.find((x) => x.name === n);
                return (
                  <tr key={n}>
                    <td className="mono">{n}</td>
                    <td className="mono">{p?.cr_value ?? p?.setting ?? "—"}{p?.unit ? ` ${p.unit}` : ""}</td>
                    <td className="mono"><strong>{staged[n]}</strong>{p?.unit ? ` ${p.unit}` : ""}</td>
                    <td><button className="btn-sm" onClick={() => unstage(n)}>remove</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {dcsStaged.length > 0 && (
            <p className="subtle">⚠ {dcsStaged.join(", ")}: restart-coordinated — the operator
              will roll pods after apply (expect a brief failover).</p>
          )}
          <div style={{ marginTop: 8 }}>
            <button onClick={() => apply(true)}>Dry-run (patch + diff, no change)</button>{" "}
            {isAdmin && (
              <>
                <input value={confirm} onChange={(e) => setConfirm(e.target.value)}
                       placeholder={kt.cr_name || kt.name} className="mono" style={{ width: 160 }} />{" "}
                <button className="primary" onClick={() => apply(false)}>Apply & verify</button>
              </>
            )}
            <span className="subtle"> — verify polls pg_settings until every value is live and
              flags pending_restart loudly.</span>
          </div>
        </div>
      )}

      {tab === "pg" && <div className="card">
        <div className="card-head" style={{ flexWrap: "wrap", gap: 8 }}>
          <input placeholder="Search parameters (name, description, category)…"
                 value={q} onChange={(e) => setQ(e.target.value)} style={{ minWidth: 280 }} />
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">all categories</option>
            {categories.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <div className="spacer" />
          {QUICK_FILTERS.map(([k, l]) => (
            <button key={k} className={`btn-sm ${quick === k ? "primary" : ""}`}
                    onClick={() => setQuick(k)}>{l}</button>
          ))}
        </div>

        {!cat ? (
          <p className="empty">No parameter snapshot yet — take one to populate the map.</p>
        ) : (
          <>
            <p className="subtle mono">{visible.length} of {cat.params.length} parameters</p>
            <table>
              <thead><tr>
                <th>Parameter</th><th>Value</th><th>Type</th><th>Applies via</th><th>Category</th><th />
              </tr></thead>
              <tbody>
                {visible.map((p) => {
                  const editable = canOp && (p.channel === "cr" || p.channel === "dcs-coordinated");
                  const isOpen = expanded === p.name;
                  const draft = drafts[p.name] ?? staged[p.name] ?? p.cr_value ?? p.setting ?? "";
                  const verr = validate(p, draft);
                  return (
                    <>
                      <tr key={p.name} style={{ cursor: "pointer" }}
                          onClick={() => setExpanded(isOpen ? null : p.name)}>
                        <td className="mono">
                          {p.name}
                          {p.pending_restart && <span className="badge failed" style={{ marginLeft: 6 }}>pending restart</span>}
                          {p.name in staged && <span className="badge running" style={{ marginLeft: 6 }}>staged</span>}
                        </td>
                        <td className="mono">
                          {p.setting}{p.unit ? ` ${p.unit}` : ""}
                          {p.cr_value !== null && <span className="badge ok" style={{ marginLeft: 6 }} title="managed via the CR">CR</span>}
                          {p.cr_value === null && (p.source ?? "default") !== "default" &&
                            <span className="badge" style={{ marginLeft: 6 }} title={`source: ${p.source}`}>≠ default</span>}
                        </td>
                        <td className="mono">{p.vartype}{p.restart_required ? " ⟳" : ""}</td>
                        <td><span className="mono subtle" title={CHANNEL_HELP[p.channel]}>{p.channel}</span></td>
                        <td className="subtle" style={{ fontSize: 12 }}>{p.category}</td>
                        <td>{editable ? <button className="btn-sm" onClick={(e) => {
                          e.stopPropagation(); setExpanded(isOpen ? null : p.name);
                        }}>{isOpen ? "close" : "edit"}</button> : null}</td>
                      </tr>
                      {isOpen && (
                        <tr key={`${p.name}-detail`}>
                          <td colSpan={6} style={{ background: "var(--panel, rgba(127,127,127,.06))" }}>
                            <p style={{ margin: "6px 0" }}>{p.short_desc}</p>
                            <p className="subtle mono" style={{ margin: "4px 0" }}>
                              context {p.context}
                              {p.min_val !== null && ` · range ${p.min_val}–${p.max_val}`}
                              {p.enumvals.length > 0 && ` · ${p.enumvals.join(" | ")}`}
                              {" · default "}{p.boot_val}
                              {p.unit ? ` · unit ${p.unit}` : ""}
                              {" · "}
                              <a href={docUrl(p, cat.pg_version)}
                                 target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>docs ↗</a>
                            </p>
                            {["patroni-locked", "operator-managed", "readonly"].includes(p.channel) && (
                              <p className="subtle">🔒 {CHANNEL_HELP[p.channel]}.</p>
                            )}
                            {editable && (
                              <div onClick={(e) => e.stopPropagation()}>
                                <Editor p={p} value={draft}
                                        onChange={(v) => setDrafts((d) => ({ ...d, [p.name]: v }))} />{" "}
                                <span className="subtle mono">{unitHint(p, draft)}</span>{" "}
                                <button className="btn-sm primary" disabled={!!verr}
                                        onClick={() => stage(p)}>Stage change</button>
                                {verr && <span className="subtle" style={{ marginLeft: 8, color: "var(--err, #c33)" }}>{verr}</span>}
                                {p.channel === "dcs-coordinated" && (
                                  <p className="subtle" style={{ margin: "4px 0 0" }}>
                                    ⚠ {CHANNEL_HELP[p.channel]}.</p>
                                )}
                              </div>
                            )}
                          </td>
                        </tr>
                      )}
                    </>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
      </div>}
    </>
  );
}

function SidecarPanel({ kind, options, crKind, isAdmin, live, staged, setStaged,
                        confirm, setConfirm, confirmName, onApply }: {
  kind: string;
  options: SidecarOption[];
  crKind: string;
  isAdmin: boolean;
  live: Record<string, string>;
  staged: Record<string, string>;
  setStaged: (f: (s: Record<string, string>) => Record<string, string>) => void;
  confirm: string;
  setConfirm: (v: string) => void;
  confirmName: string;
  onApply: (dryRun: boolean) => void;
}) {
  const [q, setQ] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const pathFor = (o: SidecarOption) =>
    crKind === "postgrescluster" ? o.crunchy_path : o.percona_path;
  // Only options living in spec.backups.pgbackrest.global have a click-apply
  // path today (the existing pgbackrest_global action); the rest show their
  // CR path as guidance.
  const stageable = (o: SidecarOption) =>
    kind === "pgbackrest" && /backups\.pgbackrest\.global/.test(pathFor(o)) &&
    !/secure/i.test(o.type);
  const needle = q.trim().toLowerCase();
  const visible = options.filter((o) =>
    !needle || o.name.toLowerCase().includes(needle) ||
    o.description.toLowerCase().includes(needle) ||
    o.section.toLowerCase().includes(needle));
  const stagedNames = Object.keys(staged);

  return (
    <>
      {stagedNames.length > 0 && (
        <div className="card" style={{ marginBottom: 12 }}>
          <div className="card-head"><h2>Staged pgBackRest changes ({stagedNames.length})</h2></div>
          <table><tbody>
            {stagedNames.map((n) => (
              <tr key={n}><td className="mono">{n}</td>
                <td className="mono">{live[n] ?? "—"} → <strong>{staged[n]}</strong></td>
                <td><button className="btn-sm" onClick={() =>
                  setStaged((s) => { const x = { ...s }; delete x[n]; return x; })}>remove</button></td></tr>
            ))}
          </tbody></table>
          <div style={{ marginTop: 8 }}>
            <button onClick={() => onApply(true)}>Dry-run (patch + diff, no change)</button>{" "}
            {isAdmin && (
              <>
                <input value={confirm} onChange={(e) => setConfirm(e.target.value)}
                       placeholder={confirmName} className="mono" style={{ width: 160 }} />{" "}
                <button className="primary" onClick={() => onApply(false)}>Apply & verify</button>
              </>
            )}
            <span className="subtle"> — verified against the rendered pgBackRest config in the pod.</span>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <input placeholder={`Search ${kind} options…`} value={q}
                 onChange={(e) => setQ(e.target.value)} style={{ minWidth: 280 }} />
          <div className="spacer" />
          <span className="subtle mono">{visible.length} of {options.length}</span>
        </div>
        {kind === "patroni" && (
          <p className="subtle">Percona v2 honors only <code>postgresql.parameters</code> and{" "}
            <code>pg_hba</code> under dynamicConfiguration — DCS timing settings map to dedicated
            CR fields (shown per option below).</p>
        )}
        {kind === "pgbouncer" && (
          <p className="subtle">Applied via <code>proxy.pgBouncer.config.global</code> in the CR —
            click-to-apply for these lands next phase; the exact path is shown per option.</p>
        )}
        <table>
          <thead><tr><th>Option</th><th>Current / default</th><th>Type</th><th>Allowed</th><th>Applies via</th><th /></tr></thead>
          <tbody>
            {visible.map((o) => {
              const cur = kind === "pgbackrest" ? live[o.name] : undefined;
              const draft = drafts[o.name] ?? staged[o.name] ?? cur ?? o.default ?? "";
              return (
                <tr key={o.name}>
                  <td style={{ maxWidth: 380 }}>
                    <span className="mono">{o.name}</span>
                    {cur !== undefined && <span className="badge ok" style={{ marginLeft: 6 }}>CR</span>}
                    <div className="subtle" style={{ fontSize: 12 }}>{o.description}</div>
                  </td>
                  <td className="mono">{cur ?? o.default ?? "—"}</td>
                  <td className="mono">{o.type}</td>
                  <td className="mono" style={{ fontSize: 11, maxWidth: 160 }}>
                    {Array.isArray(o.allowed) ? o.allowed.join(" | ") : o.allowed ?? "—"}</td>
                  <td className="mono" style={{ fontSize: 11, maxWidth: 220 }}>{pathFor(o)}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {stageable(o) && (
                      <>
                        <input className="mono" style={{ width: 90 }} value={draft}
                               onChange={(e) => setDrafts((d) => ({ ...d, [o.name]: e.target.value }))} />{" "}
                        <button className="btn-sm" disabled={!String(draft).trim()}
                                onClick={() => setStaged((s) => ({ ...s, [o.name]: String(draft) }))}>
                          stage</button>
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
