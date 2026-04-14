"""Tests for auth/google.py — Google ID token verification."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI",
        "https://cornerstone-mcp.example/oauth/callback",
    )
    monkeypatch.setenv("GOOGLE_HOSTED_DOMAIN", "charlieoscar.com")


def _mock_httpx_post(json_body: dict, status_code: int = 200):
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body)
    response.raise_for_status = MagicMock()

    client_instance = MagicMock()
    client_instance.post = AsyncMock(return_value=response)

    async_ctx = AsyncMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client_instance)
    async_ctx.__aexit__ = AsyncMock(return_value=False)
    return async_ctx, client_instance


@pytest.mark.asyncio
async def test_verify_id_token_happy_path(google_env):
    from auth import google

    ctx, client = _mock_httpx_post(
        {
            "aud": "test-client-id.apps.googleusercontent.com",
            "email": "alice@charlieoscar.com",
            "email_verified": "true",
            "hd": "charlieoscar.com",
            "name": "Alice Smith",
            "sub": "google-uid-alice",
        }
    )

    with patch("auth.google.httpx.AsyncClient", return_value=ctx):
        result = await google.verify_id_token("fake-id-token")

    assert result is not None
    assert result["email"] == "alice@charlieoscar.com"
    assert result["name"] == "Alice Smith"
    assert result["sub"] == "google-uid-alice"
    assert result["hd"] == "charlieoscar.com"


@pytest.mark.asyncio
async def test_verify_id_token_rejects_wrong_hd(google_env):
    from auth import google

    ctx, client = _mock_httpx_post(
        {
            "aud": "test-client-id.apps.googleusercontent.com",
            "email": "evil@gmail.com",
            "email_verified": "true",
            "hd": "gmail.com",
            "sub": "google-uid-evil",
        }
    )
    with patch("auth.google.httpx.AsyncClient", return_value=ctx):
        result = await google.verify_id_token("fake-id-token")

    assert result is None


@pytest.mark.asyncio
async def test_verify_id_token_rejects_missing_hd(google_env):
    from auth import google

    ctx, client = _mock_httpx_post(
        {
            "aud": "test-client-id.apps.googleusercontent.com",
            "email": "anon@example.com",
            "email_verified": "true",
            "sub": "google-uid-anon",
        }
    )
    with patch("auth.google.httpx.AsyncClient", return_value=ctx):
        result = await google.verify_id_token("fake-id-token")

    assert result is None


@pytest.mark.asyncio
async def test_verify_id_token_rejects_wrong_audience(google_env):
    from auth import google

    ctx, client = _mock_httpx_post(
        {
            "aud": "some-other-client.apps.googleusercontent.com",
            "email": "alice@charlieoscar.com",
            "email_verified": "true",
            "hd": "charlieoscar.com",
            "sub": "google-uid-alice",
        }
    )
    with patch("auth.google.httpx.AsyncClient", return_value=ctx):
        result = await google.verify_id_token("fake-id-token")

    assert result is None


@pytest.mark.asyncio
async def test_verify_id_token_rejects_unverified_email(google_env):
    from auth import google

    ctx, client = _mock_httpx_post(
        {
            "aud": "test-client-id.apps.googleusercontent.com",
            "email": "alice@charlieoscar.com",
            "email_verified": "false",
            "hd": "charlieoscar.com",
            "sub": "google-uid-alice",
        }
    )
    with patch("auth.google.httpx.AsyncClient", return_value=ctx):
        result = await google.verify_id_token("fake-id-token")

    assert result is None


@pytest.mark.asyncio
async def test_verify_id_token_rejects_tokeninfo_500(google_env):
    from auth import google

    ctx, client = _mock_httpx_post({"error": "invalid token"}, status_code=400)
    with patch("auth.google.httpx.AsyncClient", return_value=ctx):
        result = await google.verify_id_token("fake-id-token")

    assert result is None


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_happy_path(google_env):
    from auth import google

    ctx, client = _mock_httpx_post(
        {
            "access_token": "ya29.fake",
            "id_token": "eyJ.fake.token",
            "refresh_token": "1//fake",
            "expires_in": 3599,
            "scope": "openid email profile",
            "token_type": "Bearer",
        }
    )
    with patch("auth.google.httpx.AsyncClient", return_value=ctx):
        result = await google.exchange_code_for_tokens(
            "fake-auth-code", "https://cornerstone-mcp.example/oauth/callback"
        )

    assert result["id_token"] == "eyJ.fake.token"
    assert result["access_token"] == "ya29.fake"
    call_args = client.post.call_args
    assert call_args is not None
    sent_data = call_args.kwargs.get("data") or (call_args.args[1] if len(call_args.args) > 1 else None)
    assert sent_data["code"] == "fake-auth-code"
    assert sent_data["grant_type"] == "authorization_code"
    assert sent_data["client_id"] == "test-client-id.apps.googleusercontent.com"
    assert sent_data["client_secret"] == "test-client-secret"
    assert sent_data["redirect_uri"] == "https://cornerstone-mcp.example/oauth/callback"
