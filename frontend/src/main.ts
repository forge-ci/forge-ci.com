import "./style.css";

import {
  type Attestation,
  fetchHistory,
  fetchHosts,
  parseSampleEvent,
  type DiagnosticField,
  type HostInfo,
  type HostStatus,
  type Sample,
} from "./api";
import { mountAttestationStats } from "./attestation-stats";
import { LiveChart, type ChartSpec, type SeriesSpec } from "./charts";
import { mountTabs } from "./dashboard-tabs";
import { DeviceChart } from "./device-chart";
import { duration, gib, pct, rate, rateTicks, ratio } from "./format";
import { mountGitOps } from "./git-ops";
import { type Hardware, hardwareDetail, hardwareSummary } from "./hardware";
import { morph, UpdateGate } from "./live-dom";

const KEEP_S = 2 * 60 * 60;
// Charts are canvases and cheap to redraw; text panels change less often and
// are patched in place on a slower cadence so they stay readable and copyable.
const CHART_TICK_MS = 2000;
const TEXT_TICK_MS = 5000;
const ONLINE_AFTER_S = 20;
const CAPACITY_AFTER_S = 5 * 60;
const WINDOWS = [
  { seconds: 5 * 60, label: "5 min" },
  { seconds: 15 * 60, label: "15 min" },
  { seconds: 60 * 60, label: "1 hour" },
  { seconds: 2 * 60 * 60, label: "2 hours" },
] as const;

function diagnosticChart(field: DiagnosticField, title: string, description: string): ChartSpec {
  return {
    id: field,
    title,
    description,
    fixedMax: 100,
    format: pct,
    value: (sample) => sample.diagnostics?.counters[field] ?? null,
  };
}

const CHARTS: readonly ChartSpec[] = [
  {
    id: "cpu",
    title: "CPU busy time",
    description:
      "Percent of aggregate logical CPU time between samples spent in user, kernel, interrupt, or steal states. Idle and I/O-wait time are excluded.",
    value: (s) => s.cpu_pct,
    format: pct,
    fixedMax: 100,
  },
  {
    id: "mem",
    title: "Memory in use",
    description: "Total minus MemAvailable: page cache that can be reclaimed is not counted.",
    value: (s) => (100 * s.mem_used_bytes) / s.mem_total_bytes,
    format: pct,
    fixedMax: 100,
  },
  {
    id: "load",
    title: "Linux load per logical CPU",
    description:
      "1-minute average of runnable and uninterruptible tasks, divided by logical CPUs. 1.0 is one task per CPU on average. I/O waits can raise load while CPUs are idle.",
    value: (s) => s.load1 / s.cpu_count,
    format: ratio,
  },
  diagnosticChart(
    "cpu_on_pct",
    "On-CPU time",
    "CPU time spent executing work, including interrupts. Steal is excluded.",
  ),
  diagnosticChart(
    "cpu_iowait_pct",
    "CPU I/O wait",
    "CPU accounting time spent idle with outstanding I/O. Compare with I/O task stalls to see how much work is blocked.",
  ),
  diagnosticChart("cpu_steal_pct", "CPU steal", "CPU time a hypervisor took away from this guest."),
  diagnosticChart(
    "psi_cpu_some_pct",
    "CPU contention",
    "10-second Linux PSI. Share of wall time when at least one runnable task was waiting for a CPU.",
  ),
  diagnosticChart(
    "psi_io_some_pct",
    "I/O stalls",
    "10-second Linux PSI. Share of wall time when at least one task stalled on I/O.",
  ),
  diagnosticChart(
    "psi_io_full_pct",
    "I/O stalls across all active tasks",
    "10-second Linux PSI. Share of wall time when all non-idle tasks stalled on I/O together.",
  ),
  diagnosticChart(
    "psi_memory_some_pct",
    "Memory stalls",
    "10-second Linux PSI. Share of wall time when at least one task stalled on memory pressure.",
  ),
  {
    id: "net",
    title: "Network throughput",
    description: "Bytes in plus out per second on physical interfaces.",
    value: (s) => s.net_rx_bps + s.net_tx_bps,
    format: rate,
    formatTicks: rateTicks,
  },
  {
    id: "disk",
    title: "Disk throughput",
    description: "Bytes read plus written per second across whole disks.",
    value: (s) => s.disk_read_bps + s.disk_write_bps,
    format: rate,
    formatTicks: rateTicks,
  },
];

// ---------------------------------------------------------------- state

const runnerHosts = ["dell-jw22zc3", "dell-fsx3mn2"];
const runnerSamples = new Map<string, Sample>();

function updateRunnerSample(host: string, sample: Sample): void {
  if (runnerHosts.includes(host) && sample.ts > (runnerSamples.get(host)?.ts ?? 0)) {
    runnerSamples.set(host, sample);
  }
}

const samples = new Map<string, Sample[]>();
let hosts: HostInfo[] = [];
const hardware = new Map<string, Hardware>();
let windowSeconds: number = WINDOWS[1].seconds;
let clockOffset = 0; // server time minus browser time, in seconds
let connected = false;
let charts: LiveChart[] = [];
let deviceChart: DeviceChart | undefined;
let paused = false;
let lastRender = 0; // browser ms of the last applied text update

const serverNow = (): number => Date.now() / 1000 + clockOffset;

function addSample(hostId: string, sample: Sample): void {
  const list = samples.get(hostId) ?? [];
  const last = list.at(-1);
  if (last !== undefined && sample.ts <= last.ts) return;
  list.push(sample);
  const cutoff = sample.ts - KEEP_S;
  let drop = 0;
  while (drop < list.length && (list[drop]?.ts ?? cutoff) < cutoff) drop += 1;
  if (drop > 0) list.splice(0, drop);
  samples.set(hostId, list);
}

// ---------------------------------------------------------------- DOM helpers

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className !== undefined) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function requireElement(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`#${id} missing from index.html`);
  return node;
}

// ---------------------------------------------------------------- layout

const root = requireElement("app");
const connection = el("span", "pill");
const freshness = el("span", "freshness");
const pauseButton = el("button", "pause-toggle", "Pause updates");
const gate = new UpdateGate(root);
const runnerSummary = el("section", "host-card");
const hostGrid = el("section", "hosts");
const chartGrid = el("section", "charts");
const cpuGrid = el("section", "charts");
const pressureGrid = el("section", "charts");
const tableBody = el("tbody");

function seriesSpecs(): SeriesSpec[] {
  return hosts.map((h, i) => ({ id: h.id, label: h.label, colorVar: `--series-${String(i + 1)}` }));
}

function buildShell(): void {
  const header = el("header", "top");
  const titles = el("div");
  titles.append(
    el("h1", undefined, "forge-ci"),
    el("p", "subtitle", "GitHub Actions and runner fleet"),
  );
  const actions = el("nav", "dashboard-actions");
  actions.setAttribute("aria-label", "Account");
  const connect = el("a", undefined, "Connect Soldr");
  connect.href = "/soldr/pair";
  const logout = el("form");
  logout.method = "post";
  logout.action = "/logout";
  const signOut = el("button", undefined, "Sign out");
  signOut.type = "submit";
  logout.append(signOut);
  freshness.setAttribute("aria-live", "off");
  pauseButton.type = "button";
  pauseButton.setAttribute("aria-pressed", "false");
  pauseButton.addEventListener("click", () => {
    paused = !paused;
    pauseButton.setAttribute("aria-pressed", String(paused));
    pauseButton.textContent = paused ? "Resume updates" : "Pause updates";
    if (!paused) redraw();
    renderFreshness();
  });
  actions.append(freshness, pauseButton, connection, connect, logout);
  header.append(titles, actions);

  const controls = el("div", "controls");
  const group = el("div", "segmented");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Time window");
  for (const w of WINDOWS) {
    const button = el("button", undefined, w.label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(w.seconds === windowSeconds));
    button.addEventListener("click", () => {
      windowSeconds = w.seconds;
      for (const b of group.querySelectorAll("button")) {
        b.setAttribute("aria-pressed", String(b === button));
      }
      redraw();
    });
    group.append(button);
  }
  controls.append(el("span", "controls-label", "Window"), group);

  const details = el("details", "table-view");
  const summary = el("summary", undefined, "Latest values as a table");
  const table = el("table");
  const head = el("thead");
  const headRow = el("tr");
  for (const name of ["Host", "CPU", "Memory", "Load (1m)", "Network in", "Network out"]) {
    headRow.append(el("th", undefined, name));
  }
  for (const name of ["Disk read", "Disk write", "Uptime", "Last sample"]) {
    headRow.append(el("th", undefined, name));
  }
  head.append(headRow);
  table.append(head, tableBody);
  details.append(summary, table);

  const footer = el("footer");
  footer.append(
    el("span", undefined, "Each host's forge-metrics-agent pushes a sample every 5 s. "),
  );
  const source = el("a", undefined, "Source");
  source.href = "https://github.com/forge-ci/forge-ci.com";
  source.rel = "noopener";
  footer.append(source);

  const gitOps = el("div");
  const utilization = el("div");
  utilization.append(runnerSummary, chartGrid);
  deviceChart = new DeviceChart(root, hosts);
  const devices = el("div");
  devices.append(deviceChart.element);
  const fleet = el("div");
  const attestations = el("div");
  fleet.append(el("h2", undefined, "Runner fleet"), hostGrid, details);
  const tabs = [
    { id: "utilization", label: "Utilization", panel: utilization, timeWindow: true },
    { id: "cpu", label: "CPU timing", panel: cpuGrid, timeWindow: true },
    { id: "pressure", label: "Pressure", panel: pressureGrid, timeWindow: true },
    { id: "devices", label: "Block devices", panel: devices, timeWindow: true },
    { id: "git-ops", label: "Git Ops", panel: gitOps },
    { id: "attestations", label: "Attestations", panel: attestations },
    { id: "hosts", label: "Hosts", panel: fleet },
  ];
  root.replaceChildren(
    header,
    mountTabs(tabs, controls),
    controls,
    ...tabs.map((tab) => tab.panel),
    footer,
  );
  mountGitOps(gitOps);
  mountAttestationStats(attestations);
}

function buildCharts(): void {
  for (const chart of charts) chart.destroy();
  const series = seriesSpecs();
  charts = CHARTS.map((spec) => {
    const card = el("article", "chart-card");
    card.setAttribute("aria-label", spec.title);
    const head = el("div", "chart-head");
    head.append(el("h2", undefined, spec.title));
    const legend = el("ul", "legend");
    series.forEach((s) => {
      const item = el("li");
      const key = el("span", "line-key");
      key.style.backgroundColor = `var(${s.colorVar})`;
      item.append(key, el("span", undefined, s.label));
      legend.append(item);
    });
    head.append(legend);
    card.append(head, el("p", "chart-desc", spec.description));
    const grid = spec.id.startsWith("psi_")
      ? pressureGrid
      : spec.id.startsWith("cpu_")
        ? cpuGrid
        : chartGrid;
    grid.append(card);
    return new LiveChart(root, card, spec, series);
  });
}

// ---------------------------------------------------------------- host cards

interface Status {
  readonly kind: "live" | "stale" | "none";
  readonly text: string;
}

function hostStatus(id: string, now: number): Status {
  const last = samples.get(id)?.at(-1);
  if (last === undefined) return { kind: "none", text: "No data yet" };
  const age = now - last.ts;
  if (age <= ONLINE_AFTER_S) return { kind: "live", text: "Live" };
  return { kind: "stale", text: `Silent for ${duration(age)}` };
}

function tile(label: string, value: string, detail?: string): HTMLElement {
  const t = el("div", "tile");
  t.append(el("div", "tile-label", label), el("div", "tile-value", value));
  if (detail !== undefined) t.append(el("div", "tile-detail", detail));
  return t;
}

const HARDWARE_REFRESH_MS = 60_000;

/** One-line machine summary; the full model strings and disk roles show on hover. */
function hardwareLine(id: string): HTMLElement {
  const hw = hardware.get(id);
  if (hw === undefined) return el("p", "host-hw host-hw-missing", "Hardware not reported yet");
  const line = el("p", "host-hw", hardwareSummary(hw));
  line.title = hardwareDetail(hw).join("\n");
  return line;
}

function hardwareList(id: string): HTMLElement | undefined {
  const hw = hardware.get(id);
  if (hw === undefined) return undefined;
  const list = el("ul", "host-hw-detail");
  for (const line of hardwareDetail(hw)) list.append(el("li", undefined, line));
  return list;
}

function renderHosts(now: number): void {
  const cards = hosts.map((h, i) => {
    const card = el("article", "host-card");
    const head = el("div", "host-head");
    const name = el("h2");
    const key = el("span", "line-key");
    key.style.backgroundColor = `var(--series-${String(i + 1)})`;
    name.append(key, el("span", undefined, h.label));
    const status = hostStatus(h.id, now);
    const pill = el("span", `status status-${status.kind}`);
    pill.append(
      el("span", "status-icon", status.kind === "live" ? "●" : status.kind === "stale" ? "▲" : "○"),
      el("span", undefined, status.text),
    );
    head.append(name, pill);
    card.append(head, el("p", "host-role", `${h.role} · ${h.location}`), hardwareLine(h.id));
    const detail = hardwareList(h.id);
    if (detail !== undefined) card.append(detail);

    const s = samples.get(h.id)?.at(-1);
    const tiles = el("div", "tiles");
    if (s === undefined) {
      tiles.append(el("p", "empty", "Waiting for the first sample from this host's agent."));
    } else {
      tiles.append(
        tile("CPU", pct(s.cpu_pct), `${String(s.cpu_count)} cores`),
        tile(
          "Memory",
          pct((100 * s.mem_used_bytes) / s.mem_total_bytes),
          `${gib(s.mem_used_bytes)} of ${gib(s.mem_total_bytes)}`,
        ),
        tile("Load (1m)", s.load1.toFixed(1), `${ratio(s.load1 / s.cpu_count)} per core`),
        tile("Network", rate(s.net_rx_bps + s.net_tx_bps), `in ${rate(s.net_rx_bps)}`),
        tile("Disk", rate(s.disk_read_bps + s.disk_write_bps), `write ${rate(s.disk_write_bps)}`),
        tile("Uptime", duration(s.uptime_s)),
      );
    }
    card.append(tiles);
    return card;
  });
  morph(hostGrid, ...cards);
}

function renderTable(now: number): void {
  const rows = hosts.map((h) => {
    const tr = el("tr");
    tr.append(el("th", undefined, h.label));
    const s = samples.get(h.id)?.at(-1);
    const cells =
      s === undefined
        ? ["—", "—", "—", "—", "—", "—", "—", "—", "never"]
        : [
            pct(s.cpu_pct),
            `${gib(s.mem_used_bytes)} / ${gib(s.mem_total_bytes)}`,
            s.load1.toFixed(2),
            rate(s.net_rx_bps),
            rate(s.net_tx_bps),
            rate(s.disk_read_bps),
            rate(s.disk_write_bps),
            duration(s.uptime_s),
            `${duration(now - s.ts)} ago`,
          ];
    for (const c of cells) tr.append(el("td", undefined, c));
    return tr;
  });
  morph(tableBody, ...rows);
}

function renderConnection(): void {
  connection.className = `pill ${connected ? "status-live" : "status-stale"}`;
  morph(
    connection,
    el("span", "status-icon", connected ? "●" : "▲"),
    el("span", undefined, connected ? "Streaming" : "Reconnecting…"),
  );
}

const ATTESTATION_AFTER_S = 30;
const MAX_LISTED_REQUESTS = 6;

function attestationOf(sample: Sample | undefined, now: number): Attestation | undefined {
  const a = sample?.attestation;
  return a !== undefined && now - a.observedAt <= ATTESTATION_AFTER_S ? a : undefined;
}

/** One host: CI build load and attestation work side by side. */
function hostLoad(
  id: string,
  label: string,
  ci: string,
  detail: string | undefined,
  attestation: Attestation | undefined,
): HTMLElement {
  const row = el("div", "host-load");
  row.append(el("h3", "host-load-name", label), hardwareLine(id));
  const pair = el("div", "host-load-pair");
  pair.append(
    tile("CI build load", ci, detail),
    tile(
      "Attestations",
      attestation === undefined ? "None" : String(attestation.running),
      attestation === undefined
        ? "No paired worker reporting"
        : `CPU ${pct(attestation.cpuPct)} of host`,
    ),
  );
  row.append(pair);
  const list = el("ul", "attestation-requests");
  for (const request of attestation?.requests.slice(0, MAX_LISTED_REQUESTS) ?? []) {
    const item = el("li");
    item.append(
      el("span", "request-crate", request.crate ?? "—"),
      el("code", "request-command", request.command),
      el("span", "request-meta", `${duration(request.elapsedS)} · CPU ${pct(request.cpuPct)}`),
    );
    list.append(item);
  }
  if (list.childElementCount > 0) row.append(list);
  return row;
}

function renderRunnerJobs(now: number): void {
  const counts = runnerHosts.map((host) => {
    const jobs = runnerSamples.get(host)?.runnerJobs;
    const age = now - (jobs?.observedAt ?? 0);
    return jobs !== undefined && age >= 0 && age <= 30 ? jobs.count : null;
  });
  const reported = counts.filter((count) => count !== null);
  const total = reported.reduce((sum, count) => sum + count, 0);
  const label =
    reported.length === 0
      ? "Unavailable"
      : reported.length === runnerHosts.length
        ? String(total)
        : `${String(total)} (partial, ${String(reported.length)} of ${String(runnerHosts.length)} hosts)`;
  const tiles = el("div", "host-loads");
  for (const host of hosts) {
    const index = runnerHosts.indexOf(host.id);
    const capacity = runnerSamples.get(host.id)?.runnerCapacity;
    const age = now - (capacity?.observedAt ?? 0);
    const slots =
      capacity !== undefined && age >= 0 && age <= CAPACITY_AFTER_S ? capacity.slots : null;
    const running = counts[index] ?? null;
    const count =
      index < 0
        ? "Not monitored"
        : slots === null
          ? String(running ?? "Unavailable")
          : `${String(running ?? "—")} / ${String(slots)}`;
    const sample = samples.get(host.id)?.at(-1);
    const live = sample !== undefined && now - sample.ts <= ONLINE_AFTER_S ? sample : undefined;
    const counters = live?.diagnostics?.counters;
    const split =
      counters?.cpu_on_pct !== undefined && counters.cpu_iowait_pct !== undefined
        ? ` (busy ${pct(counters.cpu_on_pct)} · iowait ${pct(counters.cpu_iowait_pct)})`
        : "";
    const detail =
      live === undefined
        ? undefined
        : `CPU ${pct(live.cpu_pct)}${split} · Load ${ratio(live.load1 / live.cpu_count)} per CPU`;
    tiles.append(
      hostLoad(
        host.id,
        host.label,
        count,
        detail,
        attestationOf(runnerSamples.get(host.id) ?? live, now),
      ),
    );
  }
  morph(
    runnerSummary,
    el("h2", undefined, `GitHub Actions jobs running on Dell runners: ${label}`),
    el(
      "p",
      "chart-desc",
      "CI build load is running jobs / configured slots; pool reservations and routing can reduce availability. Attestations are soldr paired-worker requests (e.g. cargo clippy for a crate) running on the same host, with their share of host CPU.",
    ),
    tiles,
  );
}

function drawCharts(): void {
  const now = serverNow();
  const series = hosts.map((h) => samples.get(h.id) ?? []);
  for (const chart of charts) chart.update(series, now - windowSeconds, now);
  deviceChart?.update(samples, now - windowSeconds, now);
}

function renderText(): void {
  gate.run(() => {
    const now = serverNow();
    renderRunnerJobs(now);
    renderHosts(now);
    renderTable(now);
    renderConnection();
    lastRender = Date.now();
  });
  renderFreshness();
}

function renderFreshness(): void {
  const age = lastRender === 0 ? null : Math.max(0, (Date.now() - lastRender) / 1000);
  const text = paused
    ? "Updates paused"
    : gate.deferred
      ? "Update held while you select"
      : age === null
        ? "Loading…"
        : `Updated ${duration(age)} ago`;
  if (freshness.textContent !== text) freshness.textContent = text;
}

const live = (): boolean => !paused && document.visibilityState === "visible";

function redraw(): void {
  drawCharts();
  renderText();
}

// ---------------------------------------------------------------- data flow

async function loadHistory(): Promise<void> {
  const history = await fetchHistory(KEEP_S);
  clockOffset = history.now - Date.now() / 1000;
  for (const [id, list] of history.hosts) {
    for (const s of list) addSample(id, s);
  }
}

function connect(): void {
  const source = new EventSource("/api/stream");
  source.addEventListener("open", () => {
    const wasDown = !connected;
    connected = true;
    renderConnection();
    // Fill whatever arrived while the stream was down.
    if (wasDown) void Promise.all([loadHistory(), refreshRunnerJobs()]).then(redraw, reportError);
  });
  source.addEventListener("error", () => {
    connected = false;
    renderConnection();
  });
  source.addEventListener("sample", (event: MessageEvent<string>) => {
    try {
      const { host, sample } = parseSampleEvent(event.data);
      updateRunnerSample(host, sample);
      addSample(host, sample);
    } catch (error: unknown) {
      reportError(error);
    }
  });
  source.addEventListener("resync", () => {
    // The server dropped us for falling behind: reconnect with fresh history.
    source.close();
    connected = false;
    setTimeout(connect, 1000);
  });
}

function reportError(error: unknown): void {
  console.error("forge-ci dashboard:", error);
}

function applyStatus({ host, latest, hardware: hw }: HostStatus): void {
  if (latest !== null) updateRunnerSample(host.id, latest);
  if (hw !== undefined) hardware.set(host.id, hw);
}

async function refreshRunnerJobs(): Promise<void> {
  for (const status of await fetchHosts()) applyStatus(status);
}

async function main(): Promise<void> {
  const statuses: HostStatus[] = await fetchHosts();
  hosts = statuses.map((s) => s.host);
  for (const status of statuses) applyStatus(status);
  buildShell();
  buildCharts();
  await loadHistory();
  redraw();
  connect();
  setInterval(() => {
    if (live()) drawCharts();
  }, CHART_TICK_MS);
  setInterval(() => {
    if (live()) renderText();
    else renderFreshness();
  }, TEXT_TICK_MS);
  setInterval(renderFreshness, 1000);
  // Hardware is static and not streamed; pick it up when an agent first reports it.
  setInterval(() => {
    refreshRunnerJobs().catch(reportError);
  }, HARDWARE_REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (live()) redraw();
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    for (const chart of charts) chart.retheme();
    deviceChart?.retheme();
  });
}

main().catch((error: unknown) => {
  reportError(error);
  root.replaceChildren(
    el("p", "fatal", "The dashboard could not load its data. Reload to try again."),
  );
});
