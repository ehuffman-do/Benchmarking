import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { OpsCompareRow } from "../types";

const fmtS = (ms: number | null) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)}s`);

export function OpsCompare() {
  const [params] = useSearchParams();
  const runs = params.get("runs") ?? "";
  const [rows, setRows] = useState<OpsCompareRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<{ runs: OpsCompareRow[] }>(`/api/ops/compare?runs=${encodeURIComponent(runs)}`)
      .then((r) => setRows(r.runs)).catch((e) => setErr(e.message));
  }, [runs]);

  return (
    <>
      <div className="toolbar">
        <h1>Scenario comparison</h1>
        <div className="spacer" />
        <Link className="btn" to="/ops/runs">← ops runs</Link>
      </div>
      <p className="subtle" style={{ marginTop: -8 }}>
        The Case A/B/C table: trigger, client downtime, election vs restart-in-place (decided by the
        Patroni leader name — never the probe&apos;s answering IP), timeline change, new primary, and the
        pgBouncer backoff tail.
      </p>
      {err && <div className="banner-err">{err}</div>}
      <div className="card">
        <table>
          <thead><tr>
            <th>Run</th><th>Trigger</th><th className="num">Downtime</th><th className="num">Detection</th>
            <th>Election?</th><th>TL</th><th>New primary</th><th className="num">Backoff tail</th>
            <th className="num">Full HA</th><th>Status</th>
          </tr></thead>
          <tbody>
            {rows === null ? (
              <tr><td colSpan={10} className="empty mono">loading…</td></tr>
            ) : rows.map((r) => (
              <tr key={r.op_run_id}>
                <td><Link className="mono" to={`/ops/runs/${r.op_run_id}`}>{r.op_run_id}</Link>
                  <div className="subtle mono" style={{ fontSize: 11 }}>{r.target} · {r.created_utc}</div></td>
                <td className="mono">{r.case || "—"}</td>
                <td className="num mono">{fmtS(r.downtime_ms)}</td>
                <td className="num mono">{fmtS(r.detection_ms)}</td>
                <td>{r.flip == null ? "—" : r.flip
                  ? <span className="badge failed">YES — election</span>
                  : <span className="badge ok">NO — restart in place</span>}</td>
                <td className="mono">{r.tl_change || "—"}</td>
                <td className="mono">{r.new_primary}</td>
                <td className="num mono">{fmtS(r.backoff_tail_ms)}</td>
                <td className="num mono">{r.full_ha_recovery_s != null ? `${r.full_ha_recovery_s}s` : "—"}</td>
                <td><span className={`badge ${r.status}`}>{r.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
