"""Browser sign-in for the Forge CI dashboard."""

from __future__ import annotations

import asyncio
import hmac
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from forge_dash.soldr_github import GitHubPairing

STATE_PREFIX = "dashboard."
_STATE_COOKIE = "__Host-forge-login"
_SESSION_COOKIE = "__Host-forge-session"
_STATE_TTL = 600
_SESSION_TTL = 8 * 60 * 60
_MAX_PENDING = 256
_MAX_EPOCH_DIGITS = 12
_PRIVATE = {"Cache-Control": "no-store"}


@dataclass(frozen=True)
class PendingLogin:
    verifier: str
    expires: float


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER, headers=_PRIVATE)


@final
class BrowserLogin:
    def __init__(self, github: GitHubPairing | None, clock: Callable[[], float]) -> None:
        self.github = github
        self.clock = clock
        self.pending: dict[str, PendingLogin] = {}

    def _sign(self, payload: str, github: GitHubPairing) -> str:
        return hmac.digest(github.signing_key, f"dashboard:{payload}".encode(), "sha256").hex()

    def signed_in(self, request: Request) -> bool:
        if self.github is None:
            return False
        value = request.cookies.get(_SESSION_COOKIE, "")
        payload, _, signature = value.rpartition(".")
        account, _, expires = payload.partition(":")
        return (
            account == str(self.github.customer_id)
            and expires.isascii()
            and expires.isdecimal()
            and len(expires) <= _MAX_EPOCH_DIGITS
            and int(expires) > self.clock()
            and hmac.compare_digest(signature.encode(), self._sign(payload, self.github).encode())
        )

    async def begin(self, request: Request) -> RedirectResponse:
        if request.url.hostname == "www.forge-ci.com":
            return redirect("https://forge-ci.com/login")
        if self.signed_in(request):
            return redirect("/dashboard")
        if self.github is None:
            raise HTTPException(503, "GitHub sign-in unavailable", _PRIVATE)
        now = self.clock()
        self.pending = {
            state: login for state, login in self.pending.items() if login.expires > now
        }
        if len(self.pending) >= _MAX_PENDING:
            raise HTTPException(503, "Too many sign-in attempts. Try again shortly.", _PRIVATE)
        state = STATE_PREFIX + secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(32)
        self.pending[state] = PendingLogin(verifier, now + _STATE_TTL)
        response = redirect(self.github.authorize_url(state, verifier))
        response.set_cookie(_STATE_COOKIE, state, max_age=_STATE_TTL, secure=True, httponly=True)
        return response

    async def finish(self, request: Request, state: str, code: str) -> RedirectResponse:
        if self.github is None:
            raise HTTPException(503, "GitHub sign-in unavailable", _PRIVATE)
        cookie = request.cookies.get(_STATE_COOKIE, "")
        if not hmac.compare_digest(state.encode(), cookie.encode()):
            raise HTTPException(400, "Invalid GitHub state", _PRIVATE)
        login = self.pending.pop(state, None)
        if login is None or login.expires <= self.clock():
            raise HTTPException(400, "Expired GitHub sign-in", _PRIVATE)
        try:
            account = await asyncio.to_thread(self.github.identity, code, login.verifier)
        except (OSError, ValidationError) as exc:
            raise HTTPException(502, "GitHub sign-in failed", _PRIVATE) from exc
        if account.id != self.github.customer_id:
            raise HTTPException(403, "Account has no Forge CI access", _PRIVATE)
        payload = f"{account.id}:{int(self.clock()) + _SESSION_TTL}"
        response = redirect("/dashboard")
        response.set_cookie(
            _SESSION_COOKIE,
            f"{payload}.{self._sign(payload, self.github)}",
            max_age=_SESSION_TTL,
            secure=True,
            httponly=True,
        )
        response.delete_cookie(_STATE_COOKIE, secure=True, httponly=True)
        return response

    async def logout(self) -> RedirectResponse:
        response = redirect("/")
        response.delete_cookie(_SESSION_COOKIE, secure=True, httponly=True)
        return response

    def splash(self, request: Request, static_dir: Path) -> FileResponse | RedirectResponse:
        if self.signed_in(request):
            return redirect("/dashboard")
        return FileResponse(static_dir / "splash.html", headers=_PRIVATE)

    def dashboard(self, request: Request, static_dir: Path) -> FileResponse | RedirectResponse:
        if not self.signed_in(request):
            return redirect("/")
        return FileResponse(static_dir / "index.html", headers=_PRIVATE)
