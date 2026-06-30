import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

export interface ChartSeries {
  label: string;
  values: number[];
  stroke: string;
  scale?: string; // y-scale key; series on a different key get a right-hand axis
}

export interface Marker {
  t: number;                       // x position (elapsed seconds)
  label: string;
  kind: "event" | "detected";      // confirmed (solid red) vs auto-detected (dashed amber)
}

interface Props {
  title: string;
  xs: number[];                    // elapsed seconds
  series: ChartSeries[];
  height?: number;
  yFormat?: (v: number) => string;
  xFormat?: (v: number) => string; // default: "<v>s"
  xMax?: number;                   // anchor the x-axis to [0, max(data, xMax)]
  markers?: Marker[];
  baseline?: number | null;        // horizontal reference line (e.g. baseline TPS)
  annotate?: boolean;              // when true, clicking the plot calls onStamp
  onStamp?: (t: number, clientX: number, clientY: number) => void;
}

const AXIS = "#8b97a6";
const GRID = "rgba(139,151,166,0.16)";
const EVENT = "#f85149";          // confirmed event (solid)
const DETECTED = "#e0a93b";       // auto-detected (dashed)
const BASELINE = "#8b97a6";

// uPlot renders null as a clean gap but NaN/Infinity corrupt the auto-range math;
// map non-finite values to null so reset/restart holes break the line cleanly.
const clean = (vals: number[]): (number | null)[] =>
  vals.map((v) => (Number.isFinite(v) ? v : null));

export function AnnotatableChart({
  title, xs, series, height = 240, yFormat, xFormat, xMax,
  markers, baseline, annotate, onStamp,
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  // Live refs so the draw hook / click handler see current values without
  // forcing a full plot rebuild on every marker or mode change.
  const markersRef = useRef<Marker[]>(markers ?? []);
  const baselineRef = useRef<number | null>(baseline ?? null);
  const annotateRef = useRef<boolean>(!!annotate);
  const onStampRef = useRef<Props["onStamp"]>(onStamp);
  markersRef.current = markers ?? [];
  baselineRef.current = baseline ?? null;
  annotateRef.current = !!annotate;
  onStampRef.current = onStamp;

  // (Re)create the plot only when the *shape* (series identity) changes.
  useEffect(() => {
    if (!host.current) return;
    const el = host.current;
    const fmt = (vals: number[]) => (yFormat ? vals.map((v) => yFormat(v)) : vals.map(String));
    const hasRight = series.some((s) => s.scale === "y2");
    const axes: uPlot.Axis[] = [
      {
        stroke: AXIS, grid: { stroke: GRID, width: 1 }, ticks: { stroke: GRID },
        values: (_u, vals) => vals.map((v) => (xFormat ? xFormat(v) : `${v}s`)),
        font: "11px 'IBM Plex Mono', monospace",
      },
      {
        scale: "y", stroke: AXIS, grid: { stroke: GRID, width: 1 }, ticks: { stroke: GRID },
        values: (_u, vals) => fmt(vals), font: "11px 'IBM Plex Mono', monospace", size: 56,
      },
    ];
    if (hasRight) {
      axes.push({
        scale: "y2", side: 1, stroke: AXIS, grid: { show: false }, ticks: { stroke: GRID },
        values: (_u, vals) => fmt(vals), font: "11px 'IBM Plex Mono', monospace", size: 56,
      });
    }

    // Draw the baseline + event markers over the series each frame.
    const drawOverlays = (u: uPlot) => {
      const c = u.ctx;
      const bl = baselineRef.current;
      c.save();
      if (bl != null && Number.isFinite(bl)) {
        const y = Math.round(u.valToPos(bl, "y", true));
        if (y >= u.bbox.top && y <= u.bbox.top + u.bbox.height) {
          c.strokeStyle = BASELINE; c.lineWidth = 1; c.setLineDash([5, 4]);
          c.beginPath(); c.moveTo(u.bbox.left, y); c.lineTo(u.bbox.left + u.bbox.width, y); c.stroke();
        }
      }
      c.setLineDash([]);
      c.font = "10px 'IBM Plex Mono', monospace"; c.textBaseline = "top";
      // Labels only on the tall headline chart (companions show just the lines), and
      // staggered across a few vertical slots so markers near the same time don't
      // overprint each other into an unreadable smear.
      const showLabels = height >= 220;
      let li = 0;
      for (const m of [...markersRef.current].sort((a, b) => a.t - b.t)) {
        const x = Math.round(u.valToPos(m.t, "x", true));
        if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
        const ev = m.kind === "event";
        c.strokeStyle = ev ? EVENT : DETECTED; c.lineWidth = 1;
        c.setLineDash(ev ? [] : [4, 3]);
        c.beginPath(); c.moveTo(x, u.bbox.top); c.lineTo(x, u.bbox.top + u.bbox.height); c.stroke();
        if (showLabels && m.label) {
          c.setLineDash([]); c.fillStyle = ev ? EVENT : DETECTED;
          const ly = u.bbox.top + 2 + (li % 4) * 12;
          const right = x + 4 + c.measureText(m.label).width > u.bbox.left + u.bbox.width;
          c.textAlign = right ? "right" : "left";
          c.fillText(m.label, x + (right ? -4 : 4), ly);
          c.textAlign = "left";
          li++;
        }
      }
      c.restore();
    };

    const opts: uPlot.Options = {
      title,
      width: el.clientWidth || 600,
      height,
      cursor: { drag: { x: true, y: false } },
      legend: { live: true },
      scales: {
        x: xMax
          ? { time: false, range: (_u, _min, max) => [0, Math.max(max || 0, xMax)] }
          : { time: false },
        y: { range: (_u, min, max) => (min === max ? [0, max || 1] : [min, max]) },
        y2: { range: (_u, min, max) => (min === max ? [0, max || 1] : [min, max]) },
      },
      axes,
      series: [
        {},
        ...series.map((s) => ({
          label: s.label, stroke: s.stroke, width: 1.6,
          scale: s.scale ?? "y", points: { show: false },
        })),
      ],
      hooks: { draw: [drawOverlays] },
    };
    const data: uPlot.AlignedData = [xs, ...series.map((s) => clean(s.values))];
    const u = new uPlot(opts, data, el);
    plot.current = u;

    // Click-to-stamp: only active in annotate mode. posToVal maps the click's CSS
    // offset (relative to the plot area) back to an elapsed-seconds value.
    const onClick = (e: MouseEvent) => {
      if (!annotateRef.current || !onStampRef.current) return;
      const t = u.posToVal(e.offsetX, "x");
      if (!Number.isFinite(t) || t < 0) return;
      onStampRef.current(t, e.clientX, e.clientY);
    };
    u.over.addEventListener("click", onClick);

    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      u.over.removeEventListener("click", onClick);
      u.destroy();
      plot.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title, height, xMax, series.map((s) => `${s.label}:${s.scale ?? "y"}`).join("|")]);

  // Stream data in without rebuilding the plot.
  useEffect(() => {
    if (plot.current) {
      plot.current.setData([xs, ...series.map((s) => clean(s.values))] as uPlot.AlignedData);
    }
  }, [xs, series]);

  // Redraw overlays when markers/baseline change (refs already updated above).
  useEffect(() => {
    plot.current?.redraw();
  }, [markers, baseline]);

  // Toggle the crosshair affordance when annotate mode flips.
  useEffect(() => {
    if (plot.current) plot.current.over.style.cursor = annotate ? "crosshair" : "";
  }, [annotate]);

  return <div className="chart" ref={host} />;
}
