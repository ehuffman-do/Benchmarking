import { NavLink } from "react-router-dom";
import type { Me } from "../types";
import { csrfToken } from "../api";
import { useTheme } from "../lib/theme";

// During the migration some destinations are still server-rendered Jinja pages.
// `external: true` routes do a full-page navigation; the rest are SPA routes.
// As each phase lands, flip `external` off and point at the SPA path.
// All console pages are SPA routes now (basename /ui). The legacy Jinja paths
// redirect here, so the UI is fully self-contained.
const NAV: { to: string; label: string; admin?: boolean; op?: boolean }[] = [
  { to: "/", label: "Runs" },
  { to: "/new", label: "New run" },
  { to: "/tasks", label: "Tasks" },
  { to: "/targets", label: "Targets" },
  { to: "/compare", label: "Compare" },
  { to: "/ops", label: "Cluster Ops" },
  { to: "/ops/runs", label: "Ops runs" },
  { to: "/diagnostics", label: "Diagnostics", op: true },
  { to: "/users", label: "Users", admin: true },
  { to: "/settings", label: "Settings", admin: true },
  { to: "/audit", label: "Audit", admin: true },
];

export function Shell({ me, children }: { me: Me; children: React.ReactNode }) {
  const [theme, toggle] = useTheme();
  const items = NAV.filter((n) =>
    (!n.admin || me.role === "admin") && (!n.op || me.role !== "viewer"));
  return (
    <div className="app">
      <header className="topbar">
        <div className="wordmark">
          pgbench<span className="tick">/</span><span className="dim">harness</span>
        </div>
        <nav className="topnav">
          {items.map((n) => (
            <NavLink key={n.to} to={n.to} end className={({ isActive }) => (isActive ? "active" : "")}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="spacer" />
        <div className="who">
          <span className={`role role-${me.role}`}>{me.role}</span>
          <span className="mono">{me.user}</span>
          <form method="post" action="/logout" style={{ display: "inline" }}>
            <input type="hidden" name="csrf_token" value={csrfToken()} />
            <button className="ghost" type="submit">Log out</button>
          </form>
          <button className="ghost" onClick={toggle} title="Toggle theme" aria-label="Toggle theme">
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>
      </header>
      <main className="page">{children}</main>
      <footer className="foot">
        pgbench-harness {me.version} · self-signed TLS — verify the fingerprint shown at install · times are UTC
      </footer>
    </div>
  );
}
