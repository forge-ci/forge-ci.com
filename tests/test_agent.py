from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge_metrics_agent import (
    Proc,
    count_cpus,
    current_runner_jobs,
    is_physical,
    parse_cpu,
    parse_disk,
    parse_meminfo,
    parse_net,
    rate,
    read_runner_jobs,
)

if TYPE_CHECKING:
    from pathlib import Path

STAT = """cpu  100 0 50 800 50 0 0 0 0 0
cpu0 50 0 25 400 25 0 0 0 0 0
cpu1 50 0 25 400 25 0 0 0 0 0
intr 1
"""
STAT2 = """cpu  200 0 100 850 50 0 0 0 0 0
cpu0 100 0 50 425 25 0 0 0 0 0
cpu1 100 0 50 425 25 0 0 0 0 0
"""
NETDEV = """Inter-|   Receive                                                |  Transmit
 face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo
    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0
  eno1: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0
docker0: 555 1 0 0 0 0 0 0 555 1 0 0 0 0 0 0
vethab12: 777 1 0 0 0 0 0 0 777 1 0 0 0 0 0 0
  eth1: 1 1 0 0 0 0 0 0 2 1 0 0 0 0 0 0
"""
DISKSTATS = """   8       0 sda 100 0 10 0 50 0 20 0 0 0 0
   8       1 sda1 100 0 10 0 50 0 20 0 0 0 0
 259       0 nvme0n1 1 0 2 0 1 0 4 0 0 0 0
 259       1 nvme0n1p1 1 0 2 0 1 0 4 0 0 0 0
 253       0 dm-0 9 0 999 0 9 0 999 0 0 0 0
   7       0 loop0 9 0 999 0 9 0 999 0 0 0 0
"""
MEMINFO = """MemTotal:       1000 kB
MemFree:         100 kB
MemAvailable:    400 kB
SwapTotal:       200 kB
SwapFree:        150 kB
HugePages_Total:       0
"""


def test_parse_cpu_counts_iowait_as_idle() -> None:
    assert parse_cpu(STAT) == (150, 1000)
    with pytest.raises(ValueError, match="cpu"):
        parse_cpu("intr 1\n")


def test_count_cpus() -> None:
    assert count_cpus(STAT) == 2
    assert count_cpus("cpu  1 2 3 4\n") == 1


def test_parse_net_skips_virtual_interfaces() -> None:
    assert parse_net(NETDEV) == (1001, 2002)


def test_sysfs_decides_physical_interfaces(tmp_path: Path) -> None:
    for name in ("eno1", "eth1"):
        (tmp_path / name / "device").mkdir(parents=True)
    for name in ("lo", "docker0", "vethab12", "fq0"):
        (tmp_path / name).mkdir()
    assert is_physical("eno1", tmp_path)
    assert not is_physical("fq0", tmp_path)
    assert parse_net(NETDEV, tmp_path) == (1001, 2002)
    assert not is_physical("fq0", None)  # name fallback without sysfs
    assert is_physical("eno1", tmp_path / "missing")  # no sysfs dir: name fallback


def test_parse_disk_counts_whole_disks_only() -> None:
    assert parse_disk(DISKSTATS) == ((10 + 2) * 512, (20 + 4) * 512)


def test_parse_meminfo_scales_kb() -> None:
    mem = parse_meminfo(MEMINFO)
    assert mem["MemTotal"] == 1000 * 1024
    assert mem["HugePages_Total"] == 0


def test_rate_handles_resets() -> None:
    assert rate(200, 100, 10) == 10.0
    assert rate(50, 100, 10) == 0.0
    assert rate(200, 100, 0) == 0.0


def _write_proc(root: Path, stat: str) -> None:
    (root / "net").mkdir(exist_ok=True)
    _ = (root / "stat").write_text(stat)
    _ = (root / "net" / "dev").write_text(NETDEV)
    _ = (root / "diskstats").write_text(DISKSTATS)
    _ = (root / "meminfo").write_text(MEMINFO)
    _ = (root / "loadavg").write_text("1.50 1.00 0.50 3/400 1234\n")
    _ = (root / "uptime").write_text("3600.25 7000.00\n")


def test_sample_end_to_end(tmp_path: Path) -> None:
    _write_proc(tmp_path, STAT)
    proc = Proc(tmp_path)
    prev, _ = proc.counters()
    _write_proc(tmp_path, STAT2)
    values, _ = proc.sample(prev)
    # busy 150 -> 300, total 1000 -> 1200: 150 / 200 = 75 %
    assert values["cpu_pct"] == 75.0
    assert values["cpu_count"] == 2
    assert values["load1"] == 1.5
    assert values["mem_used_bytes"] == 600 * 1024
    assert values["swap_used_bytes"] == 50 * 1024
    assert values["uptime_s"] == 3600.25
    assert values["net_rx_bps"] == 0.0  # counters unchanged between reads


def test_runner_snapshots(tmp_path: Path) -> None:
    for count in (2, 3, 0):
        path = tmp_path / str(count)
        path.write_text(f'{{"gh_jobs_running":{count},"updated_at":100}}', encoding="utf-8")
        assert read_runner_jobs(path) == {"gh_jobs_running": count, "gh_jobs_updated_at": 100}
    assert read_runner_jobs(tmp_path / "missing") == {}
    path.write_text('{"gh_jobs_running":true,"updated_at":100}', encoding="utf-8")
    assert read_runner_jobs(path) == {}


def test_runner_jobs_fall_back_to_host_workers(tmp_path: Path) -> None:
    dispatcher = tmp_path / "host-load.json"
    workers = tmp_path / "runner-workers.json"
    dispatcher.write_text('{"gh_jobs_running":7,"updated_at":100}', encoding="utf-8")
    workers.write_text('{"gh_jobs_running":0,"updated_at":132}', encoding="utf-8")
    assert current_runner_jobs(tmp_path, 133) == {"gh_jobs_running": 0, "gh_jobs_updated_at": 132}
    assert current_runner_jobs(tmp_path, 163) == {}
    dispatcher.write_text('{"gh_jobs_running":5,"updated_at":130}', encoding="utf-8")
    assert current_runner_jobs(tmp_path, 133) == {"gh_jobs_running": 5, "gh_jobs_updated_at": 130}
