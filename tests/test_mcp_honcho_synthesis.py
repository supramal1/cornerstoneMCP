"""Synthesis-layer tests for Honcho ABOUT block + trailer in MCP tools.

Key discipline: retrieval output (``stats.used_memory``) is rendered
byte-identically regardless of peer_card. Honcho only contributes a
prepended ABOUT block and an appended trailer — never reorders or
filters the memory items. The ``test_two_user_synthesis_isolation``
case is the load-bearing proof of that separation.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _mock_client_ctx(response_payload: dict):
    """Return a patch target-compatible MagicMock for tools.memory._client."""
    client = MagicMock()
    client.post.return_value = _mock_response(response_payload)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=client)
    ctx.__exit__ = MagicMock(return_value=False)
    outer = MagicMock(return_value=ctx)
    return outer, client


# ---------------------------------------------------------------------------
# get_context
# ---------------------------------------------------------------------------


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "test-workspace",
    },
)
@patch("tools.memory._resolve_tool_namespace", return_value="test-workspace")
@patch("tools.memory._client")
def test_get_context_renders_about_and_trailer(mock_client, mock_ns):
    about_text = "Works in media innovation. Prefers terse technical responses."
    backend_context = (
        "=== ABOUT THE PERSON ASKING ===\n"
        f"{about_text}\n"
        "=== END ABOUT THE PERSON ASKING ===\n"
        "\nNike Q3 budget approved at 200k."
    )
    payload = {
        "context": backend_context,
        "context_request_id": "ctx-123",
        "stats": {
            "total_tokens": 42,
            "used_memory": [{"source_type": "fact", "preview": "Nike Q3..."}],
            "peer_card": {
                "present": True,
                "length": len(about_text),
                "content": about_text,
                "peer_id": "user_malik",
                "refreshed_at": 1_700_000_000.0,
            },
        },
    }
    outer, _ = _mock_client_ctx(payload)
    mock_client.side_effect = outer

    from tools.memory import get_context

    out = get_context("what's the nike budget?")

    # ABOUT block flows through backend-assembled context_text
    assert "=== ABOUT THE PERSON ASKING ===" in out
    assert about_text in out
    # Trailer appended
    assert "Honcho peer: user_malik" in out
    assert "summary refreshed" in out


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "test-workspace",
    },
)
@patch("tools.memory._resolve_tool_namespace", return_value="test-workspace")
@patch("tools.memory._client")
def test_get_context_honcho_unavailable_renders_cleanly(mock_client, mock_ns):
    """When Honcho timed out, backend returns peer_card with no peer_id.
    Response must render cleanly — no trailer, no ABOUT block, no crash."""
    payload = {
        "context": "Nike Q3 budget approved at 200k.",
        "context_request_id": "ctx-456",
        "stats": {
            "total_tokens": 10,
            "used_memory": [{"source_type": "fact", "preview": "Nike Q3..."}],
            "peer_card": {
                "present": False,
                "peer_id": None,
                "refreshed_at": None,
            },
        },
    }
    outer, _ = _mock_client_ctx(payload)
    mock_client.side_effect = outer

    from tools.memory import get_context

    out = get_context("nike budget")

    assert "Honcho peer:" not in out
    assert "ABOUT THE PERSON ASKING" not in out
    assert "Nike Q3 budget approved" in out


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "test-workspace",
    },
)
@patch("tools.memory._resolve_tool_namespace", return_value="test-workspace")
@patch("tools.memory._client")
def test_search_prepends_about_and_appends_trailer(mock_client, mock_ns):
    about_text = "Innovation Lead at EssenceMediacom. WPP/GroupM context."
    payload = {
        "context": "(not used by search)",
        "stats": {
            "used_memory": [
                {
                    "source_type": "fact",
                    "preview": "Nike Q3 2026 budget: 200k",
                    "timestamp": "2026-04-19T10:00:00Z",
                    "score": 0.91,
                },
                {
                    "source_type": "note",
                    "preview": "Meeting notes from QBR",
                    "timestamp": "2026-04-15T10:00:00Z",
                    "score": 0.72,
                },
            ],
            "peer_card": {
                "present": True,
                "length": len(about_text),
                "content": about_text,
                "peer_id": "user_malik",
                "refreshed_at": 1_700_000_000.0,
            },
        },
    }
    outer, _ = _mock_client_ctx(payload)
    mock_client.side_effect = outer

    from tools.memory import search

    out = search("nike budget")
    lines = out.split("\n")

    # Namespace marker stays first
    assert lines[0] == "[workspace: test-workspace]"
    # ABOUT block comes after the namespace marker, before grouped sections
    assert "=== ABOUT THE PERSON ASKING ===" in out
    assert about_text in out
    about_idx = next(
        i for i, line in enumerate(lines) if "ABOUT THE PERSON ASKING" in line
    )
    facts_idx = next(i for i, line in enumerate(lines) if line.startswith("## Facts"))
    assert about_idx < facts_idx
    # Retrieval items still rendered
    assert "Nike Q3 2026 budget: 200k" in out
    assert "Meeting notes from QBR" in out
    # Trailer appended at end
    assert lines[-1].startswith("Honcho peer: user_malik")


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "test-workspace",
    },
)
@patch("tools.memory._resolve_tool_namespace", return_value="test-workspace")
@patch("tools.memory._client")
def test_search_honcho_unavailable_renders_cleanly(mock_client, mock_ns):
    payload = {
        "context": "",
        "stats": {
            "used_memory": [
                {
                    "source_type": "fact",
                    "preview": "Nike Q3 2026 budget: 200k",
                    "timestamp": "2026-04-19",
                    "score": 0.91,
                }
            ],
            "peer_card": {"present": False, "peer_id": None, "refreshed_at": None},
        },
    }
    outer, _ = _mock_client_ctx(payload)
    mock_client.side_effect = outer

    from tools.memory import search

    out = search("nike budget")

    assert "Honcho peer:" not in out
    assert "ABOUT THE PERSON ASKING" not in out
    assert "Nike Q3 2026 budget: 200k" in out
    assert out.startswith("[workspace: test-workspace]")


# ---------------------------------------------------------------------------
# Two-user synthesis isolation proof
# ---------------------------------------------------------------------------


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "test-workspace",
    },
)
@patch("tools.memory._resolve_tool_namespace", return_value="test-workspace")
@patch("tools.memory._client")
def test_two_user_synthesis_isolation(mock_client, mock_ns):
    """Core contract: Honcho personalisation is synthesis-layer only.

    Two different peers hitting the same query get different ABOUT
    blocks and different trailer peer_ids — but the retrieval output
    (the grouped ``## Facts`` / ``## Notes`` sections) must be
    byte-identical. If this test fails, personalisation has leaked
    into retrieval ranking or filtering.
    """
    retrieval_items = [
        {
            "source_type": "fact",
            "preview": "Nike Q3 2026 budget: 200k",
            "timestamp": "2026-04-19",
            "score": 0.91,
        },
        {
            "source_type": "note",
            "preview": "QBR meeting notes",
            "timestamp": "2026-04-15",
            "score": 0.72,
        },
    ]

    def _payload(peer_id: str, about: str) -> dict:
        return {
            "context": "",
            "stats": {
                "used_memory": retrieval_items,
                "peer_card": {
                    "present": True,
                    "length": len(about),
                    "content": about,
                    "peer_id": peer_id,
                    "refreshed_at": 1_700_000_000.0,
                },
            },
        }

    user_a_payload = _payload(
        "user_malik", "Innovation Lead at EssenceMediacom, terse style."
    )
    user_b_payload = _payload(
        "user_kim", "Managing Director at Charlie Oscar, narrative style."
    )

    from tools.memory import search

    # User A
    outer_a, _ = _mock_client_ctx(user_a_payload)
    mock_client.side_effect = outer_a
    out_a = search("nike budget")

    # User B
    outer_b, _ = _mock_client_ctx(user_b_payload)
    mock_client.side_effect = outer_b
    out_b = search("nike budget")

    # Different ABOUT blocks
    assert "Innovation Lead" in out_a
    assert "Managing Director" in out_b
    assert "Innovation Lead" not in out_b
    assert "Managing Director" not in out_a
    # Different trailer peer_ids
    assert "Honcho peer: user_malik" in out_a
    assert "Honcho peer: user_kim" in out_b

    # Byte-identical retrieval rendering: strip Honcho wrapping and compare.
    def _retrieval_slice(rendered: str) -> str:
        lines = rendered.split("\n")
        # Drop the namespace marker (first line)
        lines = lines[1:]
        # Drop the ABOUT block if present
        if lines and lines[0].startswith("==="):
            close_idx = next(
                i for i, line in enumerate(lines) if "END ABOUT THE PERSON ASKING" in line
            )
            lines = lines[close_idx + 1 :]
        # Drop the trailer (last line) if present
        if lines and lines[-1].startswith("Honcho peer:"):
            lines = lines[:-1]
        return "\n".join(lines)

    assert _retrieval_slice(out_a) == _retrieval_slice(out_b)
