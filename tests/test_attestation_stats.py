"""Contracts for the authenticated Forge CI attestation stats proxy."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import httpx2
import pytest
from fastapi.testclient import TestClient

from forge_dash.app import create_app
from forge_dash.attestation_stats import Distribution, Stats
from forge_dash.browser_login import BrowserLogin

if TYPE_CHECKING:
    from fastapi import Request


def signed_in(_browser: BrowserLogin, _request: Request) -> bool:
    return True


def signed_out(_browser: BrowserLogin, _request: Request) -> bool:
    return False


def test_attestation_stats_require_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BrowserLogin, "signed_in", signed_out)
    app, _ = create_app()
    assert TestClient(app).get("/api/attestation-stats").status_code == 401


def test_unconfigured_attestation_service_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BrowserLogin, "signed_in", signed_in)
    monkeypatch.delenv("FORGE_ATTESTATION_STATS_ORIGIN", raising=False)
    app, _ = create_app()
    assert TestClient(app).get("/api/attestation-stats").status_code == 503


@pytest.mark.parametrize("completed", [0, 2])
def test_attestation_stats_proxies_empty_and_populated_windows(
    monkeypatch: pytest.MonkeyPatch, completed: int
) -> None:
    monkeypatch.setattr(BrowserLogin, "signed_in", signed_in)
    monkeypatch.setenv("FORGE_ATTESTATION_STATS_ORIGIN", "https://stats.example.test")
    credential = secrets.token_urlsafe(32)
    monkeypatch.setenv("FORGE_ATTESTATION_STATS_READ_TOKEN", credential)
    distribution = Distribution(p50=10, p95=20) if completed else Distribution(p50=None, p95=None)
    expected = Stats(
        version=1,
        start_unix_ms=100,
        end_unix_ms=200,
        completed=completed,
        succeeded=completed,
        failed=0,
        worker_ms=distribution,
        command_ms=distribution,
        publication_ms=distribution,
    )

    def upstream(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://stats.example.test/v1/attestations/stats"
        assert request.headers["Authorization"] == f"Bearer {credential}"
        return httpx2.Response(200, json=expected.model_dump())

    async_client = httpx2.AsyncClient

    def client(*, timeout: float, follow_redirects: bool, trust_env: bool) -> httpx2.AsyncClient:
        assert not follow_redirects
        assert not trust_env
        return async_client(timeout=timeout, transport=httpx2.MockTransport(upstream))

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    app, _ = create_app()
    response = TestClient(app).get("/api/attestation-stats")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == expected.model_dump()


@pytest.mark.parametrize("error", [httpx2.ConnectError, httpx2.InvalidURL])
def test_attestation_stats_upstream_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: type[httpx2.ConnectError | httpx2.InvalidURL]
) -> None:
    monkeypatch.setattr(BrowserLogin, "signed_in", signed_in)
    monkeypatch.setenv("FORGE_ATTESTATION_STATS_ORIGIN", "https://stats.example.test")
    monkeypatch.setenv("FORGE_ATTESTATION_STATS_READ_TOKEN", "test-read-token")

    async def upstream(_origin: str, _token: str) -> Stats:
        message = "offline"
        raise error(message)

    monkeypatch.setattr("forge_dash.attestation_stats.fetch_stats", upstream)
    app, _ = create_app()
    assert TestClient(app).get("/api/attestation-stats").status_code == 503
