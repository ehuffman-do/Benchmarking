import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

export interface ChartSeries {
  label: string;
  values: number[];
  stroke: string;
  scale?: string; // y-scale key; series on a different key get a right-hand axis
}

interface Props {
  title: string;
  xs: number[]; // elapsed seconds by default
  series: ChartSeries[];
  height?: number;
  yFormat?: (v: number) => string;
  xFormat?: (v: number) => string; // default: "<v>s"
  xMax?: number; // anchor the x-axis at [0, max(data, xMax)] so a running time
                 // axis is drawn from t=0 even before any data arrives
  spanGaps?: boolean; // connect across null holes — for sparsely-sampled series
                      // (e.g. 5s PG metrics on a per-second axis), NOT throughput
                      // where a real gap must break the line
}

// Axis/grid colours that read on both themes (mid-grey, low-alpha grid).
const AXIS = "#8b97a6";
const GRID = "rgba(139,151,166,0.16)";

// uPlot renders `null` as a clean gap but NaN/Infinity corrupt the auto-range
// math (min/max go NaN and the whole axis collapses). A loadgen restart resets
// the series to offset 0, which can briefly leave NaN holes — map them to null
// so the line breaks cleanly instead of blanking the chart.
const clean = (vals: number[]): (number | null)[] =>
  vals.map((v) => (Number.isFinite(v) ? v : null));

export function LiveChart({ title, xs, series, height = 220, yFormat, xFormat, xMax, spanGaps }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);

  // (Re)create the plot when the *shape* (series identity) changes.
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
    const opts: uPlot.Options = {
      title,
      width: el.clientWidth || 600,
      height,
      cursor: { drag: { x: true, y: false } },
      legend: { live: true },
      // Guard degenerate auto-ranges: when a series is all zeros (e.g. before any
      // data arrives), uPlot would collapse min==max and render a broken axis.
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
          label: s.label,
          stroke: s.stroke,
          width: 1.6,
          scale: s.scale ?? "y",
          points: { show: false },
          spanGaps: !!spanGaps,
        })),
      ],
    };
    const data: uPlot.AlignedData = [xs, ...series.map((s) => clean(s.values))];
    plot.current = new uPlot(opts, data, el);

    const ro = new ResizeObserver(() => {
      if (plot.current) plot.current.setSize({ width: el.clientWidth, height });
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      plot.current?.destroy();
      plot.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title, height, xMax, series.map((s) => `${s.label}:${s.scale ?? "y"}`).join("|")]);

  // Stream data in on every update without rebuilding the plot.
  useEffect(() => {
    if (plot.current) {
      plot.current.setData([xs, ...series.map((s) => clean(s.values))] as uPlot.AlignedData);
    }
  }, [xs, series]);

  return <div className="chart" ref={host} />;
}
