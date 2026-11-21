from __future__ import annotations

import hashlib
import json
from typing import final

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from forge_dash.app import MAX_INGEST_BYTES, create_app
from forge_dash.config import ConfigError, Settings, parse_token_hashes
from forge_dash.models import History, HostStatus

_HOSTS = TypeAdapter(list[HostStatus])

TOKEN = "e640-test-token"  # noqa: S105 - test fixture
DROPLET_TOKEN = "droplet-test-token"  # noqa: S105


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sample(**overrides: float) -> dict[str, float]:
    base: dict[str, float] = {
        "cpu_pct": 42.5,
        "cpu_count": 64,
        "load1": 12.0,
        "load5": 10.0,
        "load15": 8.0,
        "mem_total_bytes": 128 * 2**30,
        "mem_used_bytes": 70 * 2**30,
        "swap_total_bytes": 0,
        "swap_used_bytes": 0,
        "net_rx_bps": 1000.0,
        "net_tx_bps": 2000.0,
        "disk_read_bps": 0.0,
        "disk_write_bps": 4096.0,
        "uptime_s": 3600.0,
    }
    base.update(overrides)
    return base


@final
class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(clock: Clock) -> TestClient:
    settings = Settings(
        token_hashes={"dell-jw22zc3": _hash(TOKEN), "admin-droplet": _hash(DROPLET_TOKEN)}
    )
    app, _ = create_app(settings, clock=clock)
    return TestClient(app)


def post(client: TestClient, token: str | None, body: object) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/ingest", content=json.dumps(body), headers=headers).status_code


def host_map(client: TestClient) -> dict[str, HostStatus]:
    return {h.host.id: h for h in _HOSTS.validate_json(client.get("/api/hosts").content)}


def test_ingest_requires_a_known_token(client: TestClient) -> None:
    assert post(client, None, sample()) == 401
    assert post(client, "wrong", sample()) == 401
    assert post(client, TOKEN, sample()) == 204


def test_token_decides_the_host(client: TestClient, clock: Clock) -> None:
    assert post(client, DROPLET_TOKEN, sample(cpu_pct=7.0)) == 204
    hosts = host_map(client)
    droplet = hosts["admin-droplet"]
    assert droplet.online
    assert droplet.latest is not None
    assert droplet.latest.cpu_pct == 7.0
    assert droplet.latest.ts == clock.now
    assert not hosts["dell-jw22zc3"].online
    assert hosts["dell-jw22zc3"].latest is None


def test_host_goes_offline_after_silence(client: TestClient, clock: Clock) -> None:
    assert post(client, TOKEN, sample()) == 204
    clock.now += 21
    e640 = host_map(client)["dell-jw22zc3"]
    assert not e640.online
    assert e640.last_seen == clock.now - 21


def test_rejects_invalid_samples(client: TestClient) -> None:
    assert post(client, TOKEN, sample(cpu_pct=101.0)) == 422
    assert post(client, TOKEN, {**sample(), "extra": 1}) == 422
    assert post(client, TOKEN, sample(mem_used_bytes=2**40, mem_total_bytes=2**30)) == 422
    response = client.post(
        "/api/ingest",
        content=b"{" + b" " * MAX_INGEST_BYTES + b"}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 413


def test_rate_limits_a_chatty_agent(client: TestClient, clock: Clock) -> None:
    assert post(client, TOKEN, sample()) == 204
    clock.now += 0.5
    assert post(client, TOKEN, sample()) == 429
    clock.now += 1
    assert post(client, TOKEN, sample()) == 204


def test_history_is_columnar_and_windowed(client: TestClient, clock: Clock) -> None:
    for cpu in (10.0, 20.0, 30.0):
        assert post(client, TOKEN, sample(cpu_pct=cpu)) == 204
        clock.now += 5
    body = History.model_validate_json(client.get("/api/history", params={"seconds": 12}).content)
    e640 = body.hosts["dell-jw22zc3"]
    assert e640.columns["cpu_pct"] == [20.0, 30.0]
    assert len(e640.ts) == 2
    assert body.hosts["admin-droplet"].ts == []
    assert client.get("/api/history", params={"seconds": 5}).status_code == 422


def test_retention_drops_old_samples(clock: Clock) -> None:
    settings = Settings(token_hashes={"dell-jw22zc3": _hash(TOKEN)}, retention_s=10)
    app, dash = create_app(settings, clock=clock)
    client = TestClient(app)
    for _ in range(4):
        assert post(client, TOKEN, sample()) == 204
        clock.now += 5
    assert len(dash.store.history("dell-jw22zc3", 0).ts) == 3


def test_security_headers(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_token_hash_config_is_validated() -> None:
    assert parse_token_hashes("") == {}
    assert parse_token_hashes(json.dumps({"dell-jw22zc3": "a" * 64})) == {"dell-jw22zc3": "a" * 64}
    with pytest.raises(ConfigError):
        parse_token_hashes(json.dumps({"someone-else": "a" * 64}))
    with pytest.raises(ConfigError):
        parse_token_hashes(json.dumps({"dell-jw22zc3": "not-hex"}))
    with pytest.raises(ConfigError):
        parse_token_hashes("[1]")
