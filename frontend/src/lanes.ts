// Dispatcher lanes on a runner host: configured max slots and slots running a job.

export interface RunnerLane {
  readonly name: string;
  readonly max: number;
  readonly busy: number;
}

function slots(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

/** Valid lanes from a sample's `gh_lanes`, or undefined when absent. */
export function parseLanes(value: unknown): RunnerLane[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.flatMap((item: unknown): RunnerLane[] => {
    if (typeof item !== "object" || item === null) return [];
    const { name, max, busy } = item as Record<string, unknown>;
    return typeof name === "string" && slots(max) && slots(busy) ? [{ name, max, busy }] : [];
  });
}

/** e.g. "obs-rust 4/6 · forge-ci 0/6 · host 4/12". */
export function laneBreakdown(
  lanes: readonly RunnerLane[],
  running: number | null,
  capacity: number | null,
): string {
  const parts = lanes.map((l) => `${l.name} ${String(l.busy)}/${String(l.max)}`);
  const run = running === null ? "—" : String(running);
  parts.push(`host ${run}/${capacity === null ? "—" : String(capacity)}`);
  return parts.join(" · ");
}
