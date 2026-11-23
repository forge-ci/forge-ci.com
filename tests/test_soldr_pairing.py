from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from forge_dash.app import create_app
from forge_dash.config import Settings
from forge_dash.soldr_github import GitHubIdentity, GitHubPairing


class Credential(BaseModel):
    token: str


class Pairing(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


def test_pairing_start_returns_distinct_public_and_private_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GitHubPairing("id", "secret", 6856673, b"test-key" * 4)
    monkeypatch.setattr(GitHubPairing, "from_env", lambda: config)
    app, _ = create_app(Settings())
    with TestClient(app) as client:
        response = client.post("/api/v1/soldr/pairings")
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        pairing = Pairing.model_validate_json(response.content)
        assert pairing.verification_uri == "https://forge-ci.com/soldr/pair"
        assert len(pairing.device_code) >= 43
        assert pairing.user_code not in pairing.device_code
        headers = {"Authorization": f"Bearer {pairing.device_code}"}
        assert client.post("/api/v1/soldr/pairings/poll", headers=headers).status_code == 202
        headers = {"Authorization": f"Bearer {pairing.user_code}"}
        assert client.post("/api/v1/soldr/pairings/poll", headers=headers).status_code == 404


def test_browser_has_a_pairing_code_form() -> None:
    app, _ = create_app(Settings())
    with TestClient(app) as client:
        response = client.get("/soldr/pair")
        assert response.status_code == 200
        assert 'name="user_code"' in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "form-action 'self'" in response.headers["content-security-policy"]


@pytest.mark.parametrize(("account_id", "expected"), [(6856673, 200), (42, 403)])
def test_github_approval_issues_one_device_credential(
    monkeypatch: pytest.MonkeyPatch, account_id: int, expected: int
) -> None:
    config = GitHubPairing("id", "secret", 6856673, b"test-key" * 4)

    def identity(_config: GitHubPairing, code: str, verifier: str) -> GitHubIdentity:
        assert code == "github-code"
        assert len(verifier) >= 43
        return GitHubIdentity(id=account_id, login="customer")

    monkeypatch.setattr(GitHubPairing, "from_env", lambda: config)
    monkeypatch.setattr(GitHubPairing, "identity", identity)
    app, _ = create_app(Settings())
    with TestClient(app, base_url="https://forge-ci.com") as client:
        response = client.post("/api/v1/soldr/pairings")
        pairing = Pairing.model_validate_json(response.content)
        response = client.post(
            "/soldr/pair", data={"user_code": pairing.user_code}, follow_redirects=False
        )
        assert response.status_code == 303
        location = urlparse(response.headers["location"])
        assert location.netloc == "github.com"
        query = parse_qs(location.query)
        assert query["code_challenge_method"] == ["S256"]
        callback = "/soldr/pair/github"
        params = {"state": query["state"][0], "code": "github-code"}
        with TestClient(app, base_url="https://forge-ci.com") as other_browser:
            assert other_browser.get(callback, params=params).status_code == 400
        assert client.get(callback, params=params).status_code == expected
        assert client.get(callback, params=params).status_code == 400
        headers = {"Authorization": f"Bearer {pairing.device_code}"}
        response = client.post("/api/v1/soldr/pairings/poll", headers=headers)
        if expected == 200:
            assert response.status_code == 200
            assert b'"github_login":"customer"' in response.content
            assert b'"token":"v1.6856673.' in response.content
            credential = Credential.model_validate_json(response.content)
            session = "/api/v1/soldr/session"
            bearer = {"Authorization": f"Bearer {credential.token}"}
            replacement_app, _ = create_app(Settings())
            with TestClient(replacement_app) as replacement:
                assert replacement.get(session, headers=bearer).status_code == 204
                forged = {"Authorization": f"Bearer {credential.token}0"}
                assert replacement.get(session, headers=forged).status_code == 401
            assert client.post("/api/v1/soldr/pairings/poll", headers=headers).status_code == 404
        else:
            assert response.status_code == 202


def test_pairing_is_disabled_without_github_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitHubPairing, "from_env", lambda: None)
    app, _ = create_app(Settings())
    with TestClient(app) as client:
        assert client.post("/api/v1/soldr/pairings").status_code == 503
