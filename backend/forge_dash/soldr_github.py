"""GitHub identity verification for Soldr pairing."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from http.client import HTTPResponse

_CALLBACK = "https://forge-ci.com/soldr/pair/github"
_GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
_GITHUB_EXCHANGE = "https://github.com/login/oauth/access_token"
_GITHUB_USER = "https://api.github.com/user"
_MIN_SIGNING_KEY_BYTES = 32


class GitHubIdentity(BaseModel):
    id: int = Field(gt=0)
    login: str


class _GitHubToken(BaseModel):
    access_token: str


@dataclass(frozen=True)
class GitHubPairing:
    client_id: str
    client_secret: str
    customer_id: int
    signing_key: bytes

    @classmethod
    def from_env(cls) -> GitHubPairing | None:
        client_id = os.environ.get("FORGE_SOLDR_GITHUB_CLIENT_ID")
        client_secret = os.environ.get("FORGE_SOLDR_GITHUB_CLIENT_SECRET")
        customer_id = os.environ.get("FORGE_SOLDR_GITHUB_CUSTOMER_ID", "")
        signing_key = os.environ.get("FORGE_SOLDR_SIGNING_KEY", "").encode()
        if (
            client_id
            and client_secret
            and customer_id.isdecimal()
            and len(signing_key) >= _MIN_SIGNING_KEY_BYTES
        ):
            return cls(client_id, client_secret, int(customer_id), signing_key)
        return None

    def authorize_url(self, state: str, verifier: str) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": _CALLBACK,
                "state": state,
                "code_challenge": challenge.rstrip(b"=").decode(),
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"{_GITHUB_AUTHORIZE}?{query}"

    def identity(self, code: str, verifier: str) -> GitHubIdentity:
        payload = urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": _CALLBACK,
            }
        ).encode()
        exchange = Request(_GITHUB_EXCHANGE, data=payload, headers={"Accept": "application/json"})
        response = cast("HTTPResponse", urlopen(exchange, timeout=10))  # noqa: S310 - fixed HTTPS URL
        with response as reply:
            token = _GitHubToken.model_validate_json(reply.read(65536))
        lookup = Request(
            _GITHUB_USER,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token.access_token}",
                "User-Agent": "forge-ci-soldr",
            },
        )
        response = cast("HTTPResponse", urlopen(lookup, timeout=10))  # noqa: S310 - fixed HTTPS URL
        with response as reply:
            return GitHubIdentity.model_validate_json(reply.read(65536))
