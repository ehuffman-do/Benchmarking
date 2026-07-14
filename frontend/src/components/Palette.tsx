import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { KubeTarget, Me, OpsRun, Run } from "../types";

interface Item {
  label: string;
  hint: string;
  to: string;
  group: string;
}

const PAGES: Item[] = [
  { label: "Runs", hint: "benchmark + cluster ops feed", to: "/", group: "Pages" },
  { label: "New run", hint: "start a benchmark", to: "/new", group: "Pages" },
  { label: "Tasks", hint: "preflight / prepare / doctor", to: "/tasks", group: "Pages" },
  { label: "Targets", hint: "database targets", to: "/targets", group: "Pages" },
  { label: "Compare", hint: "compare benchmark runs", to: "/compare", group: "Pages" },
  { label: "Clusters", hint: "Cluster Ops — kube targets", to: "/ops", group: "Pages" },
  { label: "Ops runs", hint: "cluster operation runs", to: "/ops/runs", group: "Pages" },
  { label: "Diagnostics (environment)", hint: "harness environment checks", to: "/diagnostics", group: "Pages" },
  { label: "Users", hint: "manage console users", to: "/users", group: "Pages" },
  { label: "Settings", hint: "console settings", to: "/settings", group: "Pages" },
  { label: "Audit", hint: "who did what, when", to: "/audit", group: "Pages" },
];

export function Palette({ me }: { me: Me }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [dynamic, setDynamic] = useState<Item[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const nav = useNavigate();

  const openPalette = useCallback(() => {
    setOpen(true);
    setQ("");
    setSel(0);
    // Index the things people jump to: clusters, then recent runs of both kinds.
    Promise.all([
      api.get<KubeTarget[]>("/api/kube-targets").catch(() => []),
      api.get<Run[]>("/api/runs").catch(() => []),
      api.get<OpsRun[]>("/api/ops/runs").catch(() => []),
    ]).then(([kts, runs, ops]) => {
      const items: Item[] = [];
      for (const t of kts) {
        items.push({ label: t.name, hint: `${t.cr_kind}/${t.cr_name || "?"} · ns ${t.namespace}`,
                     to: `/ops/targets/${t.id}`, group: "Clusters" });
        items.push({ label: `${t.name} — parameter map`, hint: "searchable pg_settings + sidecar options",
                     to: `/ops/targets/${t.id}/params`, group: "Clusters" });
        items.push({ label: `${t.name} — diagnostics`, hint: "click-to-run checks",
                     to: `/ops/targets/${t.id}/diag`, group: "Clusters" });
      }
      for (const r of runs.slice(0, 12)) {
        items.push({ label: r.run_id, hint: [r.label, r.status].filter(Boolean).join(" · "),
                     to: `/runs/${r.run_id}`, group: "Recent benchmark runs" });
      }
      for (const r of ops.slice(0, 12)) {
        items.push({ label: r.op_run_id, hint: [r.kind, r.status].filter(Boolean).join(" · "),
                     to: `/ops/runs/${r.op_run_id}`, group: "Recent ops runs" });
      }
      setDynamic(items);
    });
    setTimeout(() => inputRef.current?.focus(), 30);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (open) setOpen(false); else openPalette();
      } else if (e.key === "Escape" && open) {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, openPalette]);

  const pages = useMemo(() =>
    PAGES.filter((p) =>
      (p.to !== "/users" && p.to !== "/settings" && p.to !== "/audit") || me.role === "admin"),
    [me.role]);

  const results = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const all = [...pages, ...dynamic];
    if (!needle) return all.slice(0, 14);
    return all
      .filter((i) => i.label.toLowerCase().includes(needle) ||
                     i.hint.toLowerCase().includes(needle))
      .slice(0, 14);
  }, [q, pages, dynamic]);

  useEffect(() => { setSel(0); }, [q]);

  if (!open) return null;

  function go(item: Item) {
    setOpen(false);
    nav(item.to);
  }

  let lastGroup = "";
  return (
    <div className="palette-overlay" onClick={() => setOpen(false)}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          placeholder="Jump to a page, cluster, or run…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, results.length - 1)); }
            if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
            if (e.key === "Enter" && results[sel]) go(results[sel]);
          }}
        />
        <div className="palette-list">
          {results.length === 0 && <div className="palette-empty">Nothing matches.</div>}
          {results.map((item, i) => {
            const header = item.group !== lastGroup ? item.group : null;
            lastGroup = item.group;
            return (
              <div key={`${item.to}-${i}`}>
                {header && <div className="palette-group">{header}</div>}
                <button
                  className={`palette-item ${i === sel ? "sel" : ""}`}
                  onMouseEnter={() => setSel(i)}
                  onClick={() => go(item)}
                >
                  <span className="pl">{item.label}</span>
                  <span className="ph">{item.hint}</span>
                </button>
              </div>
            );
          })}
        </div>
        <div className="palette-foot">
          <kbd>↑↓</kbd> navigate&nbsp;&nbsp;<kbd>↵</kbd> open&nbsp;&nbsp;<kbd>esc</kbd> close
        </div>
      </div>
    </div>
  );
}
