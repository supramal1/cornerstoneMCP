"""Integration test: list_workspaces is consumable by a non-Claude-Desktop MCP client.

Context: the Cookbook MCP (co-cookbook-mcp) calls Cornerstone MCP server-to-server
to resolve which namespaces a principal can see. That M2M path does not apply
Claude Desktop's client-side capability filter, so every tool the server
advertises in tools/list is reachable.

This test locks that contract in:

1. list_workspaces is advertised in the server's tools/list manifest (the
   method every MCP client calls to discover tools).
2. Invoking it via mcp.call_tool returns the {name, display_name, access_level}
   fields for each granted workspace. That is the exact shape Cookbook's
   scope-filter query needs to build its SQL WHERE clause.

If either assertion breaks, Cookbook's scope resolution will break. Do not
relax them to "works in Claude Desktop" — that is the wrong surface.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# Fixture: a realistic /connection/workspaces payload. Three workspaces with
# distinct access levels — proves the grant-level field round-trips, which is
# what Cookbook uses to decide write vs read-only scopes.
_BACKEND_WORKSPACES_PAYLOAD = {
    "workspaces": [
        {
            "name": "default",
            "display_name": "Default",
            "access_level": "admin",
            "status": "active",
        },
        {
            "name": "useful-machines",
            "display_name": "Useful Machines",
            "access_level": "read_write",
            "status": "active",
        },
        {
            "name": "testworkspace",
            "display_name": "Test Workspace",
            "access_level": "read",
            "status": "active",
        },
    ]
}


def _mock_http_client_returning(payload: dict) -> MagicMock:
    """Build a mock context-managed httpx client whose .get() returns `payload`."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=payload)

    client = MagicMock()
    client.get = MagicMock(return_value=response)

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=client)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "default",
    },
)
async def test_list_workspaces_advertised_in_tools_manifest():
    """An M2M MCP client calls tools/list to discover available tools.
    list_workspaces MUST appear in that manifest — no Claude Desktop filter
    applies to direct MCP clients.
    """
    # Importing server side-effect-registers every @mcp.tool() with the
    # FastMCP instance, matching what happens at real server start.
    import server  # noqa: F401 — side-effect import registers all tools
    from core import mcp

    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}

    assert "list_workspaces" in tool_names, (
        f"list_workspaces missing from MCP tools/list manifest. "
        f"Advertised tools: {sorted(tool_names)}. "
        f"This breaks Cookbook's M2M scope resolution — Cookbook relies on "
        f"tools/list exposing list_workspaces because Claude Desktop's "
        f"client-side capability filter does NOT apply to server-to-server "
        f"MCP clients (see cornerstone_mcp_surface_depends_on_client)."
    )

    # Spot-check that the rest of the surface is full-strength for M2M callers.
    # If this count drops sharply, it suggests capability-gating has leaked
    # into the server itself — that would be a regression.
    assert len(tool_names) >= 15, (
        f"Only {len(tool_names)} tools advertised — expected ~22 for the "
        f"full M2M surface. Capability-gating may have leaked into the server."
    )


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "default",
    },
)
@patch("tools.workspace._client")
async def test_list_workspaces_returns_cookbook_consumable_shape(mock_client_ctx):
    """Invoke list_workspaces via mcp.call_tool — the same code path an M2M MCP
    client exercises — and assert the response carries every field Cookbook
    needs to build its scope-filter WHERE clause.
    """
    mock_client_ctx.return_value = _mock_http_client_returning(
        _BACKEND_WORKSPACES_PAYLOAD
    )

    import server  # noqa: F401 — side-effect import registers all tools
    from core import mcp

    result = await mcp.call_tool("list_workspaces", {})

    # FastMCP's call_tool returns a sequence of content blocks. Collapse to
    # a single text body for assertion.
    body = _extract_text(result)

    # Cookbook resolves scope by: (slug, access_level) per accessible namespace.
    # Every workspace in the backend payload MUST appear in the response, and
    # each must carry its access_level so Cookbook can filter write scopes.
    for ws in _BACKEND_WORKSPACES_PAYLOAD["workspaces"]:
        assert ws["name"] in body, (
            f"Workspace slug '{ws['name']}' missing from list_workspaces output. "
            f"Cookbook needs every accessible namespace in the response to "
            f"build its scope WHERE clause."
        )
        assert ws["access_level"] in body, (
            f"Access level '{ws['access_level']}' missing from list_workspaces "
            f"output for '{ws['name']}'. Cookbook uses grant level to "
            f"distinguish write-capable scopes from read-only ones."
        )

    # Backend was hit exactly once at the expected path — proves the tool
    # routes through /connection/workspaces and is not a stub.
    # Unwrap the mock context: the HTTP GET happens on the client yielded
    # by __enter__, not on the context manager itself.
    inner_client = mock_client_ctx.return_value.__enter__.return_value
    inner_client.get.assert_called_once_with("/connection/workspaces")


def _extract_text(call_tool_result) -> str:
    """FastMCP call_tool can return either a list of content blocks or a
    (content, structured) tuple depending on version. Normalise to a single
    string so the assertions above don't care about that plumbing."""
    if isinstance(call_tool_result, tuple):
        call_tool_result = call_tool_result[0]

    parts: list[str] = []
    for block in call_tool_result or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "\n".join(parts)


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_list_workspaces_advertised_in_tools_manifest())
    asyncio.run(test_list_workspaces_returns_cookbook_consumable_shape())
    print("list_workspaces M2M contract tests passed.")
