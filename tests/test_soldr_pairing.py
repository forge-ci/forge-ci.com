from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from forge_dash.app import create_app
from forge_dash.config import Settings
from forge_dash.soldr_github import GitHubIdentity, GitHubPairing


class Credential(BaseModel):
    token: str


class WorkerGrant(BaseModel):
    origin: str
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
        assert pairing.expires_in == 1800
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
        assert "soldr pair" in response.text
        assert response.headers["cache-control"] == "no-store"
        pairing_policy = response.headers["content-security-policy"]
        assert "form-action 'self' https://github.com" in pairing_policy
        dashboard = client.get("/dashboard")
        assert "form-action 'self'" in dashboard.headers["content-security-policy"]
        assert "github.com" not in dashboard.headers["content-security-policy"]


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
    monkeypatch.setenv("FORGE_SOLDR_WORKER_ORIGIN", "https://build.forge-ci.com")
    monkeypatch.setenv("FORGE_SOLDR_WORKER_KEY", "worker-key" * 4)
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
                discovery = "/api/v1/soldr/worker"
                assert replacement.post(discovery, headers=forged).status_code == 401
                response = replacement.post(discovery, headers=bearer)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                worker = WorkerGrant.model_validate_json(response.content)
                assert worker.origin == "https://build.forge-ci.com"
                assert worker.token.startswith("w1.6856673.")
                assert (
                    replacement.get(
                        session, headers={"Authorization": f"Bearer {worker.token}"}
                    ).status_code
                    == 401
                )
            assert client.post("/api/v1/soldr/pairings/poll", headers=headers).status_code == 404
        else:
            assert response.status_code == 202


def test_pairing_is_disabled_without_github_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitHubPairing, "from_env", lambda: None)
    app, _ = create_app(Settings())
    with TestClient(app) as client:
        assert client.post("/api/v1/soldr/pairings").status_code == 503


def test_splash_and_dashboard_require_browser_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = GitHubPairing("id", "secret", 6856673, b"test-key" * 4)
    monkeypatch.setattr(GitHubPairing, "from_env", lambda: config)

    def identity(_config: GitHubPairing, _code: str, _verifier: str) -> GitHubIdentity:
        return GitHubIdentity(id=6856673, login="customer")

    monkeypatch.setattr(GitHubPairing, "identity", identity)
    (tmp_path / "splash.html").write_text("Sign in with GitHub")
    (tmp_path / "index.html").write_text("Fleet utilization")
    (tmp_path / "assets").mkdir()
    app, _ = create_app(Settings(static_dir=tmp_path))
    with TestClient(app, base_url="https://forge-ci.com", follow_redirects=False) as client:
        assert client.get("/").text == "Sign in with GitHub"
        assert client.get("/dashboard").headers["location"] == "/"
        assert client.get("/index.html").status_code == 404
        login = client.get("/login")
        assert login.status_code == 303
        query = parse_qs(urlparse(login.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        params = {"state": query["state"][0], "code": "github-code"}
        with TestClient(app, base_url="https://forge-ci.com") as other_browser:
            assert other_browser.get("/soldr/pair/github", params=params).status_code == 400
        callback = client.get("/soldr/pair/github", params=params)
        assert callback.headers["location"] == "/dashboard"
        assert "HttpOnly" in callback.headers["set-cookie"]
        assert client.get("/dashboard").text == "Fleet utilization"
        assert client.get("/soldr/pair/github", params=params).status_code == 400
        assert client.post("/logout").headers["location"] == "/"
        assert client.get("/dashboard").headers["location"] == "/"
