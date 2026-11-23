"""Short-lived Soldr device pairing requests."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, final

from fastapi import Header, HTTPException, Response, status
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

_EXPIRES_IN = 600
_POLL_INTERVAL = 5
_MAX_PENDING = 1024
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


@dataclass(frozen=True)
class PendingPairing:
    user_code: str
    expires_at: float


@final
class Pairings:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.pending: dict[str, PendingPairing] = {}

    async def start(self, response: Response) -> PairingStart:
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
        return Response(status_code=status.HTTP_202_ACCEPTED, headers=_HEADERS)


def install_pairing_routes(app: FastAPI, clock: Callable[[], float]) -> None:
    pairings = Pairings(clock)
    app.post("/api/v1/soldr/pairings", status_code=status.HTTP_201_CREATED)(pairings.start)
    app.post("/api/v1/soldr/pairings/poll")(pairings.poll)
