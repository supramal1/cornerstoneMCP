"""Tests for DCR persistence via cornerstone-api admin endpoints.

Before this fix, ``CornerstoneOAuthProvider`` held registered OAuth
clients in a process-local dict. With two Cloud Run pods serving
cornerstone-mcp, a client_id registered on pod A produced a
``client_not_found`` error on pod B, breaking Claude Code OAuth every
other attempt.

Contract now:

    register_client()  →  POST {CORNERSTONE_URL}/admin/oauth_clients
    get_client(id)     →  GET  {CORNERSTONE_URL}/admin/oauth_clients/{id}
                          (404 is translated to None)

The two-pod simulation creates two separate provider instances —
modelling two pods with independent process state — and asserts that
client registration on instance A is visible to instance B, proving
the fix survives a pod failover.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.shared.auth import OAuthClientInformationFull

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OAUTH_JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("CORNERSTONE_URL", "http://fake-backend.test")
    monkeypatch.setenv("MEMORY_API_KEY", "superuser-key-for-test")


def _make_client_info(client_id: str = "client_abc") -> OAuthClientInformationFull:
    return OAuthClientInformationFull.model_validate(
        {
            "client_id": client_id,
            "client_secret": "sek_xyz",
            "client_name": "Test Client",
            "redirect_uris": ["http://localhost:8080/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }
    )


class _FakeBackend:
    """A tiny in-memory stand-in for the cornerstone-api admin endpoints.

    Shared across two `CornerstoneOAuthProvider` instances to simulate
    pods talking to the same Supabase-backed registry.
    """

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.post_calls: list[dict] = []
        self.get_calls: list[str] = []

    async def post(self, url, headers=None, json=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        assert url.endswith("/admin/oauth_clients")
        self.store[json["client_id"]] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ""
        resp.json.return_value = {"client_id": json["client_id"]}
        return resp

    async def get(self, url, headers=None):
        self.get_calls.append(url)
        client_id = url.rsplit("/", 1)[-1]
        resp = MagicMock()
        if client_id in self.store:
            row = self.store[client_id]
            resp.status_code = 200
            resp.json.return_value = {
                "client_id": row["client_id"],
                "client_secret": row["client_secret"],
                "client_info": row["client_info"],
                "principal_id": None,
                "expires_at": None,
            }
        else:
            resp.status_code = 404
            resp.text = '{"detail":"client_not_found"}'
            resp.json.return_value = {"detail": "client_not_found"}
        return resp


def _mock_httpx(backend: _FakeBackend):
    """Return a context-manager-compatible mock for httpx.AsyncClient."""

    class _Ctx:
        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(side_effect=backend.post)
            client.get = AsyncMock(side_effect=backend.get)
            return client

        async def __aexit__(self, *a):
            return False

    def factory(*_a, **_kw):
        return _Ctx()

    return factory


# ---------------------------------------------------------------------------
# Cross-pod persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_on_pod_a_visible_on_pod_b():
    """Regression trip-wire for the Claude Code OAuth bug.

    If this test ever reverts to the in-memory dict, two separate
    provider instances lose visibility of each other's registrations
    and the test fails with ``get_client() is None``.
    """
    from auth.oauth import CornerstoneOAuthProvider

    backend = _FakeBackend()
    info = _make_client_info("client_cross_pod")

    pod_a = CornerstoneOAuthProvider()
    pod_b = CornerstoneOAuthProvider()

    with patch("auth.oauth.httpx.AsyncClient", _mock_httpx(backend)):
        await pod_a.register_client(info)
        fetched = await pod_b.get_client("client_cross_pod")

    assert fetched is not None
    assert fetched.client_id == "client_cross_pod"
    assert fetched.client_name == "Test Client"
    assert len(backend.post_calls) == 1
    assert len(backend.get_calls) == 1


@pytest.mark.asyncio
async def test_get_client_returns_none_on_404():
    """Unknown client_id must translate a backend 404 into a None.
    FastMCP's token endpoint uses the None return as the signal to
    raise ``client_not_found`` — do not change this contract."""
    from auth.oauth import CornerstoneOAuthProvider

    backend = _FakeBackend()
    provider = CornerstoneOAuthProvider()

    with patch("auth.oauth.httpx.AsyncClient", _mock_httpx(backend)):
        fetched = await provider.get_client("client_never_registered")

    assert fetched is None


@pytest.mark.asyncio
async def test_register_client_sends_full_client_info():
    """The backend needs the full pydantic dump so get_client can
    reconstruct an OAuthClientInformationFull of identical shape."""
    from auth.oauth import CornerstoneOAuthProvider

    backend = _FakeBackend()
    info = _make_client_info("client_full_dump")
    provider = CornerstoneOAuthProvider()

    with patch("auth.oauth.httpx.AsyncClient", _mock_httpx(backend)):
        await provider.register_client(info)

    assert backend.post_calls[0]["json"]["client_id"] == "client_full_dump"
    client_info = backend.post_calls[0]["json"]["client_info"]
    assert client_info["client_name"] == "Test Client"
    assert client_info["redirect_uris"] == ["http://localhost:8080/callback"]


@pytest.mark.asyncio
async def test_backend_500_returns_none_and_does_not_raise():
    """If the admin endpoint is down, get_client must degrade to None
    rather than raising — the token endpoint shouldn't 500 on a
    cornerstone-api outage, it should return client_not_found cleanly."""
    from auth.oauth import CornerstoneOAuthProvider

    class _BrokenBackend(_FakeBackend):
        async def get(self, url, headers=None):
            resp = MagicMock()
            resp.status_code = 500
            resp.text = "internal error"
            return resp

    provider = CornerstoneOAuthProvider()
    with patch("auth.oauth.httpx.AsyncClient", _mock_httpx(_BrokenBackend())):
        result = await provider.get_client("anything")
    assert result is None


# ---------------------------------------------------------------------------
# Fix 3: discovery metadata advertises "none"
# ---------------------------------------------------------------------------


def test_discovery_metadata_includes_none_auth_method():
    """Public clients (CLI tools direct-to-MCP) need ``"none"`` in
    ``token_endpoint_auth_methods_supported`` to signal PKCE-only
    auth is accepted. auth/oauth.py patches the SDK's
    ``build_metadata`` at import time; this test guards that patch."""
    from mcp.server.auth import routes as _mcp_auth_routes
    from mcp.server.auth.settings import (
        ClientRegistrationOptions,
        RevocationOptions,
    )
    from pydantic import AnyHttpUrl

    # Force the patch to load
    import auth.oauth  # noqa: F401

    metadata = _mcp_auth_routes.build_metadata(
        issuer_url=AnyHttpUrl("http://fake-mcp.test"),
        service_documentation_url=None,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(),
    )
    methods = metadata.token_endpoint_auth_methods_supported or []
    assert "none" in methods
    # Existing methods still advertised — don't regress other clients
    assert "client_secret_post" in methods
    assert "client_secret_basic" in methods
