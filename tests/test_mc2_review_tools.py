"""Tests for the MC-2 proposal review tools (tools/proposals.py).

Contract under test:

1. mc_list_proposals renders the full review surface — id, key, FULL
   value, citation link + locator, SUPERSEDES framing — plus the count
   and the promote/reject hint line.
2. mc_promote_facts relays the inbound X-Forwarded-User-Email header on
   the outbound API call when present, and sends NO identity header when
   absent (stdio/dev). The email is never invented or defaulted.
3. mc_reject_facts refuses the whole call client-side when any item is
   missing a non-empty reason — naming the offending ids, without
   touching the API. Reasons are eval feedstock and must be deliberate.
4. mc_reject_facts relays the verbatim reasons in the request body and
   echoes per-id outcomes including the recorded reason.
5. Per-id outcomes (promoted/conflict/error + detail) are echoed exactly
   as returned by the API.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ENV = {
    "CORNERSTONE_URL": "http://localhost:8000",
    "CORNERSTONE_API_KEY": "test-key",
    "CORNERSTONE_NAMESPACE": "default",
}

_FORWARDED_HEADER = "X-Forwarded-User-Email"
_REVIEWER_EMAIL = "kim@charlieoscar.co.uk"

# Fixture: a realistic /memory/fact/proposals payload — one superseding
# proposal and one brand-new fact, with full citation blocks.
_PROPOSALS_PAYLOAD = {
    "namespace": "default",
    "status": "proposed",
    "proposals": [
        {
            "id": "dp-a1b2c3d4e5",
            "namespace": "default",
            "key": "acme_retainer_value",
            "value": "Acme retainer is £42k/month from July 2026, up from £35k.",
            "category": "commercial",
            "confidence": 0.92,
            "run_id": "run-001",
            "citation": {
                "doc_id": "doc-123",
                "link": "https://docs.google.com/document/d/doc-123",
                "modified_time": "2026-06-10T09:00:00Z",
                "locator": "Section 2, 'Commercials'",
            },
            "supersedes": {
                "existing_key": "acme_retainer_value",
                "existing_value_snapshot": "Acme retainer is £35k/month.",
                "framing": "Retainer uplift agreed at the June QBR",
            },
            "status": "proposed",
            "created_at": "2026-06-10T10:00:00Z",
        },
        {
            "id": "dp-f6e5d4c3b2",
            "namespace": "default",
            "key": "acme_lead_contact",
            "value": "Acme day-to-day lead is Priya Shah (Marketing Director).",
            "category": "people",
            "confidence": 0.88,
            "run_id": "run-001",
            "citation": {
                "doc_id": "doc-456",
                "link": "https://docs.google.com/document/d/doc-456",
                "modified_time": "2026-06-09T15:30:00Z",
                "locator": "Attendees list",
            },
            "supersedes": None,
            "status": "proposed",
            "created_at": "2026-06-10T10:00:00Z",
        },
    ],
}


def _mock_http_client(get_payload: dict | None = None, post_payload: dict | None = None) -> MagicMock:
    """Build a mock context-managed httpx client.

    .get() returns get_payload, .post() returns post_payload — same shape
    as the _mock_http_client_returning helper in test_list_workspaces_m2m.
    """

    def _response(payload):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value=payload)
        return response

    client = MagicMock()
    client.get = MagicMock(return_value=_response(get_payload or {}))
    client.post = MagicMock(return_value=_response(post_payload or {}))

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=client)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _http_request_context(headers: dict[str, str]) -> SimpleNamespace:
    """Fake FastMCP Context carrying a Starlette-shaped inbound request."""
    return SimpleNamespace(
        request_context=SimpleNamespace(request=SimpleNamespace(headers=headers))
    )


def _extract_text(call_tool_result) -> str:
    """Normalise FastMCP call_tool output (list of blocks or tuple) to text."""
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


# ---------------------------------------------------------------------------
# mc_list_proposals
# ---------------------------------------------------------------------------


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_list_proposals_renders_full_review_surface(mock_client_ctx):
    """The listing IS the review surface: id, key, FULL value, citation
    link + locator, and the SUPERSEDES framing must all be present."""
    mock_client_ctx.return_value = _mock_http_client(get_payload=_PROPOSALS_PAYLOAD)

    import server  # noqa: F401 — side-effect import registers all tools
    from core import mcp

    result = await mcp.call_tool("mc_list_proposals", {})
    body = _extract_text(result)

    for p in _PROPOSALS_PAYLOAD["proposals"]:
        assert p["id"] in body, f"Proposal id {p['id']} missing from listing"
        assert p["key"] in body, f"Proposal key {p['key']} missing from listing"
        # FULL value — the reviewer decides from this text, no truncation.
        assert p["value"] in body, f"Full value missing for {p['id']}"
        assert p["citation"]["link"] in body, f"Citation link missing for {p['id']}"
        assert p["citation"]["locator"] in body, f"Citation locator missing for {p['id']}"

    # Supersession framing must be visible for the superseding proposal.
    assert "SUPERSEDES" in body
    assert "Retainer uplift agreed at the June QBR" in body
    assert "acme_retainer_value" in body

    # Count and the closing hint line.
    assert "2 'proposed' proposal(s)" in body
    assert (
        "Promote: mc_promote_facts(ids). "
        "Reject: mc_reject_facts(items=[{id, reason}]) — "
        "reason required, stored verbatim." in body
    )

    inner_client = mock_client_ctx.return_value.__enter__.return_value
    inner_client.get.assert_called_once()
    args, kwargs = inner_client.get.call_args
    assert args[0] == "/memory/fact/proposals"
    assert kwargs["params"]["status"] == "proposed"


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_list_proposals_empty(mock_client_ctx):
    mock_client_ctx.return_value = _mock_http_client(
        get_payload={"proposals": [], "namespace": "default", "status": "proposed"}
    )

    import server  # noqa: F401
    from core import mcp

    body = _extract_text(await mcp.call_tool("mc_list_proposals", {}))
    assert "No 'proposed' proposals" in body


# ---------------------------------------------------------------------------
# mc_promote_facts — identity forwarding
# ---------------------------------------------------------------------------

_PROMOTE_PAYLOAD = {
    "results": [
        {"id": "dp-a1b2c3d4e5", "outcome": "promoted"},
        {"id": "dp-f6e5d4c3b2", "outcome": "conflict", "detail": "key changed since proposal"},
    ]
}


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_promote_forwards_identity_header_when_present(mock_client_ctx):
    """When the gateway injected X-Forwarded-User-Email on the inbound MCP
    request, the same header must be relayed on the outbound API call."""
    mock_client_ctx.return_value = _mock_http_client(post_payload=_PROMOTE_PAYLOAD)

    import server  # noqa: F401
    import tools.proposals as proposals
    from core import mcp

    fake_ctx = _http_request_context({_FORWARDED_HEADER: _REVIEWER_EMAIL})
    with patch.object(proposals.mcp, "get_context", return_value=fake_ctx):
        result = await mcp.call_tool(
            "mc_promote_facts", {"ids": ["dp-a1b2c3d4e5", "dp-f6e5d4c3b2"]}
        )
    body = _extract_text(result)

    inner_client = mock_client_ctx.return_value.__enter__.return_value
    inner_client.post.assert_called_once()
    args, kwargs = inner_client.post.call_args
    assert args[0] == "/memory/fact/promote"
    assert kwargs["json"] == {"ids": ["dp-a1b2c3d4e5", "dp-f6e5d4c3b2"]}
    assert kwargs["headers"] == {_FORWARDED_HEADER: _REVIEWER_EMAIL}, (
        "Inbound X-Forwarded-User-Email must be relayed verbatim on the "
        "outbound promote call — it is the decision identity."
    )

    # Per-id outcomes echoed exactly as returned, including detail.
    assert "dp-a1b2c3d4e5: promoted" in body
    assert "dp-f6e5d4c3b2: conflict — key changed since proposal" in body


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_promote_omits_identity_header_when_absent(mock_client_ctx):
    """Without an inbound HTTP request (stdio/dev) NO identity header is
    sent — the API records the decision without a forwarded identity.
    The email must never be invented or defaulted."""
    mock_client_ctx.return_value = _mock_http_client(post_payload=_PROMOTE_PAYLOAD)

    import server  # noqa: F401
    from core import mcp

    # No patching of get_context: outside a live HTTP request the SDK has
    # no request context, which is exactly the stdio/dev condition.
    await mcp.call_tool("mc_promote_facts", {"ids": ["dp-a1b2c3d4e5"]})

    inner_client = mock_client_ctx.return_value.__enter__.return_value
    inner_client.post.assert_called_once()
    _, kwargs = inner_client.post.call_args
    assert _FORWARDED_HEADER not in kwargs["headers"], (
        "No inbound identity header — outbound call must not carry one."
    )
    assert kwargs["headers"] == {}


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_promote_ignores_blank_inbound_header(mock_client_ctx):
    """A whitespace-only inbound header value is treated as absent."""
    mock_client_ctx.return_value = _mock_http_client(post_payload=_PROMOTE_PAYLOAD)

    import server  # noqa: F401
    import tools.proposals as proposals
    from core import mcp

    fake_ctx = _http_request_context({_FORWARDED_HEADER: "   "})
    with patch.object(proposals.mcp, "get_context", return_value=fake_ctx):
        await mcp.call_tool("mc_promote_facts", {"ids": ["dp-a1b2c3d4e5"]})

    inner_client = mock_client_ctx.return_value.__enter__.return_value
    _, kwargs = inner_client.post.call_args
    assert kwargs["headers"] == {}


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_promote_refuses_empty_ids(mock_client_ctx):
    import server  # noqa: F401
    from core import mcp

    body = _extract_text(await mcp.call_tool("mc_promote_facts", {"ids": []}))
    assert "Error" in body
    mock_client_ctx.assert_not_called()


# ---------------------------------------------------------------------------
# mc_reject_facts — client-side reason validation + verbatim relay
# ---------------------------------------------------------------------------

_REJECT_PAYLOAD = {
    "results": [
        {
            "id": "dp-a1b2c3d4e5",
            "outcome": "rejected",
            "detail": "Stale figure — superseded by the signed SOW",
        },
        {"id": "dp-f6e5d4c3b2", "outcome": "error", "detail": "not found"},
    ]
}


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_reject_refuses_missing_reason_without_calling_api(mock_client_ctx):
    """Missing/empty reasons refuse the WHOLE call before any API traffic,
    naming the offending ids. Reasons are eval feedstock — they must be
    deliberate, not defaulted."""
    import server  # noqa: F401
    from core import mcp

    items = [
        {"id": "dp-a1b2c3d4e5", "reason": "Stale figure"},
        {"id": "dp-f6e5d4c3b2", "reason": "   "},  # whitespace-only = empty
        {"id": "dp-0000000000"},  # reason key absent
    ]
    body = _extract_text(await mcp.call_tool("mc_reject_facts", {"items": items}))

    assert "Error" in body
    assert "dp-f6e5d4c3b2" in body, "Offending id (empty reason) must be named"
    assert "dp-0000000000" in body, "Offending id (missing reason) must be named"
    assert "dp-a1b2c3d4e5" not in body.split("Missing/empty reason for:")[-1].split(".")[0], (
        "Items WITH a reason must not be listed as offenders"
    )
    mock_client_ctx.assert_not_called()


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_reject_refuses_empty_items(mock_client_ctx):
    import server  # noqa: F401
    from core import mcp

    body = _extract_text(await mcp.call_tool("mc_reject_facts", {"items": []}))
    assert "Error" in body
    mock_client_ctx.assert_not_called()


@patch.dict(os.environ, _ENV)
@patch("tools.proposals._client")
async def test_reject_relays_verbatim_reasons_and_forwards_identity(mock_client_ctx):
    """Reasons go to the API verbatim, the identity header is relayed, and
    per-id outcomes (including the recorded reason) are echoed back."""
    mock_client_ctx.return_value = _mock_http_client(post_payload=_REJECT_PAYLOAD)

    import server  # noqa: F401
    import tools.proposals as proposals
    from core import mcp

    items = [
        {"id": "dp-a1b2c3d4e5", "reason": "Stale figure — superseded by the signed SOW"},
        {"id": "dp-f6e5d4c3b2", "reason": "Wrong person — Priya left in May"},
    ]
    fake_ctx = _http_request_context({_FORWARDED_HEADER: _REVIEWER_EMAIL})
    with patch.object(proposals.mcp, "get_context", return_value=fake_ctx):
        result = await mcp.call_tool("mc_reject_facts", {"items": items})
    body = _extract_text(result)

    inner_client = mock_client_ctx.return_value.__enter__.return_value
    inner_client.post.assert_called_once()
    args, kwargs = inner_client.post.call_args
    assert args[0] == "/memory/fact/reject"
    assert kwargs["json"] == {"items": items}, (
        "Reasons must reach the API verbatim — no rewriting, no trimming."
    )
    assert kwargs["headers"] == {_FORWARDED_HEADER: _REVIEWER_EMAIL}

    # Per-id outcomes echoed, with the recorded reason on rejections.
    assert "dp-a1b2c3d4e5: rejected — reason recorded: " in body
    assert "Stale figure — superseded by the signed SOW" in body
    assert "dp-f6e5d4c3b2: error — not found" in body


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@patch.dict(os.environ, _ENV)
async def test_mc2_tools_advertised_in_manifest():
    import server  # noqa: F401
    from core import mcp

    tool_names = {t.name for t in await mcp.list_tools()}
    for name in ("mc_list_proposals", "mc_promote_facts", "mc_reject_facts"):
        assert name in tool_names, f"{name} missing from MCP tools/list manifest"
