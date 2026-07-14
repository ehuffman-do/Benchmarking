import { Link } from "react-router-dom";

/** Breadcrumb trail: pass [label, to] pairs; the last entry is the current page. */
export function Crumbs({ trail }: { trail: [string, string?][] }) {
  return (
    <div className="crumbs">
      {trail.map(([label, to], i) => (
        <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {i > 0 && <span className="sep">/</span>}
          {to ? <Link to={to}>{label}</Link> : <span className="here">{label}</span>}
        </span>
      ))}
    </div>
  );
}
