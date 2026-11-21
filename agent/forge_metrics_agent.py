#!/usr/bin/env python3
"""forge-metrics-agent: push host utilization to forge-ci.com every few seconds.

Standard library only, so it runs on a bare ``python:3.12-alpine`` container.
It only reads ``/proc``. Run the container with ``--network host`` so
``/proc/net/dev`` shows the host's interfaces; ``/proc/stat``, ``meminfo``,
``loadavg``, ``diskstats`` and ``uptime`` are host-wide even without it.

Environment:
  FORGE_INGEST_URL         e.g. https://forge-ci-com.onrender.com/api/ingest
  FORGE_INGEST_TOKEN_FILE  file holding this host's bearer token (default /etc/forge-metrics/token)
  FORGE_INTERVAL_S         seconds between samples (default 5)
  FORGE_PROC_ROOT          /proc mount (default /proc)
  FORGE_SYS_NET            sysfs net dir used to tell physical NICs (default /sys/class/net)
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import final

SECTOR_BYTES = 512
# Virtual interfaces carry traffic that is already counted on a physical one.
# The kernel's answer (no /sys/class/net/<if>/device) wins; this name list is
# only the fallback when sysfs is unavailable.
VIRTUAL_IFACE = re.compile(r"^(lo|veth|docker|br-|virbr|cni|flannel|vxlan|tap|tun|wg|tailscale|fq)")
# Whole disks only: partitions, device-mapper, zram and loop would double count.
WHOLE_DISK = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+)$")
MAX_BACKOFF_S = 60.0


@dataclass(frozen=True)
class Counters:
    """Monotonic counters read at one instant; rates come from two of these."""

    at: float
    cpu_busy: int
    cpu_total: int
    net_rx: int
    net_tx: int
    disk_read: int
    disk_write: int


def parse_cpu(stat: str) -> tuple[int, int]:
    """Return (busy, total) jiffies from the aggregate ``cpu`` line of /proc/stat."""
    for line in stat.splitlines():
        if line.startswith("cpu "):
            fields = [int(v) for v in line.split()[1:]]
            # user nice system idle iowait irq softirq steal [guest guest_nice]
            # guest time is already included in user/nice, so stop at steal.
            core = fields[:8]
            idle = core[3] + (core[4] if len(core) > 4 else 0)  # noqa: PLR2004
            total = sum(core)
            return total - idle, total
    msg = "no aggregate cpu line in /proc/stat"
    raise ValueError(msg)


def count_cpus(stat: str) -> int:
    return sum(1 for line in stat.splitlines() if re.match(r"^cpu\d+ ", line)) or 1


def parse_meminfo(meminfo: str) -> dict[str, int]:
    """Return /proc/meminfo values in bytes."""
    out: dict[str, int] = {}
    for line in meminfo.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            scale = 1024 if len(parts) > 1 and parts[1] == "kB" else 1
            out[name] = int(parts[0]) * scale
    return out


def is_physical(name: str, sys_net: Path | None) -> bool:
    """True for a NIC backed by a device; bridges, veths and tunnels have none."""
    if sys_net is not None and sys_net.is_dir():
        return (sys_net / name / "device").exists()
    return not VIRTUAL_IFACE.match(name)


def parse_net(netdev: str, sys_net: Path | None = None) -> tuple[int, int]:
    """Sum rx/tx bytes over physical interfaces in /proc/net/dev."""
    rx = tx = 0
    for line in netdev.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not name or not is_physical(name, sys_net):
            continue
        fields = rest.split()
        rx += int(fields[0])
        tx += int(fields[8])
    return rx, tx


def parse_disk(diskstats: str) -> tuple[int, int]:
    """Sum bytes read/written over whole disks in /proc/diskstats."""
    read = write = 0
    for line in diskstats.splitlines():
        fields = line.split()
        if len(fields) < 10 or not WHOLE_DISK.match(fields[2]):  # noqa: PLR2004
            continue
        read += int(fields[5]) * SECTOR_BYTES
        write += int(fields[9]) * SECTOR_BYTES
    return read, write


def rate(new: int, old: int, seconds: float) -> float:
    """Per-second rate; a counter that went backwards (reset, wrap) reads as 0."""
    if seconds <= 0 or new < old:
        return 0.0
    return (new - old) / seconds


@final
class Proc:
    def __init__(self, root: Path, sys_net: Path | None = None) -> None:
        self.root = root
        self.sys_net = sys_net

    def read(self, name: str) -> str:
        return (self.root / name).read_text(encoding="ascii", errors="replace")

    def counters(self) -> tuple[Counters, str]:
        stat = self.read("stat")
        busy, total = parse_cpu(stat)
        rx, tx = parse_net(self.read("net/dev"), self.sys_net)
        dread, dwrite = parse_disk(self.read("diskstats"))
        return Counters(time.monotonic(), busy, total, rx, tx, dread, dwrite), stat

    def sample(self, prev: Counters) -> tuple[dict[str, float | int], Counters]:
        cur, stat = self.counters()
        dt = cur.at - prev.at
        cpu_span = cur.cpu_total - prev.cpu_total
        cpu_pct = 100.0 * (cur.cpu_busy - prev.cpu_busy) / cpu_span if cpu_span > 0 else 0.0
        mem = parse_meminfo(self.read("meminfo"))
        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        swap_total = mem.get("SwapTotal", 0)
        load = self.read("loadavg").split()
        values: dict[str, float | int] = {
            "cpu_pct": round(min(max(cpu_pct, 0.0), 100.0), 2),
            "cpu_count": count_cpus(stat),
            "load1": float(load[0]),
            "load5": float(load[1]),
            "load15": float(load[2]),
            "mem_total_bytes": mem_total,
            "mem_used_bytes": max(0, mem_total - mem_avail),
            "swap_total_bytes": swap_total,
            "swap_used_bytes": max(0, swap_total - mem.get("SwapFree", 0)),
            "net_rx_bps": round(rate(cur.net_rx, prev.net_rx, dt), 1),
            "net_tx_bps": round(rate(cur.net_tx, prev.net_tx, dt), 1),
            "disk_read_bps": round(rate(cur.disk_read, prev.disk_read, dt), 1),
            "disk_write_bps": round(rate(cur.disk_write, prev.disk_write, dt), 1),
            "uptime_s": float(self.read("uptime").split()[0]),
        }
        return values, cur


def post(url: str, token: str, values: dict[str, float | int]) -> int:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        msg = f"not an https URL: {url}"
        raise ValueError(msg)
    connection = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=10)
    try:
        connection.request(
            "POST",
            parts.path or "/",
            body=json.dumps(values).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "forge-metrics-agent/1",
            },
        )
        response = connection.getresponse()
        _ = response.read()
        return response.status
    finally:
        connection.close()


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}", file=sys.stderr)


def main() -> int:
    url = os.environ.get("FORGE_INGEST_URL", "")
    if not url.startswith("https://"):
        log("FORGE_INGEST_URL must be an https:// URL")
        return 2
    token_file = Path(os.environ.get("FORGE_INGEST_TOKEN_FILE", "/etc/forge-metrics/token"))
    token = token_file.read_text(encoding="ascii").strip()
    interval = float(os.environ.get("FORGE_INTERVAL_S", "5"))
    proc = Proc(
        Path(os.environ.get("FORGE_PROC_ROOT", "/proc")),
        Path(os.environ.get("FORGE_SYS_NET", "/sys/class/net")),
    )

    prev, _ = proc.counters()
    backoff = interval
    failing = False
    log(f"pushing to {url} every {interval:g}s")
    while True:
        time.sleep(backoff)
        values, prev = proc.sample(prev)
        try:
            code = post(url, token, values)
        except (OSError, http.client.HTTPException) as exc:
            code = 0
            detail = str(exc)
        else:
            detail = f"HTTP {code}"
        if code == 401:  # noqa: PLR2004 - a bad token never heals by retrying fast
            log("ingest rejected the token (401); check the token file")
            backoff = MAX_BACKOFF_S
        elif 200 <= code < 300:  # noqa: PLR2004
            if failing:
                log("ingest recovered")
            failing = False
            backoff = interval
        else:
            if not failing:
                log(f"ingest failed: {detail}; backing off")
            failing = True
            backoff = min(MAX_BACKOFF_S, backoff * 2)


if __name__ == "__main__":
    sys.exit(main())
