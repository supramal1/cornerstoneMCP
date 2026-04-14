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

    # Verify the id_token was actually sent to tokeninfo
    call_args = client.post.call_args
    assert call_args is not None
    sent_params = call_args.kwargs.get("params")
    assert sent_params == {"id_token": "fake-id-token"}


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


def test_is_configured_true_when_both_envs_set(google_env):
    from auth import google
    assert google.is_configured() is True


def test_is_configured_false_when_client_id_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    from auth import google
    assert google.is_configured() is False


def test_is_configured_false_when_client_secret_missing(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    from auth import google
    assert google.is_configured() is False


def test_build_authorization_url_contains_required_params(google_env):
    from urllib.parse import urlparse, parse_qs
    from auth import google

    url = google.build_authorization_url("state-jwt-value")
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"

    params = parse_qs(parsed.query)
    assert params["client_id"] == ["test-client-id.apps.googleusercontent.com"]
    assert params["redirect_uri"] == ["https://cornerstone-mcp.example/oauth/callback"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["openid email profile"]
    assert params["state"] == ["state-jwt-value"]
    assert params["access_type"] == ["online"]
    assert params["prompt"] == ["select_account"]
    assert params["hd"] == ["charlieoscar.com"]


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_raises_on_http_error(google_env):
    import httpx
    from auth import google

    # Build a mock where raise_for_status() raises HTTPStatusError
    response = MagicMock()
    response.status_code = 400
    response.json = MagicMock(return_value={"error": "invalid_grant"})
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "400 Bad Request", request=MagicMock(), response=response
        )
    )

    client_instance = MagicMock()
    client_instance.post = AsyncMock(return_value=response)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=client_instance)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("auth.google.httpx.AsyncClient", return_value=ctx):
        with pytest.raises(httpx.HTTPStatusError):
            await google.exchange_code_for_tokens(
                "bad-code", "https://cornerstone-mcp.example/oauth/callback"
            )
