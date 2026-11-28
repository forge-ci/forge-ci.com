// Typed access to the backend. Every response is parsed from `unknown`, so a
// malformed payload fails loudly here instead of drawing nonsense.

import { type Hardware, parseHardware } from "./hardware";
import { parseLanes, type RunnerLane } from "./lanes";

export const METRIC_FIELDS = [
  "cpu_pct",
  "cpu_count",
  "load1",
  "load5",
  "load15",
  "mem_total_bytes",
  "mem_used_bytes",
  "swap_total_bytes",
  "swap_used_bytes",
  "net_rx_bps",
  "net_tx_bps",
  "disk_read_bps",
  "disk_write_bps",
  "uptime_s",
] as const;

export type MetricName = (typeof METRIC_FIELDS)[number];
const diagnosticFields = [
  "cpu_on_pct",
  "cpu_iowait_pct",
  "cpu_steal_pct",
  "psi_cpu_some_pct",
  "psi_io_some_pct",
  "psi_io_full_pct",
  "psi_memory_some_pct",
] as const;
const deviceFields = ["busy_pct", "queue_depth", "await_ms", "read_bps", "write_bps"] as const;
export type DiagnosticField = (typeof diagnosticFields)[number];
export type DeviceField = (typeof deviceFields)[number];
type DiagnosticValues = Partial<Record<DiagnosticField, number>>;
type DeviceValues = Partial<Record<DeviceField, number>>;
interface Diagnostics {
  readonly counters: DiagnosticValues;
  readonly devices: ReadonlyMap<string, DeviceValues>;
}

function optionalNumbers<K extends string>(
  value: unknown,
  fields: readonly K[],
): Partial<Record<K, number>> {
  const result: Partial<Record<K, number>> = {};
  const raw = record(value, "diagnostic counters");
  for (const field of fields) {
    const v = raw[field];
    if (typeof v === "number" && Number.isFinite(v)) result[field] = v;
  }
  return result;
}

function diagnostics(value: unknown): Diagnostics | undefined {
  if (value === null || value === undefined) return undefined;
  const raw = record(value, "diagnostics");
  const devices = new Map(
    Object.entries(record(raw["devices"], "diagnostics.devices")).map(
      ([name, values]) => [name, optionalNumbers(values, deviceFields)] as const,
    ),
  );
  return { counters: optionalNumbers(raw, diagnosticFields), devices };
}

export interface AttestationRequest {
  readonly command: string;
  readonly crate: string | null;
  readonly elapsedS: number;
  readonly cpuPct: number;
}

export interface Attestation {
  readonly running: number;
  readonly cpuPct: number;
  readonly requests: readonly AttestationRequest[];
  readonly observedAt: number;
}

/** Attestation work a host reported, or undefined when absent or malformed. */
export function parseAttestation(value: unknown): Attestation | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const r = value as Record<string, unknown>;
  const { running, cpu_pct: cpuPct, observed_at: observedAt, requests } = r;
  if (
    typeof running !== "number" ||
    typeof cpuPct !== "number" ||
    typeof observedAt !== "number" ||
    !Array.isArray(requests)
  ) {
    return undefined;
  }
  const parsed = requests.flatMap((item: unknown): AttestationRequest[] => {
    if (typeof item !== "object" || item === null) return [];
    const q = item as Record<string, unknown>;
    const { command, crate, elapsed_s: elapsedS, cpu_pct: pct } = q;
    if (typeof command !== "string" || typeof elapsedS !== "number" || typeof pct !== "number") {
      return [];
    }
    return [{ command, crate: typeof crate === "string" ? crate : null, elapsedS, cpuPct: pct }];
  });
  return { running, cpuPct, observedAt, requests: parsed };
}

export type Sample = Readonly<Record<MetricName | "ts", number>> & {
  readonly diagnostics?: Diagnostics;
  readonly runnerCapacity?: { readonly slots: number; readonly observedAt: number };
  readonly runnerJobs?: { readonly count: number; readonly observedAt: number };
  readonly attestation?: Attestation;
  readonly runnerLanes?: readonly RunnerLane[];
};

export interface HostInfo {
  readonly id: string;
  readonly label: string;
  readonly role: string;
  readonly location: string;
}

export interface HostStatus {
  readonly host: HostInfo;
  readonly online: boolean;
  readonly lastSeen: number | null;
  readonly latest: Sample | null;
  readonly hardware?: Hardware;
}

export interface History {
  readonly now: number;
  readonly hosts: ReadonlyMap<string, Sample[]>;
}

export interface SampleEvent {
  readonly host: string;
  readonly sample: Sample;
}

class PayloadError extends Error {
  override name = "PayloadError";
}

function record(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new PayloadError(`${what}: expected an object`);
  }
  return value as Record<string, unknown>;
}

function num(value: unknown, what: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new PayloadError(`${what}: expected a finite number`);
  }
  return value;
}

function str(value: unknown, what: string): string {
  if (typeof value !== "string") throw new PayloadError(`${what}: expected a string`);
  return value;
}

function numArray(value: unknown, what: string): number[] {
  if (!Array.isArray(value)) throw new PayloadError(`${what}: expected an array`);
  return value.map((v: unknown, i) => num(v, `${what}[${String(i)}]`));
}

export function parseSample(value: unknown): Sample {
  const r = record(value, "sample");
  const out: Partial<Record<MetricName | "ts", number>> = { ts: num(r["ts"], "sample.ts") };
  for (const field of METRIC_FIELDS) out[field] = num(r[field], `sample.${field}`);
  const count = r["gh_jobs_running"];
  const observedAt = r["gh_jobs_updated_at"];
  const diag = diagnostics(r["diagnostics"]);
  const slots = r["gh_runner_slots"];
  const slotsAt = r["gh_runner_slots_updated_at"];
  const capacity =
    typeof slots === "number" &&
    Number.isSafeInteger(slots) &&
    slots >= 0 &&
    typeof slotsAt === "number" &&
    Number.isFinite(slotsAt) &&
    slotsAt > 0
      ? { runnerCapacity: { slots, observedAt: slotsAt } }
      : {};
  const attestation = parseAttestation(r["attestation"]);
  const lanes = parseLanes(r["gh_lanes"]);
  const sample: Sample = {
    ...(out as Sample),
    ...(attestation === undefined ? {} : { attestation }),
    ...(diag === undefined ? {} : { diagnostics: diag }),
    ...capacity,
    ...(lanes === undefined ? {} : { runnerLanes: lanes }),
  };
  if (
    typeof count === "number" &&
    Number.isSafeInteger(count) &&
    count >= 0 &&
    typeof observedAt === "number" &&
    Number.isFinite(observedAt) &&
    observedAt > 0
  ) {
    return { ...sample, runnerJobs: { count, observedAt } };
  }
  return sample;
}

function parseHostInfo(value: unknown): HostInfo {
  const r = record(value, "host");
  return {
    id: str(r["id"], "host.id"),
    label: str(r["label"], "host.label"),
    role: str(r["role"], "host.role"),
    location: str(r["location"], "host.location"),
  };
}

function hardwareOf(value: unknown): { hardware?: Hardware } {
  const hardware = parseHardware(value);
  return hardware === undefined ? {} : { hardware };
}

export function parseHosts(value: unknown): HostStatus[] {
  if (!Array.isArray(value)) throw new PayloadError("hosts: expected an array");
  return value.map((item: unknown) => {
    const r = record(item, "host status");
    return {
      host: parseHostInfo(r["host"]),
      online: r["online"] === true,
      lastSeen: r["last_seen"] === null ? null : num(r["last_seen"], "last_seen"),
      latest: r["latest"] === null ? null : parseSample(r["latest"]),
      ...hardwareOf(r["hardware"]),
    };
  });
}

export function parseHistory(value: unknown): History {
  const r = record(value, "history");
  const hosts = new Map<string, Sample[]>();
  for (const [id, raw] of Object.entries(record(r["hosts"], "history.hosts"))) {
    const series = record(raw, `history.${id}`);
    const ts = numArray(series["ts"], `${id}.ts`);
    const columns = record(series["columns"], `${id}.columns`);
    const rawDiagnostics = series["diagnostics"];
    const diag = Array.isArray(rawDiagnostics) ? rawDiagnostics.map(diagnostics) : [];
    const cols = new Map<MetricName, number[]>(
      METRIC_FIELDS.map((f) => [f, numArray(columns[f], `${id}.${f}`)]),
    );
    hosts.set(
      id,
      ts.map((t, i) => {
        const s: Partial<Record<MetricName | "ts", number>> = { ts: t };
        for (const f of METRIC_FIELDS) s[f] = cols.get(f)?.[i] ?? 0;
        const row = diag[i];
        return row === undefined ? (s as Sample) : { ...(s as Sample), diagnostics: row };
      }),
    );
  }
  return { now: num(r["now"], "history.now"), hosts };
}

export function parseSampleEvent(data: string): SampleEvent {
  const r = record(JSON.parse(data) as unknown, "event");
  return { host: str(r["host"], "event.host"), sample: parseSample(r["sample"]) };
}

async function getJson(path: string): Promise<unknown> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path}: HTTP ${String(response.status)}`);
  return (await response.json()) as unknown;
}

export async function fetchHosts(): Promise<HostStatus[]> {
  return parseHosts(await getJson("/api/hosts"));
}

export async function fetchHistory(seconds: number): Promise<History> {
  return parseHistory(await getJson(`/api/history?seconds=${String(seconds)}`));
}
