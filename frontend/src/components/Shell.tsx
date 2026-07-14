import { useState } from "react";
import { NavLink } from "react-router-dom";
import type { Me } from "../types";
import { csrfToken } from "../api";
import { useTheme } from "../lib/theme";
import { Palette } from "./Palette";

// Grouped sidebar navigation — the enterprise-console IA: observe first,
// then the two harness domains, then administration.
interface NavItem { to: string; label: string; icon: string; admin?: boolean; op?: boolean }
interface NavGroup { title: string; items: NavItem[] }

const GROUPS: NavGroup[] = [
  {
    title: "Observe",
    items: [
      { to: "/", label: "Runs", icon: "▤" },
      { to: "/tasks", label: "Tasks", icon: "☰" },
    ],
  },
  {
    title: "Benchmarking",
    items: [
      { to: "/new", label: "New run", icon: "＋" },
      { to: "/targets", label: "DB targets", icon: "⛁" },
      { to: "/compare", label: "Compare", icon: "⇄" },
      { to: "/diagnostics", label: "Environment", icon: "✓", op: true },
    ],
  },
  {
    title: "Cluster Ops",
    items: [
      { to: "/ops", label: "Clusters", icon: "⬡" },
      { to: "/ops/runs", label: "Ops runs", icon: "◷" },
    ],
  },
  {
    title: "Administration",
    items: [
      { to: "/users", label: "Users", icon: "◉", admin: true },
      { to: "/settings", label: "Settings", icon: "⚙", admin: true },
      { to: "/audit", label: "Audit", icon: "≡", admin: true },
    ],
  },
];

export function Shell({ me, children }: { me: Me; children: React.ReactNode }) {
  const [theme, toggle] = useTheme();
  const [navOpen, setNavOpen] = useState(false);
  const groups = GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((n) =>
      (!n.admin || me.role === "admin") && (!n.op || me.role !== "viewer")),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="app shell-side">
      <aside className={`sidenav ${navOpen ? "open" : ""}`}>
        <div className="wordmark">
          pgbench<span className="tick">/</span><span className="dim">harness</span>
        </div>
        <button
          className="palette-hint"
          onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
        >
          <span>Search…</span><kbd>⌘K</kbd>
        </button>
        <nav className="sidenav-groups" onClick={() => setNavOpen(false)}>
          {groups.map((g) => (
            <div className="nav-group" key={g.title}>
              <div className="nav-title">{g.title}</div>
              {g.items.map((n) => (
                <NavLink key={n.to} to={n.to} end={n.to === "/" || n.to === "/ops"}
                         className={({ isActive }) => (isActive ? "active" : "")}>
                  <span className="ni">{n.icon}</span>{n.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidenav-foot">
          <div className="who">
            <span className={`role role-${me.role}`}>{me.role}</span>
            <span className="mono">{me.user}</span>
          </div>
          <div className="who">
            <button className="ghost" onClick={toggle} title="Toggle theme" aria-label="Toggle theme">
              {theme === "dark" ? "☀ light" : "☾ dark"}
            </button>
            <form method="post" action="/logout" style={{ display: "inline" }}>
              <input type="hidden" name="csrf_token" value={csrfToken()} />
              <button className="ghost" type="submit">Log out</button>
            </form>
          </div>
        </div>
      </aside>

      <div className="shell-main">
        <header className="topbar slim">
          <button className="ghost nav-burger" onClick={() => setNavOpen((v) => !v)}
                  aria-label="Toggle navigation">☰</button>
          <div className="wordmark small">
            pgbench<span className="tick">/</span><span className="dim">harness</span>
          </div>
          <div className="spacer" />
          <span className="subtle" style={{ fontSize: 12 }}>times are UTC</span>
        </header>
        <main className="page">{children}</main>
        <footer className="foot">
          pgbench-harness {me.version} · self-signed TLS — verify the fingerprint shown at install
        </footer>
      </div>

      <Palette me={me} />
    </div>
  );
}
