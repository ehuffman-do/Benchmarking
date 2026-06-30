// Small, dependency-free formatters. All times are UTC (the harness clock).

export function fmtInt(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

export function fmtNum(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Compact magnitude for chart axes so big values (IOPS, tuples) fit the gutter:
 *  1726 -> "1.7k", 126879 -> "127k", 1268790 -> "1.27M". Legends still show full. */
export function fmtCompact(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a < 1000) return String(Math.round(n));
  if (a < 1e6) return `${(n / 1e3).toLocaleString("en-US", { maximumFractionDigits: a < 1e4 ? 1 : 0 })}k`;
  if (a < 1e9) return `${(n / 1e6).toLocaleString("en-US", { maximumFractionDigits: a < 1e7 ? 2 : 1 })}M`;
  return `${(n / 1e9).toLocaleString("en-US", { maximumFractionDigits: 1 })}B`;
}

/** "2026-06-27T14:30:05Z" -> "Jun 27, 14:30 UTC"; passes through if unparseable. */
export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const mon = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
  const day = d.getUTCDate();
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${mon} ${day}, ${hh}:${mm} UTC`;
}

/** Human duration between two ISO timestamps (e.g. "20m 34s", "1h 05m"). */
export function durBetween(start?: string | null, end?: string | null): string {
  if (!start || !end) return "—";
  const s = (new Date(end).getTime() - new Date(start).getTime()) / 1000;
  if (Number.isNaN(s) || s < 0) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

export function relAge(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
