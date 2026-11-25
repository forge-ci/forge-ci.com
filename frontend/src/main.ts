import "./style.css";

import {
  fetchHistory,
  fetchHosts,
  parseSampleEvent,
  type HostInfo,
  type HostStatus,
  type Sample,
} from "./api";
import { LiveChart, type ChartSpec, type SeriesSpec } from "./charts";
import { duration, gib, pct, rate, ratio } from "./format";
import { mountGitOps } from "./git-ops";

const KEEP_S = 2 * 60 * 60;
const ONLINE_AFTER_S = 20;
const WINDOWS = [
  { seconds: 5 * 60, label: "5 min" },
  { seconds: 15 * 60, label: "15 min" },
  { seconds: 60 * 60, label: "1 hour" },
  { seconds: 2 * 60 * 60, label: "2 hours" },
] as const;

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
  {
    id: "net",
    title: "Network throughput",
    description: "Bytes in plus out per second on physical interfaces.",
    value: (s) => s.net_rx_bps + s.net_tx_bps,
    format: rate,
  },
  {
    id: "disk",
    title: "Disk throughput",
    description: "Bytes read plus written per second across whole disks.",
    value: (s) => s.disk_read_bps + s.disk_write_bps,
    format: rate,
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
let windowSeconds: number = WINDOWS[1].seconds;
let clockOffset = 0; // server time minus browser time, in seconds
let connected = false;
let charts: LiveChart[] = [];

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
const runnerSummary = el("section", "host-card");
const hostGrid = el("section", "hosts");
const chartGrid = el("section", "charts");
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
  actions.append(connection, connect, logout);
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
  const fleetHeading = el("h2", undefined, "Runner fleet");
  const utilization = el("details", "dashboard-disclosure");
  utilization.open = true;
  utilization.append(
    el("summary", undefined, "Fleet utilization charts"),
    runnerSummary,
    controls,
    chartGrid,
  );
  root.replaceChildren(header, utilization, gitOps, fleetHeading, hostGrid, details, footer);
  mountGitOps(gitOps);
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
    chartGrid.append(card);
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
    card.append(head, el("p", "host-role", `${h.role} · ${h.location}`));

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
  hostGrid.replaceChildren(...cards);
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
  tableBody.replaceChildren(...rows);
}

function renderConnection(): void {
  connection.className = `pill ${connected ? "status-live" : "status-stale"}`;
  connection.replaceChildren(
    el("span", "status-icon", connected ? "●" : "▲"),
    el("span", undefined, connected ? "Streaming" : "Reconnecting…"),
  );
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
  const tiles = el("div", "tiles");
  for (const host of hosts) {
    const index = runnerHosts.indexOf(host.id);
    const count = index < 0 ? "Not monitored" : String(counts[index] ?? "Unavailable");
    const sample = samples.get(host.id)?.at(-1);
    const detail =
      sample !== undefined && now - sample.ts <= ONLINE_AFTER_S
        ? `CPU ${pct(sample.cpu_pct)} · Load ${ratio(sample.load1 / sample.cpu_count)} per CPU`
        : undefined;
    tiles.append(tile(host.label, count, detail));
  }
  runnerSummary.replaceChildren(
    el("h2", undefined, `GitHub Actions jobs running on Dell runners: ${label}`),
    tiles,
  );
}

function redraw(): void {
  const now = serverNow();
  const series = hosts.map((h) => samples.get(h.id) ?? []);
  for (const chart of charts) chart.update(series, now - windowSeconds, now);
  renderRunnerJobs(now);
  renderHosts(now);
  renderTable(now);
  renderConnection();
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

async function refreshRunnerJobs(): Promise<void> {
  for (const { host, latest } of await fetchHosts()) {
    if (latest !== null) updateRunnerSample(host.id, latest);
  }
}

async function main(): Promise<void> {
  const statuses: HostStatus[] = await fetchHosts();
  hosts = statuses.map((s) => s.host);
  for (const { host, latest } of statuses) {
    if (latest !== null) updateRunnerSample(host.id, latest);
  }
  buildShell();
  buildCharts();
  await loadHistory();
  redraw();
  connect();
  setInterval(redraw, 1000);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    for (const chart of charts) chart.retheme();
  });
}

main().catch((error: unknown) => {
  reportError(error);
  root.replaceChildren(
    el("p", "fatal", "The dashboard could not load its data. Reload to try again."),
  );
});
