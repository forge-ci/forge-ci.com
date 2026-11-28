import { describe, expect, it } from "vitest";

import { parseSample } from "./api";
import { laneBreakdown } from "./lanes";

const base = {
  ts: 1,
  cpu_pct: 1,
  cpu_count: 64,
  load1: 1,
  load5: 1,
  load15: 1,
  mem_total_bytes: 1,
  mem_used_bytes: 1,
  swap_total_bytes: 0,
  swap_used_bytes: 0,
  net_rx_bps: 0,
  net_tx_bps: 0,
  disk_read_bps: 0,
  disk_write_bps: 0,
  uptime_s: 1,
};

describe("lanes", () => {
  it("parses lanes and renders the breakdown with the host total", () => {
    const sample = parseSample({
      ...base,
      gh_lanes: [
        { name: "obs-rust", max: 6, busy: 4 },
        { name: "forge-ci", max: 6, busy: 0 },
        { name: "fastled", max: 2, busy: 0 },
        { name: "talos-hq", max: 2, busy: 0 },
        { name: 3, max: 1, busy: 0 },
      ],
    });
    expect(sample.runnerLanes?.length).toBe(4);
    expect(laneBreakdown(sample.runnerLanes ?? [], 4, 16)).toBe(
      "obs-rust 4/6 · forge-ci 0/6 · fastled 0/2 · talos-hq 0/2 · host 4/16",
    );
  });

  it("omits lanes when none are reported", () => {
    expect(parseSample(base).runnerLanes).toBeUndefined();
  });
});
