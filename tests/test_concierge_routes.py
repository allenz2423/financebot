"""B0 capture routes: token-gated GET + POST on any FastAPI app."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.security.vault import Vault
from src.services.concierge.capture_schema import validate_capture_intent
from src.services.concierge.capture_tokens import CaptureTokenStore, TOKEN_TTL
from src.services.concierge.routes import register_capture_routes

KEY = "b0-routes-test-master-key"

AMAZON = {
    "label": "Amazon login",
    "explanation": "Sign-in credentials for amazon.com",
    "kind": "secret",
    "consumer_scope": ["amazon.com"],
    "fields": [
        {"type": "text", "label": "Email", "required": True},
        {"type": "password", "label": "Password", "required": True},
        {"type": "captcha", "label": "Captcha", "required": False, "retention": "ephemeral"},
    ],
}


def make_client(tmp_path):
    db = str(tmp_path / "r.db")
    store = CaptureTokenStore(db)
    vault = Vault(db, master_key=KEY)
    app = FastAPI()
    register_capture_routes(app, store=store, vault=vault)
    intent = validate_capture_intent(AMAZON, "user:1")
    token = store.issue("user:1", intent)
    return TestClient(app), token


def test_get_renders_generic_form(tmp_path):
    client, token = make_client(tmp_path)
    resp = client.get("/concierge/capture/" + token, params={"tenant": "user:1"})
    assert resp.status_code == 200
    body = resp.text
    assert "Amazon login" in body
    assert "name=\"password|Password\"" in body
    assert "name=\"text|Email\"" in body
    assert "amazon.com" in body


def test_post_returns_masks_only_and_double_post_fails(tmp_path):
    client, token = make_client(tmp_path)
    params = {"tenant": "user:1"}
    resp1 = client.post(
        "/concierge/capture/" + token,
        params=params,
        json={"text|Email": "me@example.com", "password|Password": "hunter2secret"},
    )
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["ok"] is True
    assert "hunter2secret" not in str(body1)
    assert body1["stored"][0]["display"]
    resp2 = client.post(
        "/concierge/capture/" + token,
        params=params,
        json={"text|Email": "me@example.com", "password|Password": "again"},
    )
    assert resp2.status_code == 400
    assert "already used" in resp2.json()["error"]
    assert "again" not in str(resp2.json())


def test_unknown_token_404(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.get("/concierge/capture/bogus", params={"tenant": "user:1"})
    assert resp.status_code == 404