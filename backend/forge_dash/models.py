"""Wire models shared by the ingest endpoint, the store and the browser API."""

from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

# Field names of a sample, in the order the columnar history API emits them.
METRIC_FIELDS: tuple[str, ...] = (
    "cpu_pct",
    "cpu_count",
    "load1",
    "load5",
    "load15",
    "mem_total_bytes",
    "mem_used_bytes",
    "swap_total_bytes",
    "swap_used_bytes",
    "net_rx_bps",
    "net_tx_bps",
    "disk_read_bps",
    "disk_write_bps",
    "uptime_s",
)

_MAX_BYTES = 1 << 50  # 1 PiB: anything larger is a broken agent, not a real host
_MAX_RATE = 1e12  # 1 TB/s


Percent = Annotated[float, Field(ge=0, le=100)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
DeviceName = Annotated[str, Field(pattern=r"^[a-z0-9]+$", max_length=32)]


class DiskDiagnostics(BaseModel):
    busy_pct: Percent
    queue_depth: Nonnegative
    await_ms: Nonnegative | None
    read_bps: Nonnegative
    write_bps: Nonnegative


class Diagnostics(BaseModel):
    cpu_on_pct: Percent | None = None
    cpu_iowait_pct: Percent | None = None
    cpu_steal_pct: Percent | None = None
    psi_cpu_some_pct: Percent | None = None
    psi_io_some_pct: Percent | None = None
    psi_io_full_pct: Percent | None = None
    psi_memory_some_pct: Percent | None = None
    devices: dict[DeviceName, DiskDiagnostics] = Field(default_factory=dict, max_length=16)


class AttestationRequest(BaseModel):
    """One soldr paired-worker request (e.g. ``soldr cargo clippy -p clud``) running on a host."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    command: str = Field(max_length=200)
    crate: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    elapsed_s: Nonnegative
    cpu_pct: Percent


class Attestation(BaseModel):
    """Attestation work on a host: requests under ``worker-attestation`` soldr servers."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    running: int = Field(ge=0, le=4096)
    cpu_pct: Percent
    requests: list[AttestationRequest] = Field(default_factory=list, max_length=16)
    observed_at: float = Field(gt=0, allow_inf_nan=False)


class IngestSample(BaseModel):
    """One utilization sample as an agent posts it. The server stamps the time."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    cpu_pct: float = Field(ge=0, le=100)
    cpu_count: int = Field(ge=1, le=4096)
    load1: float = Field(ge=0, le=1e5)
    load5: float = Field(ge=0, le=1e5)
    load15: float = Field(ge=0, le=1e5)
    mem_total_bytes: int = Field(gt=0, le=_MAX_BYTES)
    mem_used_bytes: int = Field(ge=0, le=_MAX_BYTES)
    swap_total_bytes: int = Field(ge=0, le=_MAX_BYTES)
    swap_used_bytes: int = Field(ge=0, le=_MAX_BYTES)
    net_rx_bps: float = Field(ge=0, le=_MAX_RATE)
    net_tx_bps: float = Field(ge=0, le=_MAX_RATE)
    disk_read_bps: float = Field(ge=0, le=_MAX_RATE)
    disk_write_bps: float = Field(ge=0, le=_MAX_RATE)
    uptime_s: float = Field(ge=0, le=1e10)
    diagnostics: Diagnostics | None = None
    gh_runner_slots: int | None = Field(default=None, strict=True, ge=0, le=4096)
    gh_runner_slots_updated_at: float | None = Field(
        default=None, strict=True, gt=0, allow_inf_nan=False
    )
    gh_jobs_running: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    gh_jobs_updated_at: float | None = Field(default=None, strict=True, gt=0, allow_inf_nan=False)
    attestation: Attestation | None = None


class Sample(IngestSample):
    """A stored sample: the agent's values plus the server receive time (unix seconds)."""

    ts: float

    def metrics(self) -> tuple[float, ...]:
        """Metric values in ``METRIC_FIELDS`` order."""
        return (
            self.cpu_pct,
            float(self.cpu_count),
            self.load1,
            self.load5,
            self.load15,
            float(self.mem_total_bytes),
            float(self.mem_used_bytes),
            float(self.swap_total_bytes),
            float(self.swap_used_bytes),
            self.net_rx_bps,
            self.net_tx_bps,
            self.disk_read_bps,
            self.disk_write_bps,
            self.uptime_s,
        )


class HostInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: str
    label: str
    role: str
    location: str


class HostStatus(BaseModel):
    host: HostInfo
    online: bool
    last_seen: float | None
    latest: Sample | None


class SeriesColumns(BaseModel):
    """Columnar history for one host: ``ts`` plus one list per metric field."""

    ts: list[float]
    columns: dict[str, list[float]]
    diagnostics: list[Diagnostics | None] = Field(default_factory=list)


class History(BaseModel):
    now: float
    seconds: int
    hosts: dict[str, SeriesColumns]
