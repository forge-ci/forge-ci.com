"""Static fleet inventory and environment-driven settings."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from forge_dash.models import HostInfo

_TOKEN_MAP = TypeAdapter(dict[str, str])

# The hosts the dashboard shows, in display order. Display order is also the
# chart colour order, so a host keeps its colour when another goes offline.
HOSTS: tuple[HostInfo, ...] = (
    HostInfo(
        id="dell-jw22zc3",
        label="E640",
        role="Dell VxRail E640 (PowerEdge R640): self-hosted GitHub Actions runner pool",
        location="on-prem",
    ),
    HostInfo(
        id="admin-droplet",
        label="Admin droplet",
        role="DigitalOcean droplet: signal.forge-ci.com, warm-pool spawner, /v1/pool",
        location="DigitalOcean nyc1",
    ),
)

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(ValueError):
    """Raised when the environment holds an invalid setting."""


def parse_token_hashes(raw: str) -> dict[str, str]:
    """Parse ``FORGE_INGEST_TOKEN_SHA256``: a JSON object of host id -> sha256 hex.

    Only hashes are configured on the server; the plaintext token lives on the host.
    """
    if not raw.strip():
        return {}
    try:
        data = _TOKEN_MAP.validate_json(raw)
    except ValidationError as exc:
        msg = "FORGE_INGEST_TOKEN_SHA256 must be a JSON object of host id -> hex string"
        raise ConfigError(msg) from exc
    known = {h.id for h in HOSTS}
    result: dict[str, str] = {}
    for host_id, digest in data.items():
        if host_id not in known:
            msg = f"FORGE_INGEST_TOKEN_SHA256 names unknown host {host_id!r}"
            raise ConfigError(msg)
        if not _SHA256_HEX.match(digest):
            msg = f"FORGE_INGEST_TOKEN_SHA256[{host_id!r}] must be 64 lowercase hex digits"
            raise ConfigError(msg)
        result[host_id] = digest
    return result


@dataclass(frozen=True)
class Settings:
    token_hashes: dict[str, str] = field(default_factory=dict)
    retention_s: int = 2 * 60 * 60
    online_after_s: float = 20.0
    min_interval_s: float = 1.0
    static_dir: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        static = os.environ.get("FORGE_STATIC_DIR", "frontend/dist")
        static_path = Path(static)
        return cls(
            token_hashes=parse_token_hashes(os.environ.get("FORGE_INGEST_TOKEN_SHA256", "")),
            static_dir=static_path if static_path.is_dir() else None,
        )
