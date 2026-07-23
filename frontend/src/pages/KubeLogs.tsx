import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import type { Job, KubeTarget, Me, Topology } from "../types";
import { openLogsStream, LogSource } from "../lib/sse";
import { Crumbs } from "../components/Crumbs";

// ── line classification ────────────────────────────────────────────────
// The database container interleaves Postgres and Patroni lines in one
// stream; severity spellings differ per component. Everything is parsed
// best-effort and normalized to debug/info/warning/error/fatal/unknown.

export type Sev = "debug" | "info" | "warning" | "error" | "fatal" | "unknown";

const SEV_NORM: Record<string, Sev> = {
  DEBUG: "debug", DEBUG1: "debug", DEBUG2: "debug", DEBUG3: "debug",
  INFO: "info", LOG: "info", NOTICE: "info", DETAIL: "info", HINT: "info",
  STATEMENT: "info", CONTEXT: "info",
  WARNING: "warning", WARN: "warning",
  ERROR: "error",
  FATAL: "fatal", PANIC: "fatal",
};

const PATRONI_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\s+([A-Z]+):/;
const PG_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.,]\d+ \S+ \[\d+\]\s+([A-Z]+\d?):/;
const BACKREST_RE = /^(?:\d+-\d+ )?P\d{2,3}\s+([A-Z]+):/;
const GENERIC_RE = /\b(DEBUG\d?|INFO|LOG|NOTICE|WARNING|WARN|ERROR|FATAL|PANIC|DETAIL|HINT|STATEMENT)\b:?/;

export interface ParsedLine {
  ts: string;          // kubectl --timestamps RFC3339 prefix ("" when absent)
  body: string;
  sev: Sev;
  comp: "postgres" | "patroni" | "pgbackrest" | "pgbouncer" | "operator" | "other";
  file: string;
}

export function classifyLine(raw: string, category: string, file: string): ParsedLine {
  // kubectl --timestamps: "2026-07-23T02:41:08.373145931Z <body>"
  let ts = "", body = raw;
  const m = raw.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s(.*)$/s);
  if (m) { ts = m[1]; body = m[2]; }
  let sev: Sev = "unknown";
  let comp: ParsedLine["comp"] = "other";
  let mm: RegExpMatchArray | null;
  if (category === "operator" && body.trimStart().startsWith("{")) {
    comp = "operator";
    try { sev = SEV_NORM[String(JSON.parse(body).level ?? "").toUpperCase()] ?? "unknown"; }
    catch { sev = "unknown"; }
  } else if ((mm = body.match(PATRONI_RE))) {
    comp = "patroni"; sev = SEV_NORM[mm[1]] ?? "unknown";
  } else if ((mm = body.match(PG_RE))) {
    comp = "postgres"; sev = SEV_NORM[mm[1]] ?? "unknown";
  } else if ((mm = body.match(BACKREST_RE))) {
    comp = "pgbackrest"; sev = SEV_NORM[mm[1]] ?? "unknown";
  } else if ((mm = body.match(GENERIC_RE))) {
    sev = SEV_NORM[mm[1]] ?? "unknown";
    comp = category === "pgbouncer" ? "pgbouncer"
      : category === "pgbackrest" ? "pgbackrest"
      : category === "postgres" ? "postgres" : "other";
  } else if (category === "pgbouncer") {
    comp = "pgbouncer";
  } else if (category === "pgbackrest") {
    comp = "pgbackrest";
  }
  return { ts, body, sev, comp, file };
}

const BUFFER_CAP = 8000;

const CATEGORY_LABEL: Record<string, string> = {
  postgres: "PostgreSQL / Patroni", pgbackrest: "pgBackRest",
  pgbouncer: "pgBouncer", operator: "Operator", backup_jobs: "Backup jobs",
  sidecars: "Other sidecars",
};

const SEV_COLOR: Record<Sev, string> = {
  debug: "var(--muted)", info: "var(--ink-dim)", warning: "var(--warn)",
  error: "var(--bad)", fatal: "var(--bad)", unknown: "var(--muted)",
};

export function KubeLogs({ me }: { me: Me }) {
  const { targetId } = useParams();
  const [kt, setKt] = useState<KubeTarget | null>(null);
  const [topo, setTopo] = useState<Topology | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [tail, setTail] = useState("1000");
  const [job, setJob] = useState<Job | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<string>("idle");
  const [lines, setLines] = useState<ParsedLine[]>([]);
  const [sevFilter, setSevFilter] = useState<Set<Sev>>(
    new Set(["debug", "info", "warning", "error", "fatal", "unknown"]));
  const [compFilter, setCompFilter] = useState<Set<string>>(new Set());
  const [text, setText] = useState("");
  const [follow, setFollow] = useState(true);
  const [sources, setSources] = useState<LogSource[]>([]);
  const esRef = useRef<EventSource | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const canOp = me.role !== "viewer";

  // available sources from topology (pods + containers)
  const available = useMemo(() => {
    const out: { pod: string; container: string; category: string }[] = [];
    const pods = topo?.pods;
    if (!pods) return out;
    for (const p of pods.instances ?? []) {
      for (const c of p.containers ?? []) {
        out.push({ pod: p.name, container: c,
                   category: c === "database" ? "postgres"
                     : c.includes("pgbackrest") ? "pgbackrest" : "sidecars" });
      }
    }
    for (const p of pods.pgbouncer ?? []) {
      for (const c of p.containers ?? []) {
        out.push({ pod: p.name, container: c,
                   category: c.includes("pgbouncer") ? "pgbouncer" : "sidecars" });
      }
    }
    for (const p of pods.backup_jobs ?? []) {
      for (const c of p.containers ?? []) {
        out.push({ pod: p.name, container: c, category: "backup_jobs" });
      }
    }
    for (const p of pods.other ?? []) {
      if (!p.name.includes("operator")) continue;
      for (const c of p.containers ?? []) {
        out.push({ pod: p.name, container: c, category: "operator" });
      }
    }
    return out;
  }, [topo]);

  const grouped = useMemo(() => {
    const g: Record<string, typeof available> = {};
    for (const s of available) (g[s.category] ??= []).push(s);
    return g;
  }, [available]);

  const key = (s: { pod: string; container: string }) => `${s.pod}/${s.container}`;
  const catByFile = useMemo(() => {
    const m: Record<string, string> = {};
    for (const s of sources) m[s.file] = s.category;
    return m;
  }, [sources]);

  const load = useCallback(() => {
    api.get<KubeTarget>(`/api/kube-targets/${targetId}`).then(setKt).catch((e) => setErr(e.message));
    api.get<{ topology: Topology | null }>(`/api/kube-targets/${targetId}/topology`)
      .then((r) => {
        setTopo(r.topology);
        // sensible default: every database container pre-selected
        setSelected((cur) => {
          if (cur.size) return cur;
          const next = new Set<string>();
          for (const p of r.topology?.pods?.instances ?? []) {
            if ((p.containers ?? []).includes("database")) next.add(`${p.name}/database`);
          }
          return next;
        });
      })
      .catch(() => undefined);
    // adopt an already-running logs job for this target
    api.get<Job[]>("/api/jobs").then((jobs) => {
      const active = jobs.find((j) =>
        j.kind === "ops_logs" && ["queued", "running"].includes(j.state) &&
        (j as unknown as { kube_target_id: number | null }).kube_target_id === Number(targetId));
      setJob(active ?? null);
      if (active?.run_id) setRunId(active.run_id);
    }).catch(() => undefined);
  }, [targetId]);
  useEffect(load, [load]);

  // attach the SSE stream whenever we know the op run id
  useEffect(() => {
    if (!runId) return;
    esRef.current?.close();
    setLines([]);
    setStreamState("connecting");
    const es = openLogsStream(runId, {
      onHello: (h) => {
        setStreamState("streaming");
        if (h.sources) setSources(h.sources);
      },
      onSources: (s) => setSources(s),
      onLines: (b) => {
        setLines((prev) => {
          const add = b.lines.map((raw) =>
            classifyLine(raw, catByFile[b.file] ?? "", b.file));
          const next = prev.concat(add);
          return next.length > BUFFER_CAP ? next.slice(next.length - BUFFER_CAP) : next;
        });
      },
      onDone: (d) => setStreamState(d.status || "done"),
      onError: () => setStreamState("reconnecting…"),
    });
    esRef.current = es;
    return () => es.close();
    // catByFile changes when sources land — intentionally NOT a dep: the
    // classifier falls back gracefully and re-subscribing would drop buffer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    if (follow && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [lines, follow]);

  async function start() {
    setErr(null);
    try {
      const srcs = available.filter((s) => selected.has(key(s)))
        .map(({ pod, container }) => ({ pod, container }));
      const params: Record<string, unknown> = { sources: srcs };
      if (tail.endsWith("m") || tail.endsWith("h")) params.since = tail;
      else params.tail = Number(tail);
      const r = await api.post<{ job_id: number }>(`/api/kube-targets/${targetId}/logs`, { params });
      // poll for the run id the job produces
      const poll = setInterval(async () => {
        try {
          const jobs = await api.get<Job[]>("/api/jobs");
          const j = jobs.find((x) => x.id === r.job_id);
          if (j) setJob(j);
          if (j?.run_id) { setRunId(j.run_id); clearInterval(poll); }
          if (j && !["queued", "running"].includes(j.state)) clearInterval(poll);
        } catch { clearInterval(poll); }
      }, 800);
    } catch (ex) { setErr((ex as Error).message); }
  }

  async function stop() {
    if (!job) return;
    try { await api.post(`/api/jobs/${job.id}/stop`, {}); } catch { /* best effort */ }
    setTimeout(load, 800);
  }

  function download() {
    const blob = new Blob([visible.map((l) => `${l.ts} ${l.body}`).join("\n")],
                          { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${kt?.name ?? "cluster"}-logs-slice.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  const visible = useMemo(() => {
    const t = text.trim().toLowerCase();
    let rx: RegExp | null = null;
    if (t.startsWith("/") && t.endsWith("/") && t.length > 2) {
      try { rx = new RegExp(t.slice(1, -1), "i"); } catch { rx = null; }
    }
    return lines.filter((l) => {
      if (!sevFilter.has(l.sev)) return false;
      if (compFilter.size && !compFilter.has(l.comp)) return false;
      if (rx) return rx.test(l.body);
      if (t) return l.body.toLowerCase().includes(t) || l.file.toLowerCase().includes(t);
      return true;
    });
  }, [lines, sevFilter, compFilter, text]);

  const sevCounts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const l of lines) c[l.sev] = (c[l.sev] ?? 0) + 1;
    return c;
  }, [lines]);

  const active = job && ["queued", "running"].includes(job.state);
  if (!kt) return <p className="subtle mono">{err ?? "loading…"}</p>;

  return (
    <>
      <Crumbs trail={[["Clusters", "/ops"], [kt.name, `/ops/targets/${targetId}`], ["Logs"]]} />
      <div className="toolbar">
        <h1>Logs — {kt.name}</h1>
        <div className="spacer" />
        <span className={`badge ${active ? "running" : "ok"}`}>
          {active ? "following" : streamState === "idle" ? "not started" : streamState}</span>
      </div>
      {err && <div className="banner-err">{err}</div>}

      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-head"><h2>Sources</h2>
          <span className="subtle" style={{ fontSize: 12 }}>
            grouped by component — the follower op streams each selected container</span>
          <div className="spacer" />
          <label className="subtle" style={{ fontSize: 12 }}>history&nbsp;
            <select value={tail} onChange={(e) => setTail(e.target.value)}>
              <option value="1000">last 1k lines</option>
              <option value="5000">last 5k lines</option>
              <option value="10000">last 10k lines</option>
              <option value="15m">last 15 min</option>
              <option value="1h">last 1 hour</option>
              <option value="6h">last 6 hours</option>
            </select></label>
          {canOp && (active
            ? <button className="danger" onClick={stop}>Stop following</button>
            : <button className="primary" disabled={selected.size === 0} onClick={start}>
                Start following ({selected.size})</button>)}
        </div>
        {available.length === 0 ? (
          <p className="empty">No topology captured yet — run discovery on the cluster page
            so the pod/container list is known.</p>
        ) : (
          <div className="log-sources">
            {Object.entries(grouped).map(([cat, srcs]) => (
              <div key={cat} className="log-source-group">
                <div className="log-source-title">{CATEGORY_LABEL[cat] ?? cat}</div>
                {srcs.map((s) => (
                  <label key={key(s)} className="log-source">
                    <input type="checkbox" checked={selected.has(key(s))}
                           disabled={!!active}
                           onChange={() => setSelected((cur) => {
                             const next = new Set(cur);
                             if (next.has(key(s))) next.delete(key(s)); else next.add(key(s));
                             return next;
                           })} />
                    <span className="mono" title={key(s)}>
                      …{s.pod.slice(-14)}/{s.container}</span>
                  </label>
                ))}
              </div>
            ))}
          </div>
        )}
        {active && <p className="subtle" style={{ margin: "8px 0 0", fontSize: 12 }}>
          To change the selection, stop and start again — the follower op is replaced.</p>}
      </div>

      <div className="card">
        <div className="card-head" style={{ flexWrap: "wrap" }}>
          <h2>Stream</h2>
          {(["fatal", "error", "warning", "info", "debug", "unknown"] as Sev[]).map((s) => (
            <button key={s}
                    className={`chip ${sevFilter.has(s) ? "chip-on" : ""}`}
                    style={{ color: SEV_COLOR[s] }}
                    onClick={() => setSevFilter((cur) => {
                      const next = new Set(cur);
                      if (next.has(s)) next.delete(s); else next.add(s);
                      return next;
                    })}>
              {s}{sevCounts[s] ? ` · ${sevCounts[s]}` : ""}</button>
          ))}
          <span className="subtle">|</span>
          {["postgres", "patroni", "pgbackrest", "pgbouncer", "operator", "other"].map((c) => (
            <button key={c}
                    className={`chip ${!compFilter.size || compFilter.has(c) ? "chip-on" : ""}`}
                    onClick={() => setCompFilter((cur) => {
                      const next = new Set(cur);
                      if (next.has(c)) next.delete(c); else next.add(c);
                      return next;
                    })}>{c}</button>
          ))}
          <div className="spacer" />
          <input placeholder="filter text or /regex/" value={text}
                 onChange={(e) => setText(e.target.value)} style={{ width: 200 }} />
          <label className="subtle" style={{ fontSize: 12 }}>
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
            &nbsp;follow</label>
          <button className="btn-sm" onClick={() => setLines([])}>clear</button>
          <button className="btn-sm" onClick={download}>download slice</button>
        </div>
        <div ref={boxRef} className="log-box mono">
          {visible.length === 0 ? (
            <div className="subtle" style={{ padding: 12 }}>
              {lines.length ? "nothing matches the filters"
                : active ? "waiting for lines…"
                : "select sources and start following"}
            </div>
          ) : visible.slice(-3000).map((l, i) => (
            <div key={i} className="log-line">
              <span className="log-ts">{l.ts.slice(11, 23)}</span>
              <span className="log-src" title={l.file}>
                {l.file.replace(/^logs_/, "").replace(/\.log$/, "").slice(-24)}</span>
              <span style={{ color: SEV_COLOR[l.sev] }}>{l.body}</span>
            </div>
          ))}
        </div>
        <p className="subtle" style={{ margin: "6px 0 0", fontSize: 11 }}>
          Ring buffer keeps the last {BUFFER_CAP.toLocaleString()} lines (showing up to
          3,000); timestamps are UTC from the kubelet, aligned with the ops event feed.
          The full capture lives in the op run&apos;s raw files.
          {runId && <> · <a href={`/ui/ops/runs/${runId}`}>open the op run</a></>}
        </p>
      </div>
    </>
  );
}
