"""Integration tests for the Google OAuth flow in auth/oauth.py."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("OAUTH_JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI",
        "https://cornerstone-mcp.example/oauth/callback",
    )
    monkeypatch.setenv("GOOGLE_HOSTED_DOMAIN", "charlieoscar.com")
    monkeypatch.setenv("CORNERSTONE_URL", "http://fake-backend.test")
    monkeypatch.setenv("MEMORY_API_KEY", "superuser-key-for-test")
    monkeypatch.setenv("ALLOW_API_KEY_LOGIN", "true")


@pytest.fixture
def app_with_google_routes(google_env):
    """Build a Starlette app exposing only the OAuth routes for testing."""
    from auth import oauth

    class DummyMCP:
        def __init__(self):
            self.routes: list[Route] = []

        def custom_route(self, path, methods):
            def decorator(fn):
                for m in methods:
                    self.routes.append(Route(path, fn, methods=[m]))
                return fn
            return decorator

    dummy = DummyMCP()
    oauth.register_login_routes(dummy)
    return Starlette(routes=dummy.routes)


def _make_session_jwt():
    from auth import oauth

    return oauth.jwt_encode(
        {
            "type": "auth_session",
            "sid": "test-session-123",
            "client_id": "claude-desktop",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "redirect_uri_explicit": True,
            "code_challenge": "test-code-challenge",
            "state": "client-state",
            "scopes": ["memory"],
            "exp": time.time() + 600,
        }
    )


def test_google_start_redirects_to_google(app_with_google_routes):
    session_jwt = _make_session_jwt()
    client = TestClient(app_with_google_routes)
    resp = client.get(f"/oauth/google/start?session={session_jwt}", follow_redirects=False)

    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth")

    qs = parse_qs(urlparse(location).query)
    assert qs["client_id"] == ["test-client.apps.googleusercontent.com"]
    assert qs["redirect_uri"] == ["https://cornerstone-mcp.example/oauth/callback"]
    assert qs["response_type"] == ["code"]
    assert qs["scope"] == ["openid email profile"]
    assert qs["hd"] == ["charlieoscar.com"]
    from auth import oauth
    state_payload = oauth.jwt_decode(qs["state"][0])
    assert state_payload is not None
    assert state_payload["type"] == "google_state"
    assert state_payload["session_sid"] == "test-session-123"


def _make_state_jwt():
    from auth import oauth

    session = {
        "type": "auth_session",
        "sid": "test-session-456",
        "client_id": "claude-desktop",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "redirect_uri_explicit": True,
        "code_challenge": "test-challenge",
        "state": "client-state-value",
        "scopes": ["memory"],
        "exp": time.time() + 600,
    }
    return oauth.jwt_encode(
        {
            "type": "google_state",
            "session_sid": session["sid"],
            "session": session,
            "exp": time.time() + 600,
        }
    )


def test_callback_happy_path_issues_auth_code(app_with_google_routes):
    state = _make_state_jwt()
    client = TestClient(app_with_google_routes)

    async def fake_exchange(code, redirect_uri):
        return {"access_token": "ya29", "id_token": "fake-id-token", "expires_in": 3599}

    async def fake_verify(id_token):
        return {
            "email": "alice@charlieoscar.com",
            "name": "Alice Smith",
            "sub": "google-alice",
            "hd": "charlieoscar.com",
        }

    backend_response = MagicMock()
    backend_response.status_code = 200
    backend_response.json = MagicMock(
        return_value={
            "principal_id": "alice-uuid",
            "principal_name": "Alice Smith",
            "api_key": "csk_test_alice",
            "created": False,
        }
    )
    backend_response.raise_for_status = MagicMock()

    backend_client = MagicMock()
    backend_client.post = AsyncMock(return_value=backend_response)
    backend_ctx = AsyncMock()
    backend_ctx.__aenter__ = AsyncMock(return_value=backend_client)
    backend_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("auth.google.exchange_code_for_tokens", side_effect=fake_exchange), \
         patch("auth.google.verify_id_token", side_effect=fake_verify), \
         patch("auth.oauth.httpx.AsyncClient", return_value=backend_ctx):
        resp = client.get(
            "/oauth/callback?code=google-auth-code&state=" + state,
            follow_redirects=False,
        )

    assert resp.status_code in (200, 302)
    body = resp.text
    assert "csk_test_alice" not in body, "api_key should never appear in HTML response"
    auth_code = None
    if resp.status_code == 302:
        loc = resp.headers["location"]
        qs = parse_qs(urlparse(loc).query)
        auth_code = qs.get("code", [None])[0]
    else:
        import re
        m = re.search(r"code=([A-Za-z0-9_\-.]+)", body)
        auth_code = m.group(1) if m else None

    assert auth_code, f"No auth code found in response: {resp.status_code} {body[:500]}"
    from auth import oauth
    payload = oauth.jwt_decode(auth_code)
    assert payload is not None
    assert payload["type"] == "auth_code"
    assert payload["principal_id"] == "alice-uuid"
    assert payload["principal_name"] == "Alice Smith"
    assert oauth._deobfuscate_key(payload["api_key_obf"]) == "csk_test_alice"


def test_callback_rejects_bad_state(app_with_google_routes):
    client = TestClient(app_with_google_routes)
    resp = client.get("/oauth/callback?code=any&state=not-a-real-jwt", follow_redirects=False)
    assert resp.status_code == 400


def test_callback_rejects_expired_state(app_with_google_routes):
    from auth import oauth

    expired = oauth.jwt_encode(
        {
            "type": "google_state",
            "session_sid": "expired",
            "session": {"type": "auth_session", "exp": time.time() - 100},
            "exp": time.time() - 100,
        }
    )
    client = TestClient(app_with_google_routes)
    resp = client.get(f"/oauth/callback?code=any&state={expired}", follow_redirects=False)
    assert resp.status_code == 400


def test_callback_rejects_unverified_google_token(app_with_google_routes):
    state = _make_state_jwt()
    client = TestClient(app_with_google_routes)

    async def fake_exchange(code, redirect_uri):
        return {"id_token": "bad-token"}

    async def fake_verify(id_token):
        return None

    with patch("auth.google.exchange_code_for_tokens", side_effect=fake_exchange), \
         patch("auth.google.verify_id_token", side_effect=fake_verify):
        resp = client.get(f"/oauth/callback?code=x&state={state}", follow_redirects=False)

    assert resp.status_code in (400, 401)
    assert "csk_" not in resp.text
    assert "type\":\"auth_code\"" not in resp.text


def test_callback_rejects_backend_failure(app_with_google_routes):
    state = _make_state_jwt()
    client = TestClient(app_with_google_routes)

    async def fake_exchange(code, redirect_uri):
        return {"id_token": "good-token"}

    async def fake_verify(id_token):
        return {
            "email": "alice@charlieoscar.com",
            "name": "Alice",
            "sub": "x",
            "hd": "charlieoscar.com",
        }

    backend_response = MagicMock()
    backend_response.status_code = 500
    backend_response.json = MagicMock(return_value={"detail": "db down"})
    backend_response.raise_for_status = MagicMock(side_effect=Exception("500"))

    backend_client = MagicMock()
    backend_client.post = AsyncMock(return_value=backend_response)
    backend_ctx = AsyncMock()
    backend_ctx.__aenter__ = AsyncMock(return_value=backend_client)
    backend_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("auth.google.exchange_code_for_tokens", side_effect=fake_exchange), \
         patch("auth.google.verify_id_token", side_effect=fake_verify), \
         patch("auth.oauth.httpx.AsyncClient", return_value=backend_ctx):
        resp = client.get(f"/oauth/callback?code=x&state={state}", follow_redirects=False)

    assert resp.status_code >= 400
