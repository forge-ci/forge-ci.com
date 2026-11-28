import { describe, expect, it } from "vitest";

import { hardwareDetail, hardwareSummary, parseHardware } from "./hardware";

const SECTOR = 512;
const jw22 = {
  cpu: { model: "Intel(R) Xeon(R) Gold 6142 CPU @ 2.60GHz", sockets: 2, cores: 32, threads: 64 },
  mem_total_bytes: 131595264 * 1024,
  disks: [
    {
      name: "nvme0n1",
      model: "SPCC M.2 PCIe SSD",
      size_bytes: 1953525168 * SECTOR,
      kind: "nvme",
      labels: ["forge-build"],
      mounts: [],
    },
    {
      name: "sda",
      model: "ST2000NX0253",
      size_bytes: 3907029168 * SECTOR,
      kind: "hdd",
      labels: ["fleet", "ESP1"],
      mounts: ["/boot1"],
    },
    {
      name: "sdb",
      model: "ST2000NX0253",
      size_bytes: 3907029168 * SECTOR,
      kind: "hdd",
      labels: ["ESP0"],
      mounts: ["/", "/boot"],
    },
  ],
  filesystems: [{ type: "btrfs", label: "fleet", profile: "raid1", disks: ["sda", "sdb"] }],
};

describe("hardware", () => {
  it("summarises a host on one line", () => {
    const hw = parseHardware(jw22);
    expect(hw).toBeDefined();
    if (hw === undefined) return;
    expect(hardwareSummary(hw)).toBe(
      "2× Xeon Gold 6142 · 32C/64T · 128 GB · NVMe 932 GB + 2× HDD 1.8 TB (btrfs RAID1)",
    );
  });

  it("lists model strings, sizes and roles in the detail", () => {
    const hw = parseHardware(jw22);
    if (hw === undefined) throw new Error("unparsed");
    const detail = hardwareDetail(hw).join("\n");
    expect(detail).toContain("Intel(R) Xeon(R) Gold 6142 CPU @ 2.60GHz");
    expect(detail).toContain("nvme0n1: NVMe SSD, SPCC M.2 PCIe SSD, 931.5 GB, label forge-build");
    expect(detail).toContain("sdb: HDD (rotational), ST2000NX0253, 1.8 TB");
    expect(detail).toContain("btrfs fleet: RAID1 across sda, sdb");
  });

  it("handles a single-socket VM without disks", () => {
    const hw = parseHardware({
      cpu: { model: "DO-Regular", sockets: 1, cores: 2, threads: 2 },
      mem_total_bytes: 4 * 2 ** 30,
      disks: [
        {
          name: "vda",
          model: "",
          size_bytes: 80 * 2 ** 30,
          kind: "virtual",
          labels: [],
          mounts: ["/"],
        },
      ],
      filesystems: [],
    });
    if (hw === undefined) throw new Error("unparsed");
    expect(hardwareSummary(hw)).toBe("DO-Regular · 2C/2T · 4 GB · Virtual 80 GB");
  });

  it("rejects malformed payloads", () => {
    expect(parseHardware(null)).toBeUndefined();
    expect(parseHardware({ cpu: {} })).toBeUndefined();
  });
});
