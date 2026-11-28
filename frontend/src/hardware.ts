// Static machine description an agent reports (CPU, RAM, disks), rendered as a
// one-line summary with the full model strings and disk roles in a detail list.

export type DiskKind = "nvme" | "ssd" | "hdd" | "virtual";

export interface Disk {
  readonly name: string;
  readonly model: string;
  readonly sizeBytes: number;
  readonly kind: DiskKind;
  readonly labels: readonly string[];
  readonly mounts: readonly string[];
}

export interface Filesystem {
  readonly type: string;
  readonly label: string;
  readonly profile: string;
  readonly disks: readonly string[];
}

export interface Hardware {
  readonly cpu: {
    readonly model: string;
    readonly sockets: number;
    readonly cores: number;
    readonly threads: number;
  };
  readonly memTotalBytes: number;
  readonly disks: readonly Disk[];
  readonly filesystems: readonly Filesystem[];
}

const KINDS: readonly DiskKind[] = ["nvme", "ssd", "hdd", "virtual"];
const KIND_SHORT: Record<DiskKind, string> = {
  nvme: "NVMe",
  ssd: "SSD",
  hdd: "HDD",
  virtual: "Virtual",
};
const KIND_LONG: Record<DiskKind, string> = {
  nvme: "NVMe SSD",
  ssd: "SATA/SAS SSD",
  hdd: "HDD (rotational)",
  virtual: "Virtual disk",
};

function obj(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function parseDisk(value: unknown): Disk[] {
  const r = obj(value);
  if (r === undefined) return [];
  const { name, model, kind } = r;
  const sizeBytes = count(r["size_bytes"]);
  if (typeof name !== "string" || sizeBytes === undefined) return [];
  return [
    {
      name,
      model: typeof model === "string" ? model : "",
      sizeBytes,
      kind: KINDS.find((k) => k === kind) ?? "ssd",
      labels: strings(r["labels"]),
      mounts: strings(r["mounts"]),
    },
  ];
}

function parseFilesystem(value: unknown): Filesystem[] {
  const r = obj(value);
  if (r === undefined) return [];
  const { type, label, profile } = r;
  if (typeof type !== "string") return [];
  return [
    {
      type,
      label: typeof label === "string" ? label : "",
      profile: typeof profile === "string" ? profile : "",
      disks: strings(r["disks"]),
    },
  ];
}

/** The host's hardware block, or undefined when absent or malformed. */
export function parseHardware(value: unknown): Hardware | undefined {
  const r = obj(value);
  const cpu = obj(r?.["cpu"]);
  if (r === undefined || cpu === undefined) return undefined;
  const sockets = count(cpu["sockets"]);
  const cores = count(cpu["cores"]);
  const threads = count(cpu["threads"]);
  const memTotalBytes = count(r["mem_total_bytes"]);
  const model = cpu["model"];
  if (
    sockets === undefined ||
    cores === undefined ||
    threads === undefined ||
    memTotalBytes === undefined ||
    typeof model !== "string"
  ) {
    return undefined;
  }
  const disks = Array.isArray(r["disks"]) ? (r["disks"] as unknown[]).flatMap(parseDisk) : [];
  const filesystems = Array.isArray(r["filesystems"])
    ? (r["filesystems"] as unknown[]).flatMap(parseFilesystem)
    : [];
  return { cpu: { model, sockets, cores, threads }, memTotalBytes, disks, filesystems };
}

/** Binary size with GB/TB labels, the way lsblk and the BIOS report disks. */
function size(value: number, precise = false): string {
  const g = value / 2 ** 30;
  if (g >= 1024) return `${(g / 1024).toFixed(1)} TB`;
  if (precise && !Number.isInteger(Math.round(g * 10) / 10)) return `${g.toFixed(1)} GB`;
  return `${String(Math.round(g))} GB`;
}

/** Installed RAM as people quote it: MemTotal minus firmware/kernel reservations, rounded up. */
function installedMemory(value: number): string {
  const g = value / 2 ** 30;
  const step = g > 16 ? 8 : 1;
  return `${String(Math.ceil(g / step) * step)} GB`;
}

function shortModel(model: string): string {
  const short = model
    .replace(/\((R|TM|tm)\)/g, "")
    .replace(/\s+@.*$/, "")
    .replace(/\s+\d+-Core Processor$/, "")
    .replace(/\s+(CPU|Processor)\b/g, "")
    .replace(/^(Intel|AMD)\s+/, "")
    .replace(/\s+/g, " ")
    .trim();
  return short === "" ? "CPU" : short;
}

function profileName(profile: string): string {
  return profile.startsWith("raid") ? profile.toUpperCase() : profile;
}

/** e.g. "2× Xeon Gold 6142 · 32C/64T · 126 GB · NVMe 932 GB + 2× HDD 1.8 TB (btrfs RAID1)". */
export function hardwareSummary(hw: Hardware): string {
  const { cpu } = hw;
  const parts = [
    `${cpu.sockets > 1 ? `${String(cpu.sockets)}× ` : ""}${shortModel(cpu.model)}`,
    `${String(cpu.cores)}C/${String(cpu.threads)}T`,
    installedMemory(hw.memTotalBytes),
  ];
  const groups = new Map<string, Disk[]>();
  for (const disk of hw.disks) {
    const key = `${KIND_SHORT[disk.kind]} ${size(disk.sizeBytes)}`;
    groups.set(key, [...(groups.get(key) ?? []), disk]);
  }
  const storage = [...groups].map(([key, disks]) => {
    const names = disks.map((d) => d.name);
    const fs = hw.filesystems.find(
      (f) => f.disks.length > 1 && f.profile !== "" && names.every((n) => f.disks.includes(n)),
    );
    const raid = fs === undefined ? "" : ` (${fs.type} ${profileName(fs.profile)})`;
    return `${disks.length > 1 ? `${String(disks.length)}× ` : ""}${key}${raid}`;
  });
  if (storage.length > 0) parts.push(storage.join(" + "));
  return parts.join(" · ");
}

/** One line per fact: full CPU model, memory, each disk with its roles, each filesystem. */
export function hardwareDetail(hw: Hardware): string[] {
  const { cpu } = hw;
  const lines = [
    `CPU: ${cpu.model || "unknown"} (${String(cpu.sockets)} socket${cpu.sockets === 1 ? "" : "s"}, ${String(cpu.cores)} cores, ${String(cpu.threads)} threads)`,
    `Memory: ${(hw.memTotalBytes / 2 ** 30).toFixed(1)} GB usable`,
  ];
  for (const d of hw.disks) {
    const facts = [KIND_LONG[d.kind], d.model, size(d.sizeBytes, true)].filter((f) => f !== "");
    if (d.labels.length > 0) facts.push(`label ${d.labels.join(", ")}`);
    if (d.mounts.length > 0) facts.push(`mounts ${d.mounts.join(", ")}`);
    lines.push(`${d.name}: ${facts.join(", ")}`);
  }
  for (const f of hw.filesystems) {
    const name = f.label === "" ? f.type : `${f.type} ${f.label}`;
    lines.push(
      `${name}: ${profileName(f.profile) || "unknown profile"} across ${f.disks.join(", ")}`,
    );
  }
  return lines;
}
