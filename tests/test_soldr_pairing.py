from fastapi.testclient import TestClient
from pydantic import BaseModel

from forge_dash.app import create_app
from forge_dash.config import Settings


class Pairing(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


def test_pairing_start_returns_distinct_public_and_private_codes() -> None:
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
