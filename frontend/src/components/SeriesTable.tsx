import { useEffect, useRef } from "react";
import type { Series } from "../lib/sse";
import { fmtInt, fmtNum } from "../lib/format";

function clk(s: number): string {
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

const MAX_ROWS = 300;   // cap DOM to the most recent window; full history is in the CSV export

// Live per-second throughput detail — the exact numbers behind the charts
// (sysbench's --report-interval=1 output, streamed live). Newest at the bottom,
// auto-follows like the console.
export function SeriesTable({ series }: { series: Series }) {
  const pane = useRef<HTMLDivElement>(null);
  const n = series.t.length;
  useEffect(() => {
    if (pane.current) pane.current.scrollTop = pane.current.scrollHeight;
  }, [n]);
  if (n === 0) return null;
  const start = Math.max(0, n - MAX_ROWS);
  const idx = Array.from({ length: n - start }, (_, k) => start + k);
  return (
    <div className="card">
      <div className="card-head">
        <h2>Per-second throughput detail</h2>
        <div className="spacer" />
        <span className="subtle mono">{n} samples{n > MAX_ROWS ? ` · showing last ${MAX_ROWS}` : ""}</span>
      </div>
      <div className="logpane" ref={pane}>
        <table className="ticker">
          <thead>
            <tr><th>t</th><th className="num">TPS</th><th className="num">QPS</th>
              <th className="num">p99 ms</th><th className="num">err/s</th><th className="num">reconn/s</th></tr>
          </thead>
          <tbody>
            {idx.map((i) => (
              <tr key={i} className={series.err[i] > 0 ? "row-warn" : ""}>
                <td className="mono">{clk(series.t[i])}</td>
                <td className="num">{fmtInt(series.tps[i])}</td>
                <td className="num">{fmtInt(series.qps[i])}</td>
                <td className="num">{fmtNum(series.p99[i])}</td>
                <td className="num">{fmtNum(series.err[i])}</td>
                <td className="num">{fmtNum(series.reconn[i])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
