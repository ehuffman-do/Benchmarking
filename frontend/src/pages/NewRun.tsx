import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Me, Target } from "../types";

// Minimal YAML emitter for flat/nested scalar+array specs (no dep; CSP-safe).
function toYaml(o: Record<string, unknown>, indent = ""): string {
  const lines: string[] = [];
  for (const [k, v] of Object.entries(o)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) lines.push(`${indent}${k}: [${v.map(yamlScalar).join(", ")}]`);
    else if (typeof v === "object") {
      const inner = toYaml(v as Record<string, unknown>, indent + "  ");
      if (inner) { lines.push(`${indent}${k}:`); lines.push(inner); }
    } else lines.push(`${indent}${k}: ${yamlScalar(v)}`);
  }
  return lines.join("\n");
}

// Quote a scalar when it would otherwise be misparsed as YAML (contains `:` `#`,
// leading/trailing space, a leading indicator char, or looks like a bool/number).
// JSON double-quoting is valid YAML flow-scalar syntax.
function yamlScalar(v: unknown): string {
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  const s = String(v);
  const needsQuote =
    s === "" || /[:#\[\]{}&*!|>'"%@`,]/.test(s) || /^[\s\-?]/.test(s) || /\s$/.test(s) ||
    ["true", "false", "null", "yes", "no", "on", "off", "~"].includes(s.toLowerCase()) ||
    /^[+-]?(\d|\.\d)/.test(s);   // numeric-looking strings stay strings
  return needsQuote ? JSON.stringify(s) : s;
}

const WORKLOADS = ["tpcc", "oltp_read_only", "oltp_read_write", "oltp_write_only", "oltp_point_select", "io_stress"];

export function NewRun({ me }: { me: Me }) {
  const canRun = me.role === "operator" || me.role === "admin";
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const cloneFrom = params.get("from") || "";
  const wantCluster = Number(params.get("cluster") || 0);
  const wantMode = params.get("mode") || "";

  const [targets, setTargets] = useState<Target[]>([]);
  const [targetMode, setTargetMode] = useState<"saved" | "inline">("inline");
  const [targetId, setTargetId] = useState<number>(0);
  const [inline, setInline] = useState({ host: "", port: 5432, database: "defaultdb", user: "doadmin", sslmode: "require", password: "" });
  const [meta, setMeta] = useState({ label: "", edition: "advanced", tshirt_size: "8c32g", tags: "", ticket: "" });
  const [wl, setWl] = useState({ type: "tpcc", tpcc_path: "/opt/sysbench-tpcc", tables: 10, scale: 30, table_size: 1000000, dataset_gb: 64, mix: "mixed", rand_type: "uniform" });
  const [mode, setMode] = useState<"sweep" | "soak" | "suite">("sweep");
  const [suite, setSuite] = useState({ duration_s: 300, threads: "1, 2, 4, 8, 16, 32", warmup_s: 30, cooldown_s: 30, pgbench: true, pgbench_scale: 1000 });
  const [rateSteps, setRateSteps] = useState("");          // soak-only, optional
  const [stepDur, setStepDur] = useState(180);
  const [sweep, setSweep] = useState({ threads: "1, 4, 16, 64", duration_s: 300, warmup_s: 60, cooldown_s: 30, repetitions: 1 });
  const [soak, setSoak] = useState({ threads: 64, duration_s: 3600, tolerate_errors: true });
  const [schedule, setSchedule] = useState("");
  const [kubeTargets, setKubeTargets] = useState<{ id: number; name: string }[]>([]);
  const [kubeTargetId, setKubeTargetId] = useState<number>(0);
  const [tplName, setTplName] = useState("");
  const [createDb, setCreateDb] = useState(false);
  const [recreateScope, setRecreateScope] = useState("");   // "" | "database" | "tables"
  const [confirmDb, setConfirmDb] = useState("");

  const [yaml, setYaml] = useState("");
  const [autoSync, setAutoSync] = useState(true);
  const [validateOut, setValidateOut] = useState<{ ok: boolean; msg: string } | null>(null);
  const [dryOut, setDryOut] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<Target[]>("/api/targets").then((t) => {
      setTargets(t);
      if (t.length) { setTargetMode("saved"); setTargetId(t[0].id); }
    }).catch(() => {});
    api.get<{ id: number; name: string }[]>("/api/kube-targets")
      .then(setKubeTargets).catch(() => {});
    if (wantCluster) setKubeTargetId(wantCluster);
    if (wantMode === "suite") { setMode("suite"); setWl((w) => ({ ...w, type: "io_stress" })); }
    if (wantMode === "rate-steps") {
      setMode("soak"); setRateSteps("500, 1000, 2000, 4000, 8000, 0");
      setWl((w) => ({ ...w, type: "io_stress" }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Clone: load a prior run's spec into the editor (raw becomes authoritative).
  useEffect(() => {
    if (!cloneFrom) return;
    api.raw(`/runs/${cloneFrom}/spec`).then((r) => r.text()).then((txt) => {
      setYaml(txt); setAutoSync(false);
    }).catch(() => {});
  }, [cloneFrom]);

  const doc = useMemo(() => {
    const tgt = targetMode === "saved" ? targets.find((t) => t.id === targetId) : null;
    const target = tgt
      ? { host: tgt.host, port: tgt.port, database: tgt.dbname, user: tgt.dbuser, sslmode: tgt.sslmode, password_env: "PGB_TARGET_PASSWORD" }
      : { host: inline.host, port: inline.port, database: inline.database, user: inline.user, sslmode: inline.sslmode, password_env: "PGB_TARGET_PASSWORD" };
    const run: Record<string, unknown> = { label: meta.label || "run", edition: meta.edition, tshirt_size: meta.tshirt_size };
    const tags = meta.tags.split(",").map((s) => s.trim()).filter(Boolean);
    if (tags.length) run.tags = tags;
    if (meta.ticket) run.ticket = meta.ticket;
    const workload = wl.type === "tpcc"
      ? { type: wl.type, tpcc_path: wl.tpcc_path, tables: wl.tables, scale: wl.scale }
      : wl.type === "io_stress"
        ? { type: wl.type, tables: wl.tables, dataset_gb: wl.dataset_gb, mix: wl.mix, rand_type: wl.rand_type }
        : { type: wl.type, tables: 1, table_size: wl.table_size };
    const d: Record<string, unknown> = { run, target, workload };
    const ladder = (v: string) => v.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !Number.isNaN(n));
    if (mode === "soak") {
      const steps = ladder(rateSteps);
      d.soak = steps.length
        ? { threads: soak.threads, rate_steps: steps, step_duration_s: stepDur }
        : { threads: soak.threads, duration_s: soak.duration_s, tolerate_errors: soak.tolerate_errors };
    } else if (mode === "suite") {
      d.suite = { duration_s: suite.duration_s, threads: ladder(suite.threads),
                  warmup_s: suite.warmup_s, cooldown_s: suite.cooldown_s,
                  pgbench: suite.pgbench, pgbench_scale: suite.pgbench_scale };
    } else d.sweep = {
      threads: ladder(sweep.threads),
      duration_s: sweep.duration_s, warmup_s: sweep.warmup_s, cooldown_s: sweep.cooldown_s, repetitions: sweep.repetitions,
    };
    return d;
  }, [targetMode, targetId, targets, inline, meta, wl, mode, sweep, soak, suite, rateSteps, stepDur]);

  useEffect(() => {
    if (autoSync) setYaml(toYaml(doc) + "\n");
  }, [doc, autoSync]);

  async function validate() {
    setErr(null);
    try {
      const d = await api.post<{ ok: boolean; mode: string; label: string; workload: string; error?: string; hint?: string }>(
        "/api/validate", { spec_yaml: yaml });
      setValidateOut(d.ok
        ? { ok: true, msg: `valid — ${d.mode} run “${d.label}” (${d.workload})` }
        : { ok: false, msg: (d.error || "invalid") + (d.hint ? ` — ${d.hint}` : "") });
    } catch (e) { setValidateOut({ ok: false, msg: (e as Error).message }); }
  }
  async function dryRun() {
    setErr(null);
    try {
      const d = await api.post<{ mode: string; budget_s: number; commands: string[] }>("/api/dry-run", { spec_yaml: yaml });
      setDryOut(`# ${d.mode} — planned wall-clock ~${Math.round(d.budget_s / 60)} min (${d.budget_s}s)\n` + d.commands.join("\n"));
    } catch (e) { setDryOut("error: " + (e as Error).message); }
  }
  function credBody(): Record<string, unknown> {
    if (targetMode === "saved") {
      if (!targetId) throw new Error("select a saved target first (or choose “Enter host”)");
      return { target_id: targetId };
    }
    if (!inline.host.trim()) throw new Error("enter a host (or choose a saved target)");
    return { password: inline.password };
  }
  async function start() {
    setErr(null);
    try {
      await api.post("/api/runs", { spec_yaml: yaml, scheduled_utc: schedule.trim() || null,
                                    ...(kubeTargetId ? { kube_target_id: kubeTargetId } : {}),
                                    ...credBody() });
      window.location.href = "/ui";
    } catch (e) { setErr("could not start: " + (e as Error).message); }
  }
  // Target database the prepare options act on (for the typed-confirm guard).
  const targetDb = targetMode === "saved"
    ? (targets.find((t) => t.id === targetId)?.dbname ?? "")
    : inline.database;

  async function task(kind: "preflight" | "prepare") {
    setErr(null);
    try {
      const body: Record<string, unknown> = { spec_yaml: yaml, ...credBody() };
      if (kind === "prepare") {
        if (createDb) body.create_db = true;
        if (recreateScope) {
          if (confirmDb !== targetDb) throw new Error(`type the database name "${targetDb}" to confirm the drop`);
          body.recreate = recreateScope;
          body.confirm = confirmDb;
        }
      }
      const d = await api.post<{ job_id: number }>(`/api/${kind}`, body);
      navigate(`/jobs/${d.job_id}`);
    } catch (e) { setErr(`could not start ${kind}: ` + (e as Error).message); }
  }
  async function saveTemplate() {
    try {
      const d = await api.post<{ name: string; version: number }>("/api/templates", { name: tplName, spec_yaml: yaml });
      setErr(null); setDryOut(`saved template “${d.name}” v${d.version}`);
    } catch (e) { setErr((e as Error).message); }
  }

  const seti = (k: keyof typeof inline) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setInline({ ...inline, [k]: k === "port" ? Number(e.target.value) : e.target.value });

  return (
    <>
      <div className="toolbar"><h1>New run</h1>{cloneFrom && <span className="chip">cloned from <span className="mono">{cloneFrom}</span></span>}</div>
      {err && <div className="banner-err">{err}</div>}

      <div className="grid2">
        <div className="card">
          <div className="card-head"><h2>Configure</h2><div className="spacer" />
            {!autoSync && <button className="ghost" onClick={() => setAutoSync(true)}>↻ Rebuild from fields</button>}
          </div>

          <label>Target cluster</label>
          <div className="seg">
            <button className={targetMode === "saved" ? "on" : ""} onClick={() => setTargetMode("saved")} disabled={!targets.length}>Saved target</button>
            <button className={targetMode === "inline" ? "on" : ""} onClick={() => setTargetMode("inline")}>Enter host</button>
          </div>
          {targetMode === "saved" ? (
            <div className="field">
              <select value={targetId} onChange={(e) => setTargetId(Number(e.target.value))}>
                {targets.map((t) => <option key={t.id} value={t.id}>{t.name} — {t.host}</option>)}
              </select>
              <p className="subtle" style={{ fontSize: 12, marginTop: 4 }}>Uses the cluster's saved encrypted password.</p>
            </div>
          ) : (
            <>
              <div className="field"><label>Host</label><input value={inline.host} onChange={seti("host")} placeholder="private-db.nyc3.db.ondigitalocean.com" /></div>
              <div className="row">
                <div className="field"><label>Port</label><input type="number" value={inline.port} onChange={seti("port")} /></div>
                <div className="field"><label>SSL</label>
                  <select value={inline.sslmode} onChange={seti("sslmode")}>{["require", "verify-full", "verify-ca", "prefer", "disable"].map((s) => <option key={s}>{s}</option>)}</select></div>
              </div>
              <div className="row">
                <div className="field"><label>Database</label><input value={inline.database} onChange={seti("database")} /></div>
                <div className="field"><label>User</label><input value={inline.user} onChange={seti("user")} /></div>
              </div>
              <div className="field"><label>Password (stored encrypted, never in spec/DB/report)</label>
                <input type="password" value={inline.password} onChange={seti("password")} autoComplete="off" /></div>
            </>
          )}

          <hr />
          <div className="row">
            <div className="field"><label>Label</label><input value={meta.label} onChange={(e) => setMeta({ ...meta, label: e.target.value })} placeholder="advanced-8c32g-tpcc" /></div>
            <div className="field"><label>Edition</label><select value={meta.edition} onChange={(e) => setMeta({ ...meta, edition: e.target.value })}><option>advanced</option><option>standard</option></select></div>
          </div>
          <div className="row">
            <div className="field"><label>T-shirt size</label><input value={meta.tshirt_size} onChange={(e) => setMeta({ ...meta, tshirt_size: e.target.value })} /></div>
            <div className="field"><label>Ticket</label><input value={meta.ticket} onChange={(e) => setMeta({ ...meta, ticket: e.target.value })} placeholder="DBAAS-1234" /></div>
          </div>
          <div className="field"><label>Tags (comma)</label><input value={meta.tags} onChange={(e) => setMeta({ ...meta, tags: e.target.value })} placeholder="nightly, scaling" /></div>

          <hr />
          <div className="row">
            <div className="field"><label>Workload</label><select value={wl.type} onChange={(e) => setWl({ ...wl, type: e.target.value })}>{WORKLOADS.map((w) => <option key={w}>{w}</option>)}</select></div>
            <div className="field"><label>Mode</label><select value={mode} onChange={(e) => setMode(e.target.value as "sweep" | "soak" | "suite")}><option value="sweep">sweep</option><option value="soak">soak</option><option value="suite">suite (IOPS evidence matrix)</option></select></div>
          </div>
          {wl.type === "tpcc" ? (
            <div className="row">
              <div className="field"><label>TPCC path</label><input value={wl.tpcc_path} onChange={(e) => setWl({ ...wl, tpcc_path: e.target.value })} /></div>
              <div className="field"><label>Tables</label><input type="number" value={wl.tables} onChange={(e) => setWl({ ...wl, tables: Number(e.target.value) })} /></div>
              <div className="field"><label>Scale (warehouses)</label><input type="number" value={wl.scale} onChange={(e) => setWl({ ...wl, scale: Number(e.target.value) })} /></div>
            </div>
          ) : wl.type === "io_stress" ? (
            <div className="row">
              <div className="field"><label>Dataset (GiB — size &ge; 2x RAM to defeat caches)</label><input type="number" value={wl.dataset_gb} onChange={(e) => setWl({ ...wl, dataset_gb: Number(e.target.value) })} /></div>
              <div className="field"><label>Tables</label><input type="number" value={wl.tables} onChange={(e) => setWl({ ...wl, tables: Number(e.target.value) })} /></div>
              <div className="field"><label>Mix</label><select value={wl.mix} onChange={(e) => setWl({ ...wl, mix: e.target.value })}><option value="read">read</option><option value="write">write</option><option value="mixed">mixed</option></select></div>
              <div className="field"><label>Key distribution</label><select value={wl.rand_type} onChange={(e) => setWl({ ...wl, rand_type: e.target.value })}>{["uniform", "special", "gaussian", "pareto", "zipfian"].map((r) => <option key={r}>{r}</option>)}</select></div>
            </div>
          ) : (
            <div className="field"><label>Table size (rows)</label><input type="number" value={wl.table_size} onChange={(e) => setWl({ ...wl, table_size: Number(e.target.value) })} /></div>
          )}
          {mode === "sweep" ? (
            <>
              <div className="field"><label>Thread ladder (comma)</label><input value={sweep.threads} onChange={(e) => setSweep({ ...sweep, threads: e.target.value })} /></div>
              <div className="row">
                <div className="field"><label>Duration (s)</label><input type="number" value={sweep.duration_s} onChange={(e) => setSweep({ ...sweep, duration_s: Number(e.target.value) })} /></div>
                <div className="field"><label>Warmup (s)</label><input type="number" value={sweep.warmup_s} onChange={(e) => setSweep({ ...sweep, warmup_s: Number(e.target.value) })} /></div>
                <div className="field"><label>Reps</label><input type="number" value={sweep.repetitions} onChange={(e) => setSweep({ ...sweep, repetitions: Number(e.target.value) })} /></div>
              </div>
            </>
          ) : mode === "suite" ? (
            <>
              <div className="field"><label>Thread ladder (comma)</label><input value={suite.threads} onChange={(e) => setSuite({ ...suite, threads: e.target.value })} /></div>
              <div className="row">
                <div className="field"><label>Duration per cell (s)</label><input type="number" value={suite.duration_s} onChange={(e) => setSuite({ ...suite, duration_s: Number(e.target.value) })} /></div>
                <div className="field"><label>Warmup (s)</label><input type="number" value={suite.warmup_s} onChange={(e) => setSuite({ ...suite, warmup_s: Number(e.target.value) })} /></div>
                <div className="field"><label>Stabilization (s)</label><input type="number" value={suite.cooldown_s} onChange={(e) => setSuite({ ...suite, cooldown_s: Number(e.target.value) })} /></div>
              </div>
              <div className="row">
                <div className="field"><label><input type="checkbox" checked={suite.pgbench} onChange={(e) => setSuite({ ...suite, pgbench: e.target.checked })} />&nbsp;pgbench second driver (TPC-B + SELECT-only)</label></div>
                {suite.pgbench && <div className="field"><label>pgbench scale</label><input type="number" value={suite.pgbench_scale} onChange={(e) => setSuite({ ...suite, pgbench_scale: Number(e.target.value) })} /></div>}
              </div>
              <p className="subtle" style={{ fontSize: 12 }}>Runs the storage team&apos;s
                full matrix (4 sysbench OLTP workloads{suite.pgbench ? " + 2 pgbench workloads" : ""} x the
                ladder) as one evidence bundle. Attach a cluster below for the
                device-IOPS verdict.</p>
            </>
          ) : (
            <>
              <div className="row">
                <div className="field"><label>Threads</label><input type="number" value={soak.threads} onChange={(e) => setSoak({ ...soak, threads: Number(e.target.value) })} /></div>
                <div className="field"><label>Duration (s)</label><input type="number" value={soak.duration_s} onChange={(e) => setSoak({ ...soak, duration_s: Number(e.target.value) })} disabled={!!rateSteps.trim()} /></div>
              </div>
              <div className="row">
                <div className="field"><label>Rate steps (tps, comma; 0 = unthrottled; blank = plain soak)</label><input value={rateSteps} onChange={(e) => setRateSteps(e.target.value)} placeholder="500, 1000, 2000, 4000, 8000, 0" /></div>
                {rateSteps.trim() && <div className="field"><label>Step duration (s)</label><input type="number" value={stepDur} onChange={(e) => setStepDur(Number(e.target.value))} /></div>}
              </div>
            </>
          )}

          <hr />
          <div className="field"><label>Start at (UTC, optional — blank = now)</label><input value={schedule} onChange={(e) => setSchedule(e.target.value)} placeholder="2026-06-28T02:00:00Z" /></div>
          <div className="field"><label>Attach cluster (evidence capture, optional)</label>
            <select value={kubeTargetId} onChange={(e) => setKubeTargetId(Number(e.target.value))}>
              <option value={0}>none — plain SQL run</option>
              {kubeTargets.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
            </select>
            <span className="subtle" style={{ fontSize: 11 }}>injects the target&apos;s
              kubeconfig into the worker so storage identity + the 1s device-IOPS
              series are captured (pair with a <code>cluster:</code> section in the spec)</span>
          </div>
          <div className="row">
            <div className="field"><label>Save current spec as template</label><input value={tplName} onChange={(e) => setTplName(e.target.value)} placeholder="template name" /></div>
            <button className="ghost" style={{ alignSelf: "end", marginBottom: 12 }} onClick={saveTemplate} disabled={!tplName}>Save template</button>
          </div>
        </div>

        <div className="card">
          <div className="card-head"><h2>Spec (YAML)</h2>{!autoSync && <span className="chip">raw editing</span>}</div>
          <textarea className="yaml" value={yaml} spellCheck={false} rows={20}
            onChange={(e) => { setYaml(e.target.value); setAutoSync(false); }} />
          {validateOut && <div className={`out ${validateOut.ok ? "ok" : "bad"}`}>{validateOut.msg}</div>}
          <div className="actions" style={{ marginTop: 10 }}>
            <button onClick={validate}>Validate</button>
            <button onClick={dryRun}>Dry-run</button>
            {canRun && <button onClick={() => task("preflight")}>Preflight</button>}
            {canRun && <button onClick={() => task("prepare")}>Prepare data</button>}
            {canRun ? <button className="primary" onClick={start}>Start run</button>
              : <span className="subtle">viewer role: read-only</span>}
          </div>
          {dryOut && <pre className="out mono dry">{dryOut}</pre>}

          {canRun && (
            <details className="prep-opts">
              <summary className="subtle">Prepare options (create / recreate database)</summary>
              <label className="follow"><input type="checkbox" checked={createDb} onChange={(e) => setCreateDb(e.target.checked)} /> Create the database <code>{targetDb || "—"}</code> if it doesn't exist</label>
              <label className="follow"><input type="checkbox" checked={!!recreateScope} onChange={(e) => { setRecreateScope(e.target.checked ? "database" : ""); setConfirmDb(""); }} /> Drop existing data first (DESTRUCTIVE)</label>
              {recreateScope && (
                <div className="recreate-box">
                  <label>What to drop
                    <select value={recreateScope} onChange={(e) => setRecreateScope(e.target.value)}>
                      <option value="database">the whole database ({targetDb})</option>
                      <option value="tables">only the benchmark tables</option>
                    </select>
                  </label>
                  <label>Type <code>{targetDb}</code> to confirm
                    <input value={confirmDb} onChange={(e) => setConfirmDb(e.target.value)} placeholder={targetDb} autoComplete="off" /></label>
                  <p className="subtle" style={{ fontSize: 12 }}>
                    This permanently deletes {recreateScope === "database" ? "the entire database and everything in it" : "the sysbench/tpcc tables"} before reloading. Applies when you click <b>Prepare data</b>.
                  </p>
                </div>
              )}
            </details>
          )}
        </div>
      </div>
    </>
  );
}
