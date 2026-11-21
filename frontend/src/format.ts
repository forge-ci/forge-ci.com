const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** Decimal byte size (1 KB = 1000 B), as network and disk tools report rates. */
export function bytes(value: number, digits = 1): string {
  let v = value;
  let unit = 0;
  while (Math.abs(v) >= 1000 && unit < BYTE_UNITS.length - 1) {
    v /= 1000;
    unit += 1;
  }
  return `${v.toFixed(unit === 0 ? 0 : digits)} ${BYTE_UNITS[unit] ?? "B"}`;
}

export function rate(value: number): string {
  return `${bytes(value)}/s`;
}

export function gib(value: number): string {
  return `${(value / 2 ** 30).toFixed(value >= 100 * 2 ** 30 ? 0 : 1)} GiB`;
}

export function pct(value: number): string {
  return `${value.toFixed(value >= 10 ? 0 : 1)}%`;
}

export function ratio(value: number): string {
  return value.toFixed(2);
}

export function duration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${String(s)}s`;
  if (s < 3600) return `${String(Math.floor(s / 60))}m`;
  if (s < 86400) return `${String(Math.floor(s / 3600))}h ${String(Math.floor((s % 3600) / 60))}m`;
  return `${String(Math.floor(s / 86400))}d ${String(Math.floor((s % 86400) / 3600))}h`;
}

export function clock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
