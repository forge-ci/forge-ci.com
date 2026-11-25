"""Wire models shared by the ingest endpoint, the store and the browser API."""

from __future__ import annotations

from typing import ClassVar

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
    gh_jobs_running: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    gh_jobs_updated_at: float | None = Field(default=None, strict=True, gt=0, allow_inf_nan=False)


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


class History(BaseModel):
    now: float
    seconds: int
    hosts: dict[str, SeriesColumns]
