import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

import type { Sample } from "./api";
import { clock } from "./format";

/** A line chart of one metric, one series per host, on a single y axis. */
export interface ChartSpec {
  readonly id: string;
  readonly title: string;
  readonly description: string;
  readonly value: (s: Sample) => number | null;
  readonly format: (v: number) => string;
  readonly formatTicks?: (ticks: readonly number[]) => string[];
  /** Fixed upper bound (percentages); otherwise the axis fits the data from 0. */
  readonly fixedMax?: number;
}

export interface SeriesSpec {
  readonly id: string;
  readonly label: string;
  /** CSS custom property holding the series colour, e.g. `--series-1`. */
  readonly colorVar: string;
}

interface Theme {
  readonly series: readonly string[];
  readonly text: string;
  readonly muted: string;
  readonly grid: string;
  readonly axis: string;
}

/** A host silent for longer than this breaks its line instead of bridging. */
const GAP_S = 20;
const HEIGHT = 200;
const LABEL_GUTTER = 150;

function readTheme(root: HTMLElement, series: readonly SeriesSpec[]): Theme {
  const css = getComputedStyle(root);
  const v = (name: string): string => css.getPropertyValue(name).trim();
  return {
    series: series.map((s) => v(s.colorVar)),
    text: v("--text-secondary"),
    muted: v("--text-muted"),
    grid: v("--grid"),
    axis: v("--axis"),
  };
}

/** Align every host's samples on one shared x axis (the union of sample times).
 *
 * At another host's sample time a line takes the value interpolated between its
 * own neighbouring samples, provided they are at most GAP_S apart; anywhere else
 * (before its first sample, after its last, inside a silence) it is `null`, so
 * uPlot breaks the line exactly across the silence. No `undefined` is emitted:
 * uPlot measures a gap between neighbouring x positions, and with `undefined`
 * those neighbours can belong to another host, shrinking the gap to nothing. */
export function alignSeries(
  hosts: readonly (readonly Sample[])[],
  value: (s: Sample) => number | null,
  start: number,
  end: number,
): uPlot.AlignedData {
  const points = hosts.map((samples) =>
    samples.filter((s) => s.ts >= start && s.ts <= end).map((s) => [s.ts, value(s)] as const),
  );
  return alignPoints(points);
}

function alignPoints(
  points: readonly (readonly (readonly [number, number | null])[])[],
): uPlot.AlignedData {
  const xs = [...new Set(points.flatMap((p) => p.map(([x]) => x)))].sort((a, b) => a - b);
  const ys = points.map((pts) => {
    const out: (number | null)[] = [];
    let k = 0;
    for (const x of xs) {
      while (k + 1 < pts.length && (pts[k + 1]?.[0] ?? Infinity) <= x) k += 1;
      const a = pts[k];
      const b = pts[k + 1];
      if (a === undefined || x < a[0]) out.push(null);
      else if (x === a[0]) out.push(a[1]);
      else if (a[1] !== null && b !== undefined && b[1] !== null && b[0] - a[0] <= GAP_S) {
        out.push(a[1] + ((b[1] - a[1]) * (x - a[0])) / (b[0] - a[0]));
      } else out.push(null);
    }
    return out;
  });
  return [xs, ...ys];
}

function nearestValue(u: uPlot, seriesIdx: number, idx: number): number | null {
  const y = u.data[seriesIdx]?.[idx];
  return typeof y === "number" ? y : null;
}

export class LiveChart {
  private plot: uPlot | null = null;
  private theme: Theme;
  private readonly plotEl: HTMLDivElement;
  private readonly tooltip: HTMLDivElement;
  private readonly resize: ResizeObserver;
  private window: [number, number] = [0, 1];

  constructor(
    private readonly root: HTMLElement,
    private readonly card: HTMLElement,
    private readonly spec: ChartSpec,
    private readonly series: readonly SeriesSpec[],
  ) {
    this.theme = readTheme(root, series);
    this.plotEl = document.createElement("div");
    this.plotEl.className = "plot";
    this.tooltip = document.createElement("div");
    this.tooltip.className = "tooltip";
    this.tooltip.setAttribute("role", "status");
    this.tooltip.hidden = true;
    this.plotEl.append(this.tooltip);
    card.append(this.plotEl);
    this.plotEl.addEventListener("pointerleave", () => {
      this.tooltip.hidden = true;
    });
    this.resize = new ResizeObserver(() => {
      this.plot?.setSize({ width: this.width(), height: HEIGHT });
    });
    this.resize.observe(this.plotEl);
  }

  private width(): number {
    return Math.max(280, Math.floor(this.plotEl.clientWidth));
  }

  /** Re-read colours (after a light/dark switch) and rebuild the plot. */
  retheme(): void {
    this.theme = readTheme(this.root, this.series);
    this.tooltip.hidden = true;
    const data = this.plot?.data;
    this.plot?.destroy();
    this.plot = null;
    if (data !== undefined) this.draw(data);
  }

  update(hosts: readonly (readonly Sample[])[], start: number, end: number): void {
    this.window = [start, end];
    this.draw(alignSeries(hosts, this.spec.value, start, end));
  }

  updateSeries(
    points: readonly (readonly (readonly [number, number | null])[])[],
    start: number,
    end: number,
  ): void {
    this.window = [start, end];
    this.draw(alignPoints(points));
  }

  private draw(data: uPlot.AlignedData): void {
    if (this.plot === null) {
      this.plot = new uPlot(this.options(), data, this.plotEl);
      this.plotEl.append(this.tooltip);
    } else {
      this.plot.setData(data);
    }
  }

  private options(): uPlot.Options {
    const t = this.theme;
    const fmt = this.spec.format;
    const fixedMax = this.spec.fixedMax;
    const axis: uPlot.Axis = {
      stroke: t.muted,
      grid: { stroke: t.grid, width: 1 },
      ticks: { stroke: t.axis, width: 1, size: 4 },
      font: "11px system-ui, -apple-system, 'Segoe UI', sans-serif",
    };
    return {
      width: this.width(),
      height: HEIGHT,
      padding: [12, LABEL_GUTTER, 0, 0],
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 8 } },
      scales: {
        x: { time: true, range: (): uPlot.Range.MinMax => this.window },
        y: {
          range: (_u, _min, max): uPlot.Range.MinMax =>
            fixedMax !== undefined ? [0, fixedMax] : [0, max > 0 ? max * 1.15 : 1],
        },
      },
      axes: [
        { ...axis },
        {
          ...axis,
          size: 64,
          values: (_u, splits): string[] =>
            this.spec.formatTicks?.(splits) ?? splits.map((v) => fmt(v)),
        },
      ],
      series: [
        {},
        ...this.series.map((s, i): uPlot.Series => ({
          label: s.label,
          stroke: t.series[i] ?? t.text,
          width: 2,
          spanGaps: false,
          points: { show: false },
        })),
      ],
      hooks: {
        draw: [
          (u): void => {
            this.drawDirectLabels(u);
          },
        ],
        setCursor: [
          (u): void => {
            this.showTooltip(u);
          },
        ],
      },
    };
  }

  /** Name and latest value at the right end of each line, nudged apart. */
  private drawDirectLabels(u: uPlot): void {
    const ctx = u.ctx;
    const dpr = devicePixelRatio;
    const right = u.bbox.left + u.bbox.width;
    const labels: { y: number; text: string; color: string }[] = [];
    const xs = u.data[0];
    const liveAfter = this.window[1] - GAP_S;
    this.series.forEach((s, i) => {
      const ys = u.data[i + 1];
      if (ys === undefined) return;
      for (let j = ys.length - 1; j >= 0; j--) {
        const y = ys[j];
        if (typeof y !== "number") continue;
        // Label only a host that is still reporting; a stale line ends unlabelled.
        if ((xs[j] ?? 0) < liveAfter) return;
        labels.push({
          y: u.valToPos(y, "y", true),
          text: `${s.label} ${this.spec.format(y)}`,
          color: this.theme.series[i] ?? this.theme.text,
        });
        return;
      }
    });
    labels.sort((a, b) => a.y - b.y);
    const minGap = 14 * dpr;
    for (let i = 1; i < labels.length; i++) {
      const prev = labels[i - 1];
      const cur = labels[i];
      if (prev !== undefined && cur !== undefined && cur.y - prev.y < minGap) {
        cur.y = prev.y + minGap;
      }
    }
    ctx.save();
    ctx.font = `${String(11 * dpr)}px system-ui, -apple-system, 'Segoe UI', sans-serif`;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left"; // uPlot leaves the context right-aligned after its axes
    for (const label of labels) {
      ctx.strokeStyle = label.color;
      ctx.lineWidth = 2 * dpr;
      ctx.beginPath();
      ctx.moveTo(right + 6 * dpr, label.y);
      ctx.lineTo(right + 14 * dpr, label.y);
      ctx.stroke();
      ctx.fillStyle = this.theme.text;
      ctx.fillText(label.text, right + 18 * dpr, label.y);
    }
    ctx.restore();
  }

  private showTooltip(u: uPlot): void {
    const idx = u.cursor.idx;
    const left = u.cursor.left;
    const x = idx === null || idx === undefined ? undefined : u.data[0][idx];
    // uPlot snaps to the nearest sample however far away it is; only show a
    // readout when that sample is close to where the pointer actually is.
    if (
      idx === null ||
      idx === undefined ||
      x === undefined ||
      left === undefined ||
      left < 0 ||
      Math.abs(u.posToVal(left, "x") - x) > GAP_S
    ) {
      this.tooltip.hidden = true;
      return;
    }
    const rows = this.series.map((s, i) => {
      const value = nearestValue(u, i + 1, idx);
      const row = document.createElement("div");
      row.className = "tooltip-row";
      const key = document.createElement("span");
      key.className = "line-key";
      key.style.backgroundColor = this.theme.series[i] ?? this.theme.text;
      const strong = document.createElement("strong");
      strong.textContent = value === null ? "no sample" : this.spec.format(value);
      const name = document.createElement("span");
      name.className = "tooltip-label";
      name.textContent = s.label;
      row.append(key, strong, name);
      return row;
    });
    const time = document.createElement("div");
    time.className = "tooltip-time";
    time.textContent = clock(x);
    this.tooltip.replaceChildren(time, ...rows);
    this.tooltip.hidden = false;
    const plotLeft = u.bbox.left / devicePixelRatio;
    const flip = left > (u.bbox.width / devicePixelRatio) * 0.6;
    this.tooltip.style.left = `${String(plotLeft + left + (flip ? -12 : 12))}px`;
    this.tooltip.style.transform = flip ? "translateX(-100%)" : "none";
  }

  destroy(): void {
    this.resize.disconnect();
    this.plot?.destroy();
    this.plotEl.remove();
    this.card.remove();
  }
}
