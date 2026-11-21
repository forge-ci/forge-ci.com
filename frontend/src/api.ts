// Typed access to the backend. Every response is parsed from `unknown`, so a
// malformed payload fails loudly here instead of drawing nonsense.

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
export type Sample = Readonly<Record<MetricName | "ts", number>>;

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
  return out as Sample;
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

export function parseHosts(value: unknown): HostStatus[] {
  if (!Array.isArray(value)) throw new PayloadError("hosts: expected an array");
  return value.map((item: unknown) => {
    const r = record(item, "host status");
    return {
      host: parseHostInfo(r["host"]),
      online: r["online"] === true,
      lastSeen: r["last_seen"] === null ? null : num(r["last_seen"], "last_seen"),
      latest: r["latest"] === null ? null : parseSample(r["latest"]),
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
    const cols = new Map<MetricName, number[]>(
      METRIC_FIELDS.map((f) => [f, numArray(columns[f], `${id}.${f}`)]),
    );
    hosts.set(
      id,
      ts.map((t, i) => {
        const s: Partial<Record<MetricName | "ts", number>> = { ts: t };
        for (const f of METRIC_FIELDS) s[f] = cols.get(f)?.[i] ?? 0;
        return s as Sample;
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
