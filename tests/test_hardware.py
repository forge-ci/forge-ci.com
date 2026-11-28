from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from forge_dash.app import create_app
from forge_dash.config import Settings
from forge_dash.models import HostHardware, HostStatus
from forge_metrics_agent import HardwareReporter, collect_hardware, parse_cpuinfo

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "hw-test-token"  # noqa: S105 - test fixture
HOSTS = TypeAdapter(list[HostStatus])
SAMPLE: dict[str, float] = {
    "cpu_pct": 1.0,
    "cpu_count": 64,
    "load1": 1.0,
    "load5": 1.0,
    "load15": 1.0,
    "mem_total_bytes": 128 * 2**30,
    "mem_used_bytes": 2**30,
    "swap_total_bytes": 0,
    "swap_used_bytes": 0,
    "net_rx_bps": 0.0,
    "net_tx_bps": 0.0,
    "disk_read_bps": 0.0,
    "disk_write_bps": 0.0,
    "uptime_s": 10.0,
}
MOUNTS = """/dev/sdb2 / btrfs rw 0 0
/dev/sdb2 /var/lib/docker btrfs rw 0 0
/dev/sdb1 /boot vfat rw 0 0
/dev/loop0 /run/x squashfs ro 0 0
"""
MODEL = "Intel(R) Xeon(R) Gold 6142 CPU @ 2.60GHz"


def cpuinfo(sockets: int, cores: int, threads_per_core: int) -> str:
    blocks: list[str] = []
    n = 0
    for socket in range(sockets):
        for _ in range(threads_per_core):
            for core in range(cores):
                fields = (
                    ("processor", n),
                    ("model name", MODEL),
                    ("physical id", socket),
                    ("core id", core),
                    ("cpu cores", cores),
                )
                blocks.append("".join(f"{k}\t: {v}\n" for k, v in fields))
                n += 1
    return "\n".join(blocks)


def test_parse_cpuinfo_counts_sockets_cores_threads() -> None:
    cpu = parse_cpuinfo(cpuinfo(2, 16, 2))
    assert (cpu.sockets, cpu.cores, cpu.threads) == (2, 32, 64)
    assert cpu.model == MODEL


def test_parse_cpuinfo_without_topology_is_one_socket() -> None:
    text = "processor\t: 0\nmodel name\t: DO-Regular\n\nprocessor\t: 1\nmodel name\t: DO-Regular\n"
    cpu = parse_cpuinfo(text)
    assert (cpu.sockets, cpu.cores, cpu.threads) == (1, 2, 2)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text)


def disk(  # noqa: PLR0913, PLR0917
    sys: Path, name: str, sectors: int, rotational: int, model: str, parts: int = 0
) -> None:
    write(sys / "block" / name / "size", f"{sectors}\n")
    write(sys / "block" / name / "queue" / "rotational", f"{rotational}\n")
    if model:
        write(sys / "block" / name / "device" / "model", model + "   \n")
    for p in range(1, parts + 1):
        write(sys / "block" / name / f"{name}{p}" / "partition", f"{p}\n")


def fake_jw22(tmp_path: Path) -> tuple[Path, Path, Path]:
    proc, sys, labels = tmp_path / "proc", tmp_path / "sys", tmp_path / "labels"
    write(proc / "cpuinfo", cpuinfo(2, 16, 2))
    write(proc / "meminfo", "MemTotal:       131595264 kB\n")
    write(
        proc / "1" / "mounts",
        MOUNTS,
    )
    disk(sys, "nvme0n1", 1953525168, 0, "SPCC M.2 PCIe SSD")
    disk(sys, "sda", 3907029168, 1, "ST2000NX0253", parts=2)
    disk(sys, "sdb", 3907029168, 1, "ST2000NX0253", parts=2)
    disk(sys, "loop0", 34387560, 0, "")
    disk(sys, "zram0", 65797632, 0, "")
    disk(sys, "vdb", 992, 1, "")  # config drive
    fs = sys / "fs" / "btrfs" / "f7a1ca0e-235e-429a-8e42-da4fd5af5ec1"
    write(fs / "label", "fleet\n")
    for dev in ("sda2", "sdb2"):
        write(fs / "devices" / dev / "x", "")
    write(fs / "allocation" / "data" / "raid1" / "total_bytes", "1\n")
    write(fs / "allocation" / "data" / "total_bytes", "1\n")
    gate = sys / "fs" / "btrfs" / "9667d165-1ffe-4f74-9af4-9031c8dd318c"
    write(gate / "devices" / "loop0" / "x", "")
    write(gate / "allocation" / "data" / "single" / "total_bytes", "1\n")
    write(sys / "fs" / "btrfs" / "features" / "x", "")
    labels.mkdir()
    (labels / "forge-build").symlink_to("../../nvme0n1")
    (labels / "fleet").symlink_to("../../sda2")
    return proc, sys, labels


def test_collect_hardware_reads_disks_and_btrfs(tmp_path: Path) -> None:
    hw = collect_hardware(*fake_jw22(tmp_path))
    assert hw.mem_total_bytes == 131595264 * 1024
    assert hw.cpu.sockets == 2
    by_name = {d.name: d for d in hw.disks}
    assert sorted(by_name) == ["nvme0n1", "sda", "sdb"]
    nvme = by_name["nvme0n1"]
    assert (nvme.kind, nvme.model, nvme.size_bytes) == (
        "nvme",
        "SPCC M.2 PCIe SSD",
        1953525168 * 512,
    )
    assert nvme.labels == ("forge-build",)
    assert by_name["sda"].kind == "hdd"
    assert by_name["sda"].model == "ST2000NX0253"
    assert by_name["sda"].labels == ("fleet",)
    assert by_name["sdb"].mounts == ("/", "/boot", "/var/lib/docker")
    assert len(hw.filesystems) == 1
    fs = hw.filesystems[0]
    assert (fs.type, fs.label, fs.profile, fs.disks) == ("btrfs", "fleet", "raid1", ("sda", "sdb"))
    _ = HostHardware.model_validate(hw.to_wire())


def test_collect_hardware_survives_missing_sysfs(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    write(proc / "cpuinfo", cpuinfo(1, 2, 1))
    write(proc / "meminfo", "MemTotal: 4000000 kB\n")
    hw = collect_hardware(proc, tmp_path / "nosys", tmp_path / "nolabels")
    assert hw.disks == ()
    assert hw.filesystems == ()
    _ = HostHardware.model_validate(hw.to_wire())


def test_reporter_sends_until_acknowledged_then_periodically(tmp_path: Path) -> None:
    reporter = HardwareReporter(*fake_jw22(tmp_path), every_s=300)
    assert reporter.due(0.0) is not None
    assert reporter.due(10.0) is not None  # not acknowledged yet: keep sending
    reporter.sent(10.0)
    assert reporter.due(100.0) is None
    assert reporter.due(311.0) is not None


def test_backend_keeps_hardware_across_plain_samples(tmp_path: Path) -> None:
    now = [1_000_000.0]
    settings = Settings(token_hashes={"dell-jw22zc3": hashlib.sha256(TOKEN.encode()).hexdigest()})
    app, _ = create_app(settings, clock=lambda: now[0])
    client = TestClient(app)
    hw = collect_hardware(*fake_jw22(tmp_path)).to_wire()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    first = json.dumps({**SAMPLE, "hardware": hw})
    assert client.post("/api/ingest", content=first, headers=headers).status_code == 204
    now[0] += 10
    plain = json.dumps(SAMPLE)
    assert client.post("/api/ingest", content=plain, headers=headers).status_code == 204
    hosts = {h.host.id: h for h in HOSTS.validate_json(client.get("/api/hosts").content)}
    got = hosts["dell-jw22zc3"].hardware
    assert got is not None
    assert got.cpu.threads == 64
    assert got.disks[0].name == "nvme0n1"
    latest = hosts["dell-jw22zc3"].latest
    assert latest is not None
    assert latest.hardware is None
    assert hosts["admin-droplet"].hardware is None
