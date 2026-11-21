"""FastAPI application: agent ingest, history, live stream and the built frontend."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import TYPE_CHECKING, Annotated, final

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from forge_dash.config import HOSTS, Settings
from forge_dash.models import History, HostStatus, IngestSample, Sample
from forge_dash.store import DROPPED, MetricsStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

MAX_INGEST_BYTES = 4096
MAX_SUBSCRIBERS = 500
KEEPALIVE_S = 15.0
HISTORY_MAX_S = 2 * 60 * 60

_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _host_for_token(token: str, token_hashes: dict[str, str]) -> str | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    match: str | None = None
    for host_id, expected in token_hashes.items():
        # Compare against every entry so timing does not reveal which host matched.
        if hmac.compare_digest(digest, expected):
            match = host_id
    return match


def _authenticate(authorization: str | None, token_hashes: dict[str, str]) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    host_id = _host_for_token(token, token_hashes) if scheme.lower() == "bearer" and token else None
    if host_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid ingest token")
    return host_id


async def _read_sample(request: Request) -> IngestSample:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_INGEST_BYTES):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "sample too large")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_INGEST_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "sample too large")
    try:
        values = IngestSample.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False)
        ) from exc
    if values.mem_used_bytes > values.mem_total_bytes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "mem_used > mem_total")
    return values


def _security_headers(response: Response, path: str) -> None:
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif not path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")


async def _sse_events(
    store: MetricsStore, queue: asyncio.Queue[str], request: Request
) -> AsyncIterator[str]:
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except TimeoutError:
                if await request.is_disconnected():
                    return
                yield ": keepalive\n\n"
                continue
            if message == DROPPED:
                yield "event: resync\ndata: {}\n\n"
                return
            yield f"event: sample\ndata: {message}\n\n"
    finally:
        store.unsubscribe(queue)


@final
class Dashboard:
    """Route handlers bound to one store, settings and clock."""

    def __init__(self, settings: Settings, clock: Callable[[], float]) -> None:
        self.settings = settings
        self.clock = clock
        self.store = MetricsStore((h.id for h in HOSTS), retention_s=settings.retention_s)

    async def security_headers(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        _security_headers(response, request.url.path)
        return response

    async def healthz(self) -> dict[str, bool | int]:
        return {"ok": True, "subscribers": self.store.subscriber_count}

    async def ingest(
        self,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        host_id = _authenticate(authorization, self.settings.token_hashes)
        values = await _read_sample(request)
        now = self.clock()
        previous = self.store.latest(host_id)
        if previous is not None and now - previous.ts < self.settings.min_interval_s:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "sampling too fast")
        sample = Sample.model_validate({**values.model_dump(), "ts": now})
        self.store.add(host_id, sample)
        self.store.publish(f'{{"host":"{host_id}","sample":{sample.model_dump_json()}}}')
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def hosts(self) -> list[HostStatus]:
        now = self.clock()
        out: list[HostStatus] = []
        for host in HOSTS:
            latest = self.store.latest(host.id)
            last_seen = latest.ts if latest else None
            online = last_seen is not None and now - last_seen <= self.settings.online_after_s
            out.append(HostStatus(host=host, online=online, last_seen=last_seen, latest=latest))
        return out

    async def history(
        self,
        seconds: Annotated[int, Query(ge=10, le=HISTORY_MAX_S)] = 900,
    ) -> History:
        now = self.clock()
        return History(
            now=now,
            seconds=seconds,
            hosts={h.id: self.store.history(h.id, now - seconds) for h in HOSTS},
        )

    async def stream(self, request: Request) -> StreamingResponse:
        if self.store.subscriber_count >= MAX_SUBSCRIBERS:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "too many viewers")
        queue = self.store.subscribe()
        return StreamingResponse(
            _sse_events(self.store, queue, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )


def create_app(
    settings: Settings | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[FastAPI, Dashboard]:
    """Build the ASGI app; also returns the handler object so tests can inspect its store."""
    dash = Dashboard(settings if settings is not None else Settings.from_env(), clock)
    app = FastAPI(title="forge-ci.com", docs_url=None, redoc_url=None, openapi_url=None)
    app.middleware("http")(dash.security_headers)
    app.get("/healthz")(dash.healthz)
    app.post("/api/ingest", status_code=status.HTTP_204_NO_CONTENT)(dash.ingest)
    app.get("/api/hosts")(dash.hosts)
    app.get("/api/history")(dash.history)
    app.get("/api/stream")(dash.stream)
    if dash.settings.static_dir is not None:
        app.mount("/", StaticFiles(directory=dash.settings.static_dir, html=True), name="frontend")
    return app, dash


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory``."""
    app, _ = create_app()
    return app
