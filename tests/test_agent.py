from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from forge_dash.models import IngestSample
from forge_metrics_agent import (
    AttestationScanner,
    Proc,
    Task,
    count_cpus,
    current_runner_jobs,
    describe_failure,
    is_physical,
    parse_cpu,
    parse_disk,
    parse_meminfo,
    parse_net,
    parse_task,
    rate,
    read_host_id,
    read_runner_jobs,
    short_command,
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


def test_bottleneck_samples_separate_cpu_pressure_and_disks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_proc(tmp_path, STAT)
    pressure = tmp_path / "pressure"
    pressure.mkdir()
    _ = (pressure / "cpu").write_text("some avg10=2.50 avg60=1.00 avg300=0.00 total=120\n")
    _ = (pressure / "io").write_text(
        """some avg10=28.00 avg60=10.00 avg300=0.00 total=460
full avg10=20.00 avg60=9.00 avg300=0.00 total=330
"""
    )

    times = iter((100.0, 105.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(times, 105.0))
    proc = Proc(tmp_path)
    previous, _ = proc.counters()
    _ = (tmp_path / "stat").write_text("cpu 200 0 100 850 100 0 0 50\n")
    _ = (tmp_path / "diskstats").write_text("8 0 sda 105 0 20 100 55 0 35 300 0 2500 6000\n")
    sample, _ = proc.sample(previous)
    assert sample["diagnostics"] == {
        "cpu_on_pct": 50.0,
        "cpu_iowait_pct": 16.67,
        "cpu_steal_pct": 16.67,
        "psi_cpu_some_pct": 2.5,
        "psi_io_some_pct": 28.0,
        "psi_io_full_pct": 20.0,
        "devices": {
            "sda": {
                "busy_pct": 50.0,
                "queue_depth": 1.2,
                "await_ms": 40.0,
                "read_bps": 1024.0,
                "write_bps": 1536.0,
            }
        },
    }


def test_runner_slots_survive_short_dispatcher_stalls(tmp_path: Path) -> None:
    dispatcher = tmp_path / "host-load.json"
    dispatcher.write_text('{"gh_jobs_running":6,"updated_at":100,"cap":10}', encoding="utf-8")
    workers = tmp_path / "runner-workers.json"
    workers.write_text('{"gh_jobs_running":3,"updated_at":145}', encoding="utf-8")
    assert current_runner_jobs(tmp_path, 150) == {
        "gh_runner_slots": 10,
        "gh_runner_slots_updated_at": 100,
        "gh_jobs_running": 3,
        "gh_jobs_updated_at": 145,
    }
    assert current_runner_jobs(tmp_path, 401) == {}


def _task(root: Path, pid: int, ppid: int, comm: str, ticks: int, argv: list[str]) -> None:  # noqa: PLR0913, PLR0917
    d = root / str(pid)
    d.mkdir(exist_ok=True)
    fields = ["S", str(ppid), *["0"] * 9, str(ticks), "0", *["0"] * 6, "1000", "0", "0"]
    _ = (d / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
    _ = (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def test_parse_task_handles_spaces_in_comm() -> None:
    task = parse_task(7, "7 (a (b) c) S 3 0 0 0 0 0 0 0 0 0 11 4 0 0 0 0 0 0 99 0 0\n")
    assert task == Task(7, 3, "a (b) c", 15, 99)
    assert parse_task(7, "garbage") is None


def test_attestation_scanner_finds_requests_and_daemon_compiles(tmp_path: Path) -> None:
    worker = "/home/u/forge-ci-paired-worker-1/worker-attestation-abc-bin/soldr"
    scanner = AttestationScanner(tmp_path)
    assert scanner.scan(100, 20.0, 1.0) is None  # no server: nothing to report

    def write(clippy_ticks: int, rustc_ticks: int) -> None:
        _task(tmp_path, 10, 1, "soldr", 0, [worker, "remote", "serve", "--listen", "x"])
        _task(tmp_path, 11, 10, "soldr", clippy_ticks, [worker, "cargo", "clippy", "-p", "clud"])
        _task(tmp_path, 12, 10, "soldr-broker", 0, [])  # zombie broker: ignored
        _task(tmp_path, 20, 1, "soldr-daemon", 0, ["/x/soldr-daemon"])
        _task(
            tmp_path, 21, 20, "rustc", rustc_ticks, ["rustc", "/home/u/forge-ci-paired-worker-1/s"]
        )
        _task(tmp_path, 22, 20, "rustc", 500, ["rustc", "/home/other/src"])  # not attestation
        _task(tmp_path, 30, 1, "soldr", 0, ["/home/u/pilot/soldr", "remote", "serve"])

    write(0, 0)
    first = scanner.scan(100, 20.0, 1.0)
    assert first is not None
    assert first["running"] == 1
    assert first["cpu_pct"] == 0.0  # first sighting has no baseline
    write(30, 20)
    snap = scanner.scan(100, 20.0, 2.0)
    assert snap == {
        "running": 1,
        "cpu_pct": 50.0,
        "requests": [
            {
                "command": "soldr cargo clippy -p clud",
                "crate": "clud",
                "elapsed_s": 10.0,
                "cpu_pct": 30.0,
            }
        ],
        "observed_at": 2.0,
    }
    assert short_command(["/a/b", "x" * 300]).endswith("…")


def test_ingest_model_accepts_attestation() -> None:
    base = {
        "cpu_pct": 1.0,
        "cpu_count": 2,
        "load1": 0.0,
        "load5": 0.0,
        "load15": 0.0,
        "mem_total_bytes": 1,
        "mem_used_bytes": 0,
        "swap_total_bytes": 0,
        "swap_used_bytes": 0,
        "net_rx_bps": 0.0,
        "net_tx_bps": 0.0,
        "disk_read_bps": 0.0,
        "disk_write_bps": 0.0,
        "uptime_s": 1.0,
    }
    request = {"command": "soldr test -p clud", "crate": "clud", "elapsed_s": 3.0, "cpu_pct": 9.0}
    attestation = {
        "running": 1,
        "cpu_pct": 50.0,
        "observed_at": 2.0,
        "requests": [request],
    }
    sample = IngestSample.model_validate({**base, "attestation": attestation})
    assert sample.attestation is not None
    assert sample.attestation.requests[0].crate == "clud"
    with pytest.raises(ValueError, match="crate"):
        _ = IngestSample.model_validate(
            {
                **base,
                "attestation": {
                    **attestation,
                    "requests": [{**request, "crate": "a b"}],
                },
            }
        )


def test_host_identity_comes_from_the_host_hostname(tmp_path: Path) -> None:
    hostname = tmp_path / "hostname"
    hostname.write_text("dell-fsx3mn2\n")
    assert read_host_id(hostname) == "dell-fsx3mn2"
    assert read_host_id(tmp_path / "missing") is None
    (tmp_path / "blank").write_text("\n")
    assert read_host_id(tmp_path / "blank") is None


def test_ingest_failures_have_fixed_messages() -> None:
    assert "401" in describe_failure(401)
    assert "claimed host" in describe_failure(403)
    assert describe_failure(500) == "ingest failed: HTTP 500"
    assert describe_failure(0) == "ingest failed: connection error"
