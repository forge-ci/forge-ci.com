import type { DeviceField, HostInfo, Sample } from "./api";
import { LiveChart, type ChartSpec } from "./charts";
import { pct, rate, ratio } from "./format";

const metrics = {
  busy_pct: ["Device busy time", "Share of the interval the kernel accounted as doing I/O.", pct],
  queue_depth: ["Average queue depth", "Average number of I/O requests outstanding.", ratio],
  await_ms: [
    "Read/write latency",
    "Average time in queue and service per completed read or write.",
    (value: number): string => `${ratio(value)} ms`,
  ],
  read_bps: ["Read throughput", "Bytes read per second.", rate],
  write_bps: ["Write throughput", "Bytes written per second.", rate],
} as const satisfies Record<DeviceField, readonly [string, string, (value: number) => string]>;

export class DeviceChart {
  readonly element = document.createElement("section");
  private readonly host = document.createElement("select");
  private readonly metric = document.createElement("select");
  private readonly description = document.createElement("p");
  private readonly plot = document.createElement("div");
  private chart: LiveChart | undefined;
  private key = "";

  constructor(
    private readonly root: HTMLElement,
    hosts: readonly HostInfo[],
  ) {
    this.element.className = "chart-card";
    const title = document.createElement("h2");
    title.textContent = "Block device diagnostics";
    const controls = document.createElement("div");
    controls.className = "ops-filters";
    this.host.setAttribute("aria-label", "Device host");
    this.metric.setAttribute("aria-label", "Device metric");
    this.host.append(...hosts.map((host) => new Option(host.label, host.id)));
    if (hosts.some((host) => host.id === "dell-fsx3mn2")) this.host.value = "dell-fsx3mn2";
    this.metric.append(
      ...Object.entries(metrics).map(([field, [name]]) => new Option(name, field)),
    );
    controls.append(this.host, this.metric);
    this.description.className = "chart-desc";
    this.element.append(title, controls, this.description, this.plot);
  }

  update(samples: ReadonlyMap<string, Sample[]>, start: number, end: number): void {
    const rows = (samples.get(this.host.value) ?? []).filter(
      (sample) => sample.ts >= start && sample.ts <= end,
    );
    const names = [
      ...new Set(rows.flatMap((sample) => [...(sample.diagnostics?.devices.keys() ?? [])])),
    ].sort();
    const field = this.metric.value as DeviceField;
    const [title, description, format] = metrics[field];
    const key = JSON.stringify([this.host.value, field, names]);
    if (key !== this.key) {
      this.key = key;
      this.chart?.destroy();
      this.chart = undefined;
      if (names.length === 0) {
        this.description.textContent = "No whole-disk observations in this window.";
        return;
      }
      this.description.textContent = `${description} Whole disks only. Hover for per-device values.`;
      const series = names.map((name, i) => ({
        id: name,
        label: name,
        colorVar: `--series-${String((i % 3) + 1)}`,
      }));
      const spec: ChartSpec = {
        id: field,
        title,
        description,
        format,
        value: () => null,
        ...(field === "busy_pct" ? { fixedMax: 100 } : {}),
      };
      this.chart = new LiveChart(this.root, this.plot, spec, series);
    }
    this.chart?.updateSeries(
      names.map((name) =>
        rows.map(
          (sample) => [sample.ts, sample.diagnostics?.devices.get(name)?.[field] ?? null] as const,
        ),
      ),
      start,
      end,
    );
  }

  retheme(): void {
    this.chart?.retheme();
  }
}
