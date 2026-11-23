"""Short-lived Soldr device pairing requests."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, final
from urllib.parse import parse_qs

from fastapi import Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ValidationError

from forge_dash.soldr_github import GitHubIdentity, GitHubPairing

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

_EXPIRES_IN = 600
_POLL_INTERVAL = 5
_MAX_PENDING = 1024
_MAX_FORM_BYTES = 256
_HEADERS = {"Cache-Control": "no-store"}
_USER_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PairingStart(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass
class PendingPairing:
    user_code: str
    expires_at: float
    state: str = ""
    verifier: str = ""
    account: GitHubIdentity | None = None


@final
class Pairings:
    def __init__(self, clock: Callable[[], float], github: GitHubPairing | None) -> None:
        self.clock = clock
        self.github = github
        self.pending: dict[str, PendingPairing] = {}

    async def start(self, response: Response) -> PairingStart:
        if self.github is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Soldr access unavailable", _HEADERS
            )
        now = self.clock()
        self.pending = {key: entry for key, entry in self.pending.items() if entry.expires_at > now}
        if len(self.pending) >= _MAX_PENDING:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many pending pairings")
        device_code = secrets.token_urlsafe(32)
        active_codes = {entry.user_code for entry in self.pending.values()}
        while True:
            code = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(10))
            user_code = f"{code[:5]}-{code[5:]}"
            if user_code not in active_codes:
                break
        self.pending[_digest(device_code)] = PendingPairing(user_code, now + _EXPIRES_IN)
        response.headers.update(_HEADERS)
        return PairingStart(
            device_code=device_code,
            user_code=user_code,
            verification_uri="https://forge-ci.com/soldr/pair",
            expires_in=_EXPIRES_IN,
            interval=_POLL_INTERVAL,
        )

    async def poll(self, authorization: Annotated[str | None, Header()] = None) -> Response:
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not credential:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device code required", _HEADERS)
        key = _digest(credential)
        pairing = self.pending.get(key)
        if pairing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device code", _HEADERS)
        if pairing.expires_at <= self.clock():
            del self.pending[key]
            raise HTTPException(status.HTTP_410_GONE, "pairing expired", _HEADERS)
        if pairing.account is None or self.github is None:
            return Response(status_code=status.HTTP_202_ACCEPTED, headers=_HEADERS)
        del self.pending[key]
        claims = f"v1.{pairing.account.id}.{secrets.token_urlsafe(32)}"
        signature = self.sign(claims, self.github.signing_key)
        return JSONResponse(
            {"token": f"{claims}.{signature}", "github_login": pairing.account.login},
            headers=_HEADERS,
        )

    @staticmethod
    def sign(claims: str, key: bytes) -> str:
        return hmac.digest(key, claims.encode(), "sha256").hex()

    async def session(self, authorization: Annotated[str | None, Header()] = None) -> Response:
        if self.github is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Soldr access unavailable", _HEADERS
            )
        scheme, _, credential = (authorization or "").partition(" ")
        claims, _, signature = credential.rpartition(".")
        version, _, account_and_nonce = claims.partition(".")
        account, _, nonce = account_and_nonce.partition(".")
        if (
            scheme.lower() != "bearer"
            or version != "v1"
            or account != str(self.github.customer_id)
            or not nonce
            or not hmac.compare_digest(
                signature.encode(), self.sign(claims, self.github.signing_key).encode()
            )
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid Soldr credential", _HEADERS)
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_HEADERS)

    async def form(self) -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Pair Soldr</title>
<main><h1>Pair Soldr</h1>
<form method="post"><label for="code">Code shown by Soldr</label>
<input id="code" name="user_code" required maxlength="11" autocomplete="off">
<button>Continue with GitHub</button></form></main></html>""",
            headers=_HEADERS,
        )

    async def begin_github(self, request: Request) -> RedirectResponse:
        if self.github is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub sign-in unavailable")
        form = bytearray()
        async for chunk in request.stream():
            form.extend(chunk)
            if len(form) > _MAX_FORM_BYTES:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "form too large")
        code = parse_qs(form.decode("utf-8", errors="replace")).get("user_code", [""])[0]
        now = self.clock()
        for pairing in self.pending.values():
            if pairing.user_code == code.strip().upper() and pairing.expires_at > now:
                break
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired pairing code")
        if pairing.account is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "pairing already approved")
        pairing.state = secrets.token_urlsafe(32)
        pairing.verifier = secrets.token_urlsafe(32)
        response = RedirectResponse(
            self.github.authorize_url(pairing.state, pairing.verifier),
            status_code=status.HTTP_303_SEE_OTHER,
            headers=_HEADERS,
        )
        response.set_cookie(
            "__Host-soldr-pair", pairing.state, max_age=_EXPIRES_IN, secure=True, httponly=True
        )
        return response

    async def finish_github(self, request: Request, state: str, code: str) -> HTMLResponse:
        if self.github is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub sign-in unavailable")
        cookie = request.cookies.get("__Host-soldr-pair", "")
        if not state or not hmac.compare_digest(state.encode(), cookie.encode()):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid GitHub state", _HEADERS)
        now = self.clock()
        for device_digest, pairing in self.pending.items():  # noqa: B007 - checked after exchange
            if pairing.state == state and pairing.expires_at > now:
                break
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "expired GitHub sign-in", _HEADERS)
        verifier = pairing.verifier
        pairing.state = ""
        pairing.verifier = ""
        try:
            account = await asyncio.to_thread(self.github.identity, code, verifier)
        except (OSError, ValidationError) as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "GitHub sign-in failed", _HEADERS
            ) from exc
        if account.id != self.github.customer_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "account has no Soldr access", _HEADERS)
        if self.pending.get(device_digest) is not pairing or pairing.expires_at <= self.clock():
            raise HTTPException(status.HTTP_410_GONE, "pairing expired", _HEADERS)
        if pairing.account is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "pairing already approved", _HEADERS)
        pairing.account = account
        response = HTMLResponse(
            """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Soldr paired</title>
<main><h1>Soldr paired</h1><p>Return to your terminal.</p></main></html>""",
            headers=_HEADERS,
        )
        response.delete_cookie("__Host-soldr-pair", secure=True, httponly=True)
        return response


def install_pairing_routes(app: FastAPI, clock: Callable[[], float]) -> None:
    pairings = Pairings(clock, GitHubPairing.from_env())
    app.post("/api/v1/soldr/pairings", status_code=status.HTTP_201_CREATED)(pairings.start)
    app.post("/api/v1/soldr/pairings/poll")(pairings.poll)
    app.get("/api/v1/soldr/session")(pairings.session)
    app.get("/soldr/pair")(pairings.form)
    app.post("/soldr/pair")(pairings.begin_github)
    app.get("/soldr/pair/github")(pairings.finish_github)
