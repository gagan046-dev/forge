import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

interface Props {
  /** Unix seconds, ascending. */
  xs: number[];
  ys: number[];
  /** Series name shown in the hover readout. */
  label: string;
  format: (v: number) => string;
  /** Bucket size in seconds; decides axis and readout date formats. */
  bucketSeconds: number;
  kind: "bars" | "line";
  height?: number;
}

function cssVar(el: Element, name: string): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

function formatBucket(t: number, bucketSeconds: number): string {
  const d = new Date(t * 1000);
  return bucketSeconds < 86_400
    ? d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

/**
 * One measure per chart (no dual axes). uPlot is a ~45 KB canvas renderer —
 * the one Grafana uses — so it stays fast and small. Colours come from the
 * CSS theme variables and are re-read when the OS colour scheme flips.
 */
export function TimeSeriesChart({ xs, ys, label, format, bucketSeconds, kind, height = 168 }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{ left: number; top: number; t: number; v: number; flip: boolean } | null>(null);
  const [scheme, setScheme] = useState(() => window.matchMedia("(prefers-color-scheme: dark)").matches);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setScheme(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const accent = cssVar(host, "--accent");
    const muted = cssVar(host, "--text-muted");
    const grid = cssVar(host, "--grid");
    const font = `11px ${cssVar(host, "--font-mono")}`;
    const axis = { stroke: muted, font, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1, size: 4 } };

    const opts: uPlot.Options = {
      width: Math.max(200, host.clientWidth),
      height,
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 7 } },
      scales: { x: { time: true }, y: { range: (_u, _min, max) => [0, max > 0 ? max * 1.1 : 1] } },
      axes: [
        { ...axis, space: 70 },
        { ...axis, size: 52, values: (_u, splits) => splits.map((v) => format(v)) },
      ],
      series: [
        {},
        kind === "bars"
          ? {
              label,
              stroke: accent,
              fill: accent,
              width: 0,
              paths: uPlot.paths.bars!({ size: [0.72, 28], gap: 2 }),
              points: { show: false },
            }
          : { label, stroke: accent, width: 2, points: { show: xs.length < 2 } },
      ],
      hooks: {
        setCursor: [
          (u) => {
            const idx = u.cursor.idx;
            const left = u.cursor.left ?? -1;
            if (idx === null || idx === undefined || left < 0) {
              setHover(null);
              return;
            }
            const v = u.data[1]?.[idx];
            const t = u.data[0]?.[idx];
            if (v === null || v === undefined || t === undefined) {
              setHover(null);
              return;
            }
            // Pin the readout to the top of the plot so it never covers the axis labels, and
            // flip it to the cursor's left in the right half so it stays inside the panel.
            const x = left + u.bbox.left / devicePixelRatio;
            setHover({ left: x, top: u.bbox.top / devicePixelRatio + 18, t, v, flip: x > u.width / 2 });
          },
        ],
      },
    };

    const chart = new uPlot(opts, [xs, ys], host);
    const resize = new ResizeObserver(() => chart.setSize({ width: Math.max(200, host.clientWidth), height }));
    resize.observe(host);
    return () => {
      resize.disconnect();
      chart.destroy();
    };
  }, [xs, ys, label, format, kind, height, scheme]);

  return (
    <div className="chart" onMouseLeave={() => setHover(null)}>
      <div ref={hostRef} className="chart-canvas" />
      {hover && (
        <div className={`chart-tip${hover.flip ? " chart-tip-left" : ""}`} style={{ left: hover.left, top: hover.top }}>
          <div className="chart-tip-time">{formatBucket(hover.t, bucketSeconds)}</div>
          <div>
            <span className="muted">{label}</span> <span className="mono">{format(hover.v)}</span>
          </div>
        </div>
      )}
    </div>
  );
}
