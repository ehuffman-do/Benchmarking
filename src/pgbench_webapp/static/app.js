"use strict";
// pgbench-harness web UI — vanilla JS (CSP-safe: external file, no eval).

const $ = (id) => document.getElementById(id);
const CSRF = window.CSRF || "";

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
    body: JSON.stringify(body || {}),
  });
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { error: text }; }
  if (!r.ok) throw new Error(data.detail || data.error || ("HTTP " + r.status));
  return data;
}

// ── theme ──
(function () {
  const t = $("themeToggle");
  const saved = localStorage.getItem("pgbench_theme");
  if (saved) document.documentElement.dataset.theme = saved;
  if (t) t.addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = cur;
    localStorage.setItem("pgbench_theme", cur);
  });
})();

// ── new run page ──
function scaffoldYAML() {
  const tags = ($("g_tags").value || "").split(",").map(s => s.trim()).filter(Boolean);
  const mode = $("g_mode").value;
  const run = {
    label: $("g_label").value || "run", edition: $("g_edition").value,
    tshirt_size: $("g_size").value || "8c32g",
  };
  if (tags.length) run.tags = tags;
  if ($("g_ticket").value) run.ticket = $("g_ticket").value;
  const lines = [];
  const emit = (o, ind) => { for (const k in o) {
    const v = o[k];
    if (Array.isArray(v)) lines.push(`${ind}${k}: [${v.join(", ")}]`);
    else if (v && typeof v === "object") { lines.push(`${ind}${k}:`); emit(v, ind + "  "); }
    else lines.push(`${ind}${k}: ${v}`);
  } };
  const doc = {
    run,
    target: { host: "private-xyz.db.ondigitalocean.com", port: 5432, database: "sbtest",
      user: "doadmin", password_env: "PGB_TARGET_PASSWORD", sslmode: "require" },
    workload: { type: "tpcc", tpcc_path: "/opt/sysbench-tpcc", tables: 10, scale: 30 },
  };
  if (mode === "soak") doc.soak = { threads: 64, duration_s: 3600, tolerate_errors: true };
  else doc.sweep = { threads: [1, 4, 16, 64], duration_s: 300, warmup_s: 60, cooldown_s: 30, repetitions: 1 };
  emit(doc, "");
  $("spec").value = lines.join("\n") + "\n";
}

function initNewRun() {
  if (!$("spec")) return;
  if ($("preset")) $("preset").addEventListener("change", (e) => {
    const p = (window.PRESETS || {})[e.target.value];
    if (p) $("spec").value = p;
  });
  if ($("scaffold")) $("scaffold").addEventListener("click", (e) => { e.preventDefault(); scaffoldYAML(); });
  if ($("btn_validate")) $("btn_validate").addEventListener("click", async (e) => {
    e.preventDefault();
    const out = $("validate_out");
    try {
      const d = await postJSON("/api/validate", { spec_yaml: $("spec").value });
      if (d.ok) { out.className = "out ok"; out.textContent = `valid — ${d.mode} run “${d.label}” (${d.workload})`; }
      else { out.className = "out bad"; out.textContent = (d.error || "invalid") + (d.hint ? " — " + d.hint : ""); }
    } catch (err) { out.className = "out bad"; out.textContent = err.message; }
  });
  if ($("btn_dryrun")) $("btn_dryrun").addEventListener("click", async (e) => {
    e.preventDefault();
    const out = $("dryrun_out");
    try {
      const d = await postJSON("/api/dry-run", { spec_yaml: $("spec").value });
      const mins = Math.round(d.budget_s / 60);
      out.textContent = `# ${d.mode} — planned wall-clock budget: ${d.budget_s}s (~${mins} min)\n` + d.commands.join("\n");
    } catch (err) { out.textContent = "error: " + err.message; }
  });
  if ($("btn_start")) $("btn_start").addEventListener("click", async (e) => {
    e.preventDefault();
    const out = $("validate_out");
    try {
      await postJSON("/api/runs", {
        spec_yaml: $("spec").value, password: $("g_password").value,
        scheduled_utc: ($("g_schedule").value || "").trim() || null, csrf_token: CSRF });
      window.location = "/";
    } catch (err) { out.className = "out bad"; out.textContent = "could not start: " + err.message; }
  });
  if ($("save_tpl")) $("save_tpl").addEventListener("click", async (e) => {
    e.preventDefault();
    const out = $("tpl_out");
    try {
      const d = await postJSON("/api/templates",
        { name: $("tpl_name").value, spec_yaml: $("spec").value, csrf_token: CSRF });
      out.textContent = `saved template “${d.name}” v${d.version}`;
    } catch (err) { out.textContent = "error: " + err.message; }
  });
}

function initSettings() {
  const b = $("notify_test"); if (!b) return;
  b.addEventListener("click", async () => {
    const out = $("notify_test_out");
    try {
      const d = await postJSON("/api/notify/test", {});
      out.textContent = d.sent.length ? "sent via: " + d.sent.join(", ")
                                      : "nothing configured (no channels)";
    } catch (e) { out.textContent = e.message; }
  });
}

// ── cancel buttons (history) ──
function initCancel() {
  document.querySelectorAll(".cancel-job").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("Cancel job " + b.dataset.job + "?")) return;
      try { await postJSON(`/api/jobs/${b.dataset.job}/cancel`, {}); location.reload(); }
      catch (e) { alert(e.message); }
    }));
}

// ── detail page: mark / resume / live SSE ──
function initDetail() {
  if (!window.RUN_ID) return;
  document.querySelectorAll(".mark-btn").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await postJSON(`/api/runs/${b.dataset.run}/mark`,
          { type: b.dataset.type, label: b.dataset.type, csrf_token: CSRF });
        flash(`marked ${b.dataset.type}`);
      } catch (e) { alert(e.message); }
    }));
  const rb = $("resume-btn");
  if (rb) rb.addEventListener("click", async () => {
    try { await postJSON(`/api/runs/${rb.dataset.run}/resume`, {}); location = "/"; }
    catch (e) { alert(e.message); }
  });
  startStream(window.RUN_ID);
}

function flash(msg) {
  const s = $("chart_status"); if (s) s.textContent = msg;
}

// minimal canvas line chart for live TPS
function drawChart(rows) {
  const c = $("chart"); if (!c || !rows.length) return;
  const ctx = c.getContext("2d"); const W = c.width, H = c.height, pad = 30;
  ctx.clearRect(0, 0, W, H);
  const xs = rows.map(r => r.t), ys = rows.map(r => r.tps);
  const xmax = Math.max(...xs, 1), ymax = Math.max(...ys, 1);
  ctx.strokeStyle = "#9fb0c0"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, H - pad); ctx.lineTo(W - 5, H - pad);
  ctx.moveTo(pad, 5); ctx.lineTo(pad, H - pad); ctx.stroke();
  ctx.strokeStyle = "#0061eb"; ctx.lineWidth = 2; ctx.beginPath();
  rows.forEach((r, i) => {
    const x = pad + (r.t / xmax) * (W - pad - 8);
    const y = (H - pad) - (r.tps / ymax) * (H - pad - 8);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "#5b6573"; ctx.font = "11px sans-serif";
  ctx.fillText(`${Math.round(ymax)} TPS`, pad + 2, 12);
  ctx.fillText(`${xmax}s`, W - 30, H - pad + 14);
}

function parseSamples(payload) {
  const cols = payload.header.split(",");
  const ti = cols.indexOf("t") >= 0 ? cols.indexOf("t") : cols.indexOf("t_offset");
  const qi = cols.indexOf("tps");
  if (ti < 0 || qi < 0) return [];
  return payload.rows.map(line => {
    const f = line.split(",");
    return { t: parseFloat(f[ti]), tps: parseFloat(f[qi]) };
  }).filter(r => !isNaN(r.t) && !isNaN(r.tps));
}

function startStream(runId) {
  const log = $("log");
  const es = new EventSource(`/runs/${runId}/stream`);
  es.addEventListener("log", (e) => {
    if (log) { log.textContent += JSON.parse(e.data); log.scrollTop = log.scrollHeight; }
  });
  es.addEventListener("samples", (e) => {
    const rows = parseSamples(JSON.parse(e.data));
    drawChart(rows); flash(`${rows.length} samples`);
  });
  es.addEventListener("done", (e) => {
    flash("finished: " + JSON.parse(e.data).status); es.close();
  });
  es.onerror = () => flash("stream reconnecting…");
}

// ── compare page ──
function initCompare() {
  const f = $("cmpform"); if (!f) return;
  $("cmpgo").addEventListener("click", (e) => {
    e.preventDefault();
    const ids = [...document.querySelectorAll('input[name=run]:checked')].map(c => c.value);
    if (ids.length < 2) { alert("select at least two runs"); return; }
    window.open(`/compare/view?runs=${ids.join(",")}`, "_blank");
  });
}

initNewRun(); initCancel(); initDetail(); initCompare(); initSettings();
