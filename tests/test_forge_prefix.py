"""Test that remember("Forge: ...") routes to the Forge queue with correct tags."""

import os
import sys
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _make_mock_response():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    return resp


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "test-workspace",
    },
)
@patch("tools.memory._client")
@patch("tools.memory._resolve_tool_namespace", return_value="test-workspace")
def test_forge_prefix_routes_correctly(mock_ns, mock_client_ctx):
    """remember('Forge: our Monday reporting takes 2 hours') should post a note
    with forge-opportunity tags, not a regular note or fact."""

    mock_client = MagicMock()
    mock_client.post.return_value = _make_mock_response()
    mock_client_ctx.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_client_ctx.return_value.__exit__ = MagicMock(return_value=False)

    from tools.memory import remember

    result = remember("Forge: our Monday reporting takes 2 hours and nobody owns it")

    # Verify the note was posted
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args

    # Correct endpoint
    assert call_args[0][0] == "/memory/note"

    # Correct tags
    payload = call_args[1]["json"]
    assert payload["tags"] == ["forge-opportunity", "ai-ops-request", "pending"]

    # Content includes the description (prefix stripped)
    assert "our Monday reporting takes 2 hours" in payload["content"]
    assert "AI Ops Opportunity:" in payload["content"]

    # Namespace passed through
    assert payload["namespace"] == "test-workspace"

    # Return message confirms Forge routing
    assert "Forge opportunity" in result


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "test-workspace",
    },
)
@patch("tools.memory._client")
@patch("tools.memory._resolve_tool_namespace", return_value="test-workspace")
def test_forge_prefix_case_insensitive(mock_ns, mock_client_ctx):
    """FORGE:, forge:, Forge: should all trigger the prefix detection."""

    mock_client = MagicMock()
    mock_client.post.return_value = _make_mock_response()
    mock_client_ctx.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_client_ctx.return_value.__exit__ = MagicMock(return_value=False)

    from tools.memory import remember

    for prefix in ["Forge:", "forge:", "FORGE:", "FoRgE:"]:
        mock_client.reset_mock()
        result = remember(f"{prefix} test description")
        assert "Forge opportunity" in result, f"Failed for prefix: {prefix}"
        payload = mock_client.post.call_args[1]["json"]
        assert payload["tags"] == ["forge-opportunity", "ai-ops-request", "pending"]


@patch.dict(
    os.environ,
    {
        "CORNERSTONE_URL": "http://localhost:8000",
        "CORNERSTONE_API_KEY": "test-key",
        "CORNERSTONE_NAMESPACE": "test-workspace",
    },
)
@patch(
    "tools.memory._classify_memory", return_value=("note", {"content": "normal note"})
)
@patch("tools.memory._client")
@patch("tools.memory._resolve_tool_namespace", return_value="test-workspace")
def test_non_forge_content_routes_normally(mock_ns, mock_client_ctx, mock_classify):
    """Content without forge: prefix should route through normal classification."""

    mock_client = MagicMock()
    mock_client.post.return_value = _make_mock_response()
    mock_client_ctx.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_client_ctx.return_value.__exit__ = MagicMock(return_value=False)

    from tools.memory import remember

    result = remember("The client budget is $50,000")

    payload = mock_client.post.call_args[1]["json"]
    assert payload["tags"] == ["remember"]  # Normal note tags
    assert "Forge opportunity" not in result


if __name__ == "__main__":
    test_forge_prefix_routes_correctly()
    test_forge_prefix_case_insensitive()
    test_non_forge_content_routes_normally()
    print("All Forge prefix tests passed.")
