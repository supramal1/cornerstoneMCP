"""Tests for the login page rendering mode (Google vs API key)."""

import os
import sys
import time
from urllib.parse import quote

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def build_app():
    """Return a factory that builds a Starlette app with the login routes."""
    def _factory():
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

    return _factory


def _session():
    from auth import oauth

    return oauth.jwt_encode(
        {
            "type": "auth_session",
            "sid": "s",
            "client_id": "c",
            "redirect_uri": "https://example/cb",
            "redirect_uri_explicit": True,
            "code_challenge": "x",
            "state": "y",
            "scopes": ["memory"],
            "exp": time.time() + 600,
        }
    )


def test_default_mode_hides_api_key_form(monkeypatch, build_app):
    monkeypatch.setenv("OAUTH_JWT_SECRET", "test-jwt-secret")
    monkeypatch.delenv("ALLOW_API_KEY_LOGIN", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    session = _session()

    app = build_app()
    client = TestClient(app)
    resp = client.get(f"/oauth/login?session={session}")

    assert resp.status_code == 200
    body = resp.text
    assert "Sign in with Google" in body
    assert f"/oauth/google/start?session={quote(session)}" in body or "/oauth/google/start" in body
    # API key form must not be present
    assert 'name="api_key"' not in body
    assert "csk_..." not in body


def test_dev_mode_shows_both(monkeypatch, build_app):
    monkeypatch.setenv("OAUTH_JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("ALLOW_API_KEY_LOGIN", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    session = _session()

    app = build_app()
    client = TestClient(app)
    resp = client.get(f"/oauth/login?session={session}")

    assert resp.status_code == 200
    body = resp.text
    assert "Sign in with Google" in body
    assert 'name="api_key"' in body


def test_google_unconfigured_falls_back_to_api_key(monkeypatch, build_app):
    """Defensive: if Google creds are missing, don't lock users out — show the API key form."""
    monkeypatch.setenv("OAUTH_JWT_SECRET", "test-jwt-secret")
    monkeypatch.delenv("ALLOW_API_KEY_LOGIN", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    session = _session()

    app = build_app()
    client = TestClient(app)
    resp = client.get(f"/oauth/login?session={session}")

    assert resp.status_code == 200
    body = resp.text
    assert 'name="api_key"' in body


def test_google_unconfigured_but_api_key_allowed_shows_api_key_only(monkeypatch, build_app):
    """Row 4: no Google creds + ALLOW_API_KEY_LOGIN=true → API key form only, no Google link."""
    monkeypatch.setenv("OAUTH_JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("ALLOW_API_KEY_LOGIN", "true")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    session = _session()

    app = build_app()
    client = TestClient(app)
    resp = client.get(f"/oauth/login?session={session}")

    assert resp.status_code == 200
    body = resp.text
    assert 'name="api_key"' in body
    assert "Sign in with Google" not in body
