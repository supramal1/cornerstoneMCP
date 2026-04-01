"""
Cornerstone MCP Server.

Exposes Cornerstone memory API as MCP tools for agent integrations
(Claude Code, Codex, etc).

Run:
    python server.py                          # stdio mode (default)
    python server.py --transport http --port 3100  # HTTP mode

Requires: pip install mcp httpx
"""

from __future__ import annotations

import argparse
import logging

# Import core to initialise the MCP server, config, and shared state.
from core import _ws, logger, mcp

# Import tool modules so their @mcp.tool() decorators register with the
# FastMCP instance.  The imports are side-effect-only — the modules don't
# need to export anything back to server.py.
import tools.memory  # noqa: F401 — remember, recall, forget, get_context, add_fact, add_note, search
import tools.workspace  # noqa: F401 — list/get/switch/set_default workspace
import tools.retrieval  # noqa: F401 — list_facts, list_notes, get_recent_sessions, list_threads, report_context_feedback
import tools.sessions  # noqa: F401 — save_conversation


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cornerstone MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=3100)
    args = parser.parse_args()

    logger.info(
        "Active workspace: %s (default: %s, auth: %s, principal: %s)",
        _ws.active_workspace or "(none)",
        _ws.default_workspace or "(none)",
        _ws.principal_type,
        _ws.principal_id or "n/a",
    )
    logger.info(
        "Governance: %s", "active" if _ws._is_governed else "inactive (shared-key)"
    )
    if _ws._is_governed and not _ws.active_workspace:
        available = [ws["name"] for ws in _ws._available_workspaces]
        if available:
            logger.warning("No workspace selected. Available: %s", ", ".join(available))
        else:
            logger.warning("No workspace selected and no workspace grants found.")

    if args.transport == "http":
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = args.port
        logger.info("Starting Cornerstone MCP server on HTTP port %d", args.port)
        mcp.run(transport="streamable-http")
    else:
        logger.info("Starting Cornerstone MCP server on stdio")
        mcp.run(transport="stdio")
