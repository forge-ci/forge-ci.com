#!/usr/bin/env python3
"""forge-metrics-agent: push host utilization to forge-ci.com every few seconds.

Standard library only, so it runs on a bare ``python:3.12-alpine`` container.
It reads ``/proc`` and an optional runner snapshot. Run the container with ``--network host`` so
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

import contextlib
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import cast, final

SECTOR_BYTES = 512
# Virtual interfaces carry traffic that is already counted on a physical one.
# The kernel's answer (no /sys/class/net/<if>/device) wins; this name list is
# only the fallback when sysfs is unavailable.
VIRTUAL_IFACE = re.compile(r"^(lo|veth|docker|br-|virbr|cni|flannel|vxlan|tap|tun|wg|tailscale|fq)")
# Whole disks only: partitions, device-mapper, zram and loop would double count.
WHOLE_DISK = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+)$")
MAX_BACKOFF_S = 60.0
RUNNER_COUNT_MAX_AGE_S = 30
RUNNER_CAP_MAX_AGE_S = 300
MAX_RUNNER_SLOTS = 4096


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
    cpu_wait: int
    cpu_steal: int
    disks: dict[str, DiskCounters]


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


@dataclass(frozen=True)
class DiskCounters:
    read_bytes: int
    write_bytes: int
    reads: int
    writes: int
    request_ms: int
    busy_ms: int
    weighted_ms: int


def disk_counters(diskstats: str) -> dict[str, DiskCounters]:
    disks: dict[str, DiskCounters] = {}
    for line in diskstats.splitlines():
        v = line.split()
        if len(v) < 14 or not WHOLE_DISK.fullmatch(v[2]):  # noqa: PLR2004
            continue
        disks[v[2]] = DiskCounters(
            int(v[5]) * SECTOR_BYTES,
            int(v[9]) * SECTOR_BYTES,
            int(v[3]),
            int(v[7]),
            int(v[6]) + int(v[10]),
            int(v[12]),
            int(v[13]),
        )
    return disks


def parse_disk(diskstats: str) -> tuple[int, int]:
    disks = disk_counters(diskstats).values()
    return sum(d.read_bytes for d in disks), sum(d.write_bytes for d in disks)


def disk_sample(new: DiskCounters, old: DiskCounters, seconds: float) -> dict[str, float | None]:
    requests = new.reads + new.writes - old.reads - old.writes
    elapsed = new.request_ms - old.request_ms
    return {
        "busy_pct": round(min(100.0, rate(new.busy_ms, old.busy_ms, seconds) / 10), 2),
        "queue_depth": round(rate(new.weighted_ms, old.weighted_ms, seconds) / 1000, 3),
        "await_ms": round(elapsed / requests, 2) if requests > 0 and elapsed >= 0 else None,
        "read_bps": round(rate(new.read_bytes, old.read_bytes, seconds), 1),
        "write_bps": round(rate(new.write_bytes, old.write_bytes, seconds), 1),
    }


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
        disks = disk_counters(self.read("diskstats"))
        cpu = next(line.split()[1:] for line in stat.splitlines() if line.startswith("cpu "))
        wait, steal = (int(cpu[i]) if len(cpu) > i else 0 for i in (4, 7))
        return Counters(
            time.monotonic(),
            busy,
            total,
            rx,
            tx,
            sum(d.read_bytes for d in disks.values()),
            sum(d.write_bytes for d in disks.values()),
            wait,
            steal,
            disks,
        ), stat

    def diagnostics(self, cur: Counters, prev: Counters) -> dict[str, object]:
        seconds = cur.at - prev.at
        span = cur.cpu_total - prev.cpu_total
        steal = cur.cpu_steal - prev.cpu_steal
        values: dict[str, object] = {}
        if span > 0:
            for field, ticks in (
                ("cpu_on_pct", cur.cpu_busy - prev.cpu_busy - steal),
                ("cpu_iowait_pct", cur.cpu_wait - prev.cpu_wait),
                ("cpu_steal_pct", steal),
            ):
                if ticks >= 0:
                    values[field] = round(min(100.0, 100 * ticks / span), 2)
        for resource in ("cpu", "io", "memory"):
            with contextlib.suppress(OSError, ValueError):
                for line in self.read(f"pressure/{resource}").splitlines():
                    kind, *fields = line.split()
                    if kind == "some" or (kind == "full" and resource == "io"):
                        avg = next((value for value in fields if value.startswith("avg10=")), None)
                        if avg is not None:
                            values[f"psi_{resource}_{kind}_pct"] = float(avg.split("=", 1)[1])
        values["devices"] = {
            name: disk_sample(cur.disks[name], prev.disks[name], seconds)
            for name in sorted(cur.disks.keys() & prev.disks.keys())[:16]
        }
        return values

    def sample(self, prev: Counters) -> tuple[dict[str, object], Counters]:
        cur, stat = self.counters()
        dt = cur.at - prev.at
        cpu_span = cur.cpu_total - prev.cpu_total
        cpu_pct = 100.0 * (cur.cpu_busy - prev.cpu_busy) / cpu_span if cpu_span > 0 else 0.0
        mem = parse_meminfo(self.read("meminfo"))
        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        swap_total = mem.get("SwapTotal", 0)
        load = self.read("loadavg").split()
        values: dict[str, object] = {
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
            "diagnostics": self.diagnostics(cur, prev),
        }
        return values, cur


def read_runner_jobs(path: Path) -> dict[str, float | int]:
    """Read the dispatcher's last job observation, if available."""
    try:
        snapshot = cast("object", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    if not isinstance(snapshot, dict):
        return {}
    fields = cast("dict[str, object]", snapshot)
    count = fields.get("gh_jobs_running")
    observed = fields.get("updated_at")
    if (
        type(count) is int
        and 0 <= count <= 2**53 - 1
        and type(observed) in {int, float}
        and isinstance(observed, int | float)
        and 0 < observed <= time.time()
        and math.isfinite(observed)
    ):
        values: dict[str, float | int] = {"gh_jobs_running": count, "gh_jobs_updated_at": observed}
        cap = fields.get("cap")
        if type(cap) is int and 0 <= cap <= MAX_RUNNER_SLOTS:
            values.update(gh_runner_slots=cap, gh_runner_slots_updated_at=observed)
        return values
    return {}


def current_runner_jobs(directory: Path, now: float) -> dict[str, float | int]:
    """Prefer the dispatcher and fall back to the host process observer if it stalls."""
    dispatch = read_runner_jobs(directory / "host-load.json")
    values: dict[str, float | int] = {}
    cap = dispatch.get("gh_runner_slots")
    cap_at = dispatch.get("gh_runner_slots_updated_at")
    if cap is not None and cap_at is not None and 0 <= now - cap_at <= RUNNER_CAP_MAX_AGE_S:
        values.update(gh_runner_slots=cap, gh_runner_slots_updated_at=cap_at)
    for source in (dispatch, read_runner_jobs(directory / "runner-workers.json")):
        count = source.get("gh_jobs_running")
        observed = source.get("gh_jobs_updated_at")
        if (
            count is not None
            and observed is not None
            and 0 <= now - observed <= RUNNER_COUNT_MAX_AGE_S
        ):
            values.update(gh_jobs_running=count, gh_jobs_updated_at=observed)
            break
    return values


def post(url: str, token: str, values: dict[str, object]) -> int:
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
        values.update(current_runner_jobs(Path("/run/forge"), time.time()))
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
