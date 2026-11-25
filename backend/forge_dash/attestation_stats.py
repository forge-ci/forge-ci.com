"""Read the independently deployed Soldr attestation statistics service."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import httpx2
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, NonNegativeInt, ValidationError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from forge_dash.browser_login import BrowserLogin

_MAX_BODY = 8192
_PRIVATE = {"Cache-Control": "private, no-store"}


class Distribution(BaseModel):
    p50: NonNegativeInt | None
    p95: NonNegativeInt | None


class Stats(BaseModel):
    version: Literal[1]
    start_unix_ms: NonNegativeInt
    end_unix_ms: NonNegativeInt
    completed: NonNegativeInt
    succeeded: NonNegativeInt
    failed: NonNegativeInt
    worker_ms: Distribution
    command_ms: Distribution
    publication_ms: Distribution


def valid_origin(origin: str) -> bool:
    try:
        url = urlsplit(origin)
        port = url.port
    except ValueError:
        return False
    return (
        url.scheme == "https"
        and url.hostname is not None
        and port in (None, 443)
        and url.username is None
        and url.password is None
        and url.path in ("", "/")
        and not url.query
        and not url.fragment
    )


class ResponseTooLargeError(ValueError):
    """The stats response exceeds the proxy's byte limit."""


async def fetch_stats(origin: str, token: str) -> Stats:
    async with (
        asyncio.timeout(5),
        httpx2.AsyncClient(timeout=3, follow_redirects=False, trust_env=False) as client,
        client.stream(
            "GET", f"{origin}/v1/attestations/stats", headers={"Authorization": f"Bearer {token}"}
        ) as response,
    ):
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_BODY:
                raise ResponseTooLargeError
            body.extend(chunk)
    return Stats.model_validate_json(body)


def install_attestation_stats(app: FastAPI, browser: BrowserLogin) -> None:
    origin = os.environ.get("FORGE_ATTESTATION_STATS_ORIGIN", "").rstrip("/")
    token = os.environ.get("FORGE_ATTESTATION_STATS_READ_TOKEN", "")

    @app.get("/api/attestation-stats")
    async def stats(request: Request, response: Response) -> Stats:
        if not browser.signed_in(request):
            raise HTTPException(401, "sign in required", _PRIVATE)
        if not valid_origin(origin) or not token:
            raise HTTPException(503, "attestation statistics unavailable", _PRIVATE)
        try:
            result = await fetch_stats(origin, token)
        except (
            httpx2.HTTPError,
            httpx2.InvalidURL,
            TimeoutError,
            ValidationError,
            ResponseTooLargeError,
        ) as exc:
            raise HTTPException(503, "attestation statistics unavailable", _PRIVATE) from exc
        response.headers.update(_PRIVATE)
        return result
