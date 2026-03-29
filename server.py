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
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cornerstone.mcp")

# ---------------------------------------------------------------------------
# Configuration — from environment variables
# ---------------------------------------------------------------------------

CORNERSTONE_URL = os.environ.get("CORNERSTONE_URL", "http://127.0.0.1:8000")
CORNERSTONE_API_KEY = os.environ.get(
    "CORNERSTONE_API_KEY", os.environ.get("MEMORY_API_KEY", "")
)
DEFAULT_NAMESPACE = os.environ.get("CORNERSTONE_NAMESPACE", "default")
DEFAULT_AGENT_ID = os.environ.get("CORNERSTONE_AGENT_ID", "openclaw")

_SETTINGS_PATH = Path.home() / ".cornerstone" / "settings.json"


# ---------------------------------------------------------------------------
# Client detection
# ---------------------------------------------------------------------------


def _detect_client() -> str:
    """Detect which MCP client is running this server."""
    # Claude Code sets specific env vars and runs via stdio
    if os.environ.get("CLAUDE_CODE") or os.environ.get("CLAUDE_CODE_VERSION"):
        return "claude-code"
    # Check parent process name as fallback
    try:
        import psutil

        parent = psutil.Process(os.getpid()).parent()
        if parent:
            pname = parent.name().lower()
            if "claude" in pname:
                return "claude-code"
            if "codex" in pname:
                return "codex"
    except Exception:
        pass
    # Check for common env markers
    if os.environ.get("CODEX_CLI"):
        return "codex"
    if os.environ.get("CURSOR_SESSION"):
        return "cursor"
    # Default based on transport
    return "mcp-client"


# ---------------------------------------------------------------------------
# Session buffer — fire-and-forget event recording
# ---------------------------------------------------------------------------


class SessionBuffer:
    """Fire-and-forget session event recording to the backend buffer."""

    def __init__(self, api_url: str, api_key: str, client_name: str = "unknown"):
        self.api_url = api_url
        self.api_key = api_key
        self.client_name = client_name
        self.current_session_id: str | None = None
        self._lock = threading.Lock()

    def record(
        self, tool_name: str, tool_params: dict = None, result_summary: str = None
    ):
        with self._lock:
            session_id = self.current_session_id

        def _send():
            try:
                response = httpx.post(
                    f"{self.api_url}/session-buffer/event",
                    headers={
                        "X-API-Key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "session_id": session_id,
                        "tool_name": tool_name,
                        "tool_params": _truncate_params(tool_params),
                        "tool_result_summary": (result_summary or "")[:300],
                        "client_name": self.client_name,
                    },
                    timeout=5,
                )
                if response.status_code == 200:
                    data = response.json()
                    with self._lock:
                        self.current_session_id = data.get("session_id")
                else:
                    logger.warning(
                        f"Session buffer event failed: {response.status_code}"
                    )
            except Exception as e:
                logger.debug(f"Session buffer event failed (non-critical): {e}")

        threading.Thread(target=_send, daemon=True).start()

    def reset(self):
        with self._lock:
            self.current_session_id = None


def _truncate_params(params: dict | None) -> dict:
    if not params:
        return {}
    result = {}
    for k, v in params.items():
        if k in ("api_key", "key", "password", "secret", "token"):
            continue
        if isinstance(v, str) and len(v) > 200:
            result[k] = v[:200] + "..."
        else:
            result[k] = v
    return result


session_buffer = SessionBuffer(
    api_url=CORNERSTONE_URL,
    api_key=CORNERSTONE_API_KEY,
    client_name=_detect_client(),
)


# ---------------------------------------------------------------------------
# Workspace session state
# ---------------------------------------------------------------------------


class WorkspaceState:
    """Session-level workspace state.

    active_workspace:  current workspace for this session (changed by switch_workspace)
    default_workspace: persisted default (changed by set_default_workspace)

    At startup, if the API key belongs to a governed principal (not the shared
    superuser key), we attempt to resolve the granted workspace automatically.
    This ensures scoped principals never fall back to the hardcoded "default"
    namespace.
    """

    def __init__(self):
        self.default_workspace: str = self._load_default()
        self.active_workspace: str = self.default_workspace
        self.principal_type: str = "unknown"  # "shared-key", "principal", or "unknown"
        self.principal_id: Optional[str] = None
        self._is_governed: bool = False
        self._available_workspaces: list[dict] = []

        # Attempt grant-aware workspace resolution at startup
        self._resolve_principal_workspace()

    def _load_default(self) -> str:
        """Load default workspace from settings file, falling back to env var."""
        try:
            if _SETTINGS_PATH.exists():
                data = json.loads(_SETTINGS_PATH.read_text())
                if data.get("default_workspace"):
                    return data["default_workspace"]
        except Exception:
            pass
        return DEFAULT_NAMESPACE

    def _resolve_principal_workspace(self) -> None:
        """Resolve workspace from backend grants at startup.

        For scoped principal keys:
        - Calls POST /connection/verify to determine auth type
        - If principal-based auth, calls GET /connection/workspaces
        - If exactly 1 granted workspace, auto-sets active_workspace to it
        - If multiple, logs a warning and keeps the static default

        For shared superuser keys:
        - Keeps current behavior (static default is fine)

        Fault-tolerant: if the API is unreachable, falls back to static
        default with a warning log.
        """
        if not CORNERSTONE_API_KEY:
            logger.warning(
                "No API key configured — skipping workspace resolution, "
                "using static default: %s",
                self.active_workspace,
            )
            return

        headers = {"Content-Type": "application/json"}
        if CORNERSTONE_API_KEY:
            headers["X-API-Key"] = CORNERSTONE_API_KEY

        try:
            with httpx.Client(
                base_url=CORNERSTONE_URL, headers=headers, timeout=10
            ) as client:
                # Step 1: Check principal type
                verify_resp = client.post("/connection/verify")
                verify_resp.raise_for_status()
                verify_data = verify_resp.json()

                auth_type = verify_data.get("auth_type", "")
                self.principal_id = verify_data.get("principal_id")
                self.principal_type = auth_type

                if auth_type == "shared-key":
                    self._is_governed = False
                    logger.info(
                        "Superuser (shared-key) auth detected — "
                        "keeping static workspace: %s",
                        self.active_workspace,
                    )
                    return

                if auth_type != "principal":
                    self._is_governed = False
                    logger.info(
                        "Auth type '%s' — keeping static workspace: %s",
                        auth_type,
                        self.active_workspace,
                    )
                    return

                # Principal auth — governance active
                self._is_governed = True

                # Step 2: For principal-based auth, fetch granted workspaces
                ws_resp = client.get("/connection/workspaces")
                ws_resp.raise_for_status()
                ws_data = ws_resp.json()

                workspaces = ws_data.get("workspaces", [])
                # Filter to active (non-archived, non-deleted) workspaces
                active_ws = [
                    ws
                    for ws in workspaces
                    if ws.get("status", "active") not in ("archived", "deleted")
                ]

                if len(active_ws) == 0:
                    self._available_workspaces = []
                    self.active_workspace = ""
                    self.default_workspace = ""
                    logger.warning(
                        "Principal %s has no active workspace grants — "
                        "no workspace selected",
                        self.principal_id,
                    )
                    return

                if len(active_ws) == 1:
                    self._available_workspaces = active_ws
                    granted_name = active_ws[0]["name"]
                    old = self.active_workspace
                    self.active_workspace = granted_name
                    self.default_workspace = granted_name
                    logger.info(
                        "Auto-resolved workspace from grant: %s -> %s (principal: %s)",
                        old,
                        granted_name,
                        self.principal_id,
                    )
                    return

                # Multiple workspaces — can't auto-select
                self._available_workspaces = active_ws
                self.active_workspace = ""
                ws_names = [ws["name"] for ws in active_ws]
                logger.warning(
                    "Principal %s has %d granted workspaces: %s — "
                    "no workspace selected. "
                    "Use switch_workspace() to select one.",
                    self.principal_id,
                    len(active_ws),
                    ", ".join(ws_names),
                )

        except httpx.ConnectError as e:
            logger.warning(
                "Cannot reach Cornerstone API at %s for workspace resolution "
                "(connection error: %s) — using static default: %s",
                CORNERSTONE_URL,
                e,
                self.active_workspace,
            )
        except httpx.TimeoutException as e:
            logger.warning(
                "Timeout connecting to Cornerstone API at %s for workspace "
                "resolution (%s) — using static default: %s",
                CORNERSTONE_URL,
                e,
                self.active_workspace,
            )
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Cornerstone API returned %d during workspace resolution: %s — "
                "using static default: %s",
                e.response.status_code,
                e.response.text[:200],
                self.active_workspace,
            )
        except Exception as e:
            logger.warning(
                "Unexpected error during workspace resolution (%s: %s) — "
                "using static default: %s",
                type(e).__name__,
                e,
                self.active_workspace,
            )

    def _save_default(self) -> bool:
        """Persist default workspace to settings file. Returns success."""
        try:
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if _SETTINGS_PATH.exists():
                data = json.loads(_SETTINGS_PATH.read_text())
            data["default_workspace"] = self.default_workspace
            _SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n")
            return True
        except Exception as e:
            logger.warning("Failed to save default workspace: %s", e)
            return False


_ws = WorkspaceState()


def active_namespace() -> str:
    """Return the current active workspace for this session."""
    return _ws.active_workspace


def _resolve_tool_namespace(namespace: str = "") -> str:
    """Resolve the namespace for a tool call.

    This is the ONLY way tools should resolve namespace. Tools must never
    default to "default" independently.

    Resolution order:
    1. Explicit namespace argument (if provided and non-empty)
    2. Active workspace (if set)
    3. Default workspace (if set)
    4. For governed principals: auto-resolve single grant, else empty
    5. Ungoverned fallback: DEFAULT_NAMESPACE
    """
    if namespace and namespace.strip():
        return namespace.strip()
    if _ws.active_workspace:
        return _ws.active_workspace
    if _ws.default_workspace:
        return _ws.default_workspace
    if _ws._is_governed:
        available = [ws["name"] for ws in _ws._available_workspaces]
        if len(available) == 1:
            _ws.active_workspace = available[0]
            return available[0]
        return ""  # Caller must handle empty namespace
    return DEFAULT_NAMESPACE


def _no_workspace_error() -> str:
    """Return a standard error message when no workspace is selected."""
    available = [ws["name"] for ws in _ws._available_workspaces]
    return (
        "Error: no workspace selected. "
        f"Available workspaces: {', '.join(available)}. "
        "Use switch_workspace() to select one."
    )


def _format_http_error(e: httpx.HTTPStatusError, operation: str) -> str:
    """Format an HTTP error into a readable tool error message."""
    status = e.response.status_code
    try:
        body = e.response.json()
        detail = body.get("detail", body.get("message", ""))
    except Exception:
        detail = e.response.text[:300]

    if status == 403:
        return (
            f"Access denied ({operation}): {detail or 'insufficient permissions'}. "
            f"Check that this principal has the required grant for the "
            f"target workspace. Use list_workspaces() to see available workspaces."
        )
    if status == 404:
        return f"Not found ({operation}): {detail or 'resource does not exist'}."
    if status == 401:
        return f"Authentication failed ({operation}): {detail or 'invalid or expired API key'}."
    if status == 422:
        return f"Validation error ({operation}): {detail}"
    return f"HTTP {status} error ({operation}): {detail}"


# ---------------------------------------------------------------------------
# Simple API helpers (remember/recall/forget)
# ---------------------------------------------------------------------------


def _classify_memory(content: str) -> tuple[str, dict]:
    """Classify content as fact or note.

    Returns (type, metadata) where:
    - type is "fact" or "note"
    - metadata includes extracted key/value for facts
    """
    content_lower = content.strip().lower()

    # Pattern matching for fact-like content:
    # "X is Y", "X are Y", "X = Y", "X: Y"
    fact_patterns = [
        r"^(.+?)\s+(?:is|are|was|were|=)\s+(.+)$",
        r"^(.+?):\s+(.+)$",
        r"^(.+?)\s+(?:equals|costs?|has|have)\s+(.+)$",
    ]

    for pattern in fact_patterns:
        match = re.match(pattern, content, re.IGNORECASE)
        if match:
            key_raw = match.group(1).strip()
            value = match.group(2).strip()

            # Only classify as fact if the key is short and specific
            if len(key_raw.split()) <= 5:
                key = _slugify(key_raw)
                return "fact", {"key": key, "value": value, "display_key": key_raw}

    # Default to note
    return "note", {"content": content}


def _slugify(text: str) -> str:
    """Convert human text to a fact key."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _looks_like_fact_key(query: str) -> bool:
    """Check if the query looks like a fact key (snake_case, no spaces, short)."""
    return bool(re.match(r"^[a-z][a-z0-9_]*$", query)) and len(query) < 60


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "cornerstone",
    instructions=(
        "Long-term memory backed by Cornerstone. "
        "Store and retrieve facts, notes, and context. "
        "Start with remember/recall/forget for simple use. "
        "Use workspace tools (list_workspaces, get_current_workspace, "
        "switch_workspace, set_default_workspace) to manage multi-workspace sessions."
    ),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if CORNERSTONE_API_KEY:
        h["X-API-Key"] = CORNERSTONE_API_KEY
    return h


def _client() -> httpx.Client:
    return httpx.Client(base_url=CORNERSTONE_URL, headers=_headers(), timeout=30)


# ---------------------------------------------------------------------------
# Simple tools (start here)
# ---------------------------------------------------------------------------


@mcp.tool()
def remember(content: str, type: str = "auto") -> str:
    """Save something to memory. Cornerstone will remember it for future conversations.

    Use this to store anything important: facts, decisions, preferences, notes,
    meeting outcomes, project details — anything you might need later.

    Args:
        content: What to remember. Can be a fact ("Malik's email is malik@co.com"),
                 a note ("Meeting decided to go with option B"), or any text.
        type: How to store it. Options:
              - "auto" (default): Cornerstone decides based on content
              - "fact": Key-value information (e.g., "Project deadline is March 15")
              - "note": Freeform observation or note

    Examples:
        remember("The client's budget is $50,000")
        remember("Meeting decided to postpone launch to Q2")
        remember("Malik prefers morning meetings", type="fact")
    """
    ns = _resolve_tool_namespace()
    if not ns:
        return _no_workspace_error()

    if type == "auto":
        memory_type, metadata = _classify_memory(content)
    elif type == "fact":
        memory_type = "fact"
        metadata = _extract_fact(content)
    elif type == "note":
        memory_type = "note"
        metadata = {"content": content}
    else:
        return f"Unknown type '{type}'. Use 'auto', 'fact', or 'note'."

    if memory_type == "fact":
        try:
            with _client() as c:
                r = c.post(
                    "/memory/fact",
                    json={
                        "key": metadata["key"],
                        "value": metadata["value"],
                        "namespace": ns,
                        "category": "general",
                        "confidence": 0.9,
                        "agent_id": DEFAULT_AGENT_ID,
                    },
                )
                r.raise_for_status()
        except httpx.HTTPStatusError as e:
            return _format_http_error(e, "remember")
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return f"Error (remember): cannot reach Cornerstone API — {e}"

        display_key = metadata.get("display_key", metadata["key"])
        session_buffer.record(
            tool_name="remember",
            tool_params={"content": content, "type": type},
            result_summary=f"Saved fact: {display_key}",
        )
        return f"[{ns}] Remembered fact: {display_key} = {metadata['value']}"

    else:  # note
        try:
            with _client() as c:
                r = c.post(
                    "/memory/note",
                    json={
                        "content": metadata["content"],
                        "namespace": ns,
                        "tags": ["remember"],
                        "agent_id": DEFAULT_AGENT_ID,
                    },
                )
                r.raise_for_status()
        except httpx.HTTPStatusError as e:
            return _format_http_error(e, "remember")
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return f"Error (remember): cannot reach Cornerstone API — {e}"

        preview = content[:80] + "..." if len(content) > 80 else content
        session_buffer.record(
            tool_name="remember",
            tool_params={"content": content, "type": type},
            result_summary=f"Saved note: {preview[:60]}",
        )
        return f"[{ns}] Remembered note: {preview}"


def _extract_fact(content: str) -> dict:
    """Extract a fact key/value from content when type='fact' is specified."""
    metadata = _classify_memory(content)
    if metadata[0] == "fact":
        return metadata[1]
    # Fallback: use entire content as both key and value
    key = _slugify(content[:50])
    return {"key": key, "value": content, "display_key": content[:50]}


@mcp.tool()
def recall(query: str) -> str:
    """Search memory for relevant information. Use this whenever you need to
    remember something from a previous conversation or stored knowledge.

    Args:
        query: What to look for. Can be a question, a topic, a name — anything.

    Examples:
        recall("What is the client's budget?")
        recall("Google pitch")
        recall("decisions from last week")
    """
    ns = _resolve_tool_namespace()
    if not ns:
        return _no_workspace_error()

    try:
        with _client() as c:
            r = c.post("/context", json={"query": query, "namespace": ns})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "recall")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (recall): cannot reach Cornerstone API — {e}"

    context_text = data.get("context", "")
    context_request_id = data.get("context_request_id", "")
    stats = data.get("stats", {})

    if not context_text or context_text.strip() == "":
        session_buffer.record(
            tool_name="recall",
            tool_params={"query": query},
            result_summary="No relevant memories found",
        )
        return f"[{ns}] No relevant memories found for: {query}"

    result = f"[{ns}] [Context ID: {context_request_id}]\n\n{context_text}"

    total = stats.get("total_items", 0) or len(stats.get("used_memory", []))
    if total:
        result += f"\n\n({total} memories used)"

    session_buffer.record(
        tool_name="recall",
        tool_params={"query": query},
        result_summary=f"Found {total} items",
    )
    return result


@mcp.tool()
def forget(query: str, type: str = "auto", confirm: bool = False) -> str:
    """Remove something from memory. Use this to delete incorrect or outdated information.

    By default, shows what would be deleted and asks for confirmation.
    Set confirm=True to delete immediately.

    Args:
        query: What to forget. Can be a fact key, a search query, or specific content.
        type: What to delete. Options:
              - "auto" (default): Search all memory types
              - "fact": Delete a specific fact by key
              - "note": Delete a matching note
        confirm: Set to True to delete without preview. Default: False (preview only).

    Examples:
        forget("project_deadline")  # Preview what would be deleted
        forget("project_deadline", confirm=True)  # Actually delete it
        forget("outdated meeting notes", type="note", confirm=True)
    """
    ns = _resolve_tool_namespace()
    if not ns:
        return _no_workspace_error()

    if type == "fact" or (type == "auto" and _looks_like_fact_key(query)):
        result = _forget_fact(ns, query, confirm)
    elif type == "note":
        result = _forget_note(ns, query, confirm)
    else:
        result = _forget_search(ns, query, confirm)

    session_buffer.record(
        tool_name="forget",
        tool_params={"query": query, "type": type, "confirm": confirm},
        result_summary=result[:100],
    )
    return result


def _forget_fact(namespace: str, key: str, confirm: bool) -> str:
    """Delete a fact by key."""
    try:
        with _client() as c:
            # Try slugified key first
            slugified = _slugify(key)
            r = c.get(
                "/memory/facts",
                params={"namespace": namespace, "key": slugified, "limit": 5},
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "forget")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (forget): cannot reach Cornerstone API — {e}"

    facts = data.get("facts", [])
    if not facts:
        return f"[{namespace}] No fact found matching '{key}'"

    fact = facts[0]

    if not confirm:
        return (
            f"[{namespace}] Found fact to delete:\n"
            f"  Key: {fact['key']}\n"
            f"  Value: {fact['value']}\n"
            f'\nCall forget("{key}", confirm=True) to delete it.'
        )

    try:
        with _client() as c:
            r = c.delete(
                f"/memory/facts/{fact['id']}",
                params={"namespace": namespace},
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "forget")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (forget): cannot reach Cornerstone API — {e}"

    return f"[{namespace}] Deleted fact: {fact['key']} = {fact['value']}"


def _forget_note(namespace: str, query: str, confirm: bool) -> str:
    """Handle note deletion — redirects to UI for safety."""
    if not confirm:
        return (
            f"[{namespace}] To delete a specific note, use the Notes page in the UI "
            f"or specify the exact note content.\n"
            f"Note deletion by search query is available in the Cornerstone UI."
        )
    return (
        f"[{namespace}] Note deletion by search requires the UI for safety. "
        f"Use the Notes page to find and delete specific notes."
    )


def _forget_search(namespace: str, query: str, confirm: bool) -> str:
    """Search across all types and show what could be deleted."""
    try:
        with _client() as c:
            r = c.post("/context", json={"query": query, "namespace": namespace})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "forget")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (forget): cannot reach Cornerstone API — {e}"

    context = data.get("context", "")
    if not context:
        return f"[{namespace}] No memories found matching '{query}'"

    return (
        f"[{namespace}] Found memories matching '{query}':\n\n"
        f"{context[:500]}...\n\n"
        f"To delete specific items:\n"
        f'- Facts: forget("fact_key", type="fact", confirm=True)\n'
        f"- Notes: Use the Cornerstone UI Notes page\n"
        f"- Sessions: Sessions cannot be individually deleted via this tool"
    )


# ---------------------------------------------------------------------------
# Workspace tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_workspaces() -> str:
    """List all workspaces available to this principal.

    Returns workspace name, display name, status, and access level for each
    granted workspace. Marks the current active workspace with an asterisk.
    Use this to discover which workspaces you can switch to.
    """
    try:
        with _client() as c:
            r = c.get("/connection/workspaces")
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "list_workspaces")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (list_workspaces): cannot reach Cornerstone API — {e}"

    workspaces = data.get("workspaces", [])
    if not workspaces:
        return "No workspaces available. Contact admin to get workspace grants."

    lines = ["## Available Workspaces\n"]
    for ws in workspaces:
        marker = " * " if ws["name"] == _ws.active_workspace else "   "
        status_tag = ""
        if ws.get("status") == "archived":
            status_tag = " [ARCHIVED]"
        elif ws.get("status") == "frozen":
            status_tag = " [FROZEN]"
        display = ws.get("display_name", ws["name"])
        access = ws.get("access_level", "read")
        lines.append(f"{marker}{ws['name']} ({display}) — {access}{status_tag}")

    lines.append(f"\n* = current workspace: {_ws.active_workspace}")
    lines.append(f"Default workspace: {_ws.default_workspace}")

    session_buffer.record(
        tool_name="list_workspaces",
        result_summary=f"Listed {len(workspaces)} workspaces",
    )
    return "\n".join(lines)


@mcp.tool()
def get_current_workspace() -> str:
    """Show the current active workspace and default workspace.

    The active workspace is used for all memory operations unless a
    specific namespace is provided. The default workspace is loaded on
    startup and persisted across restarts.
    """
    current = _ws.active_workspace
    default = _ws.default_workspace
    principal_info = ""
    if _ws.principal_type != "unknown":
        principal_info = f"\nAuth type: {_ws.principal_type}" + (
            f" (principal: {_ws.principal_id})" if _ws.principal_id else ""
        )
    mismatch = ""
    if current and current != default:
        mismatch = f"\nNote: active workspace differs from default ({default})"
    available_info = ""
    if _ws._is_governed and _ws._available_workspaces:
        ws_list = ", ".join(ws["name"] for ws in _ws._available_workspaces)
        available_info = f"\nAvailable workspaces: {ws_list}"
    no_ws_warning = ""
    if not current:
        no_ws_warning = (
            "\nWarning: no workspace selected. Use switch_workspace() to select one."
        )

    session_buffer.record(
        tool_name="get_current_workspace",
        result_summary=f"Current: {current or '(none)'}",
    )
    return (
        f"Current workspace: {current or '(none)'}\nDefault workspace: {default or '(none)'}"
        f"{principal_info}{mismatch}{available_info}{no_ws_warning}"
    )


@mcp.tool()
def switch_workspace(name: str) -> str:
    """Switch to a different workspace for this session.

    The switch only affects the current session — it does not change the
    default workspace or modify any config files. After switching, the
    system verifies the target workspace is accessible.

    Args:
        name: Target workspace name (must be granted to this principal).
    """
    if not name:
        return "Error: workspace name is required."

    target = name.strip().lower()

    try:
        with _client() as c:
            r = c.post("/connection/verify-workspace", json={"name": target})
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "switch_workspace")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (switch_workspace): cannot reach Cornerstone API — {e}"

    if data.get("status") == "failed":
        reason = data.get("reason_code", "unknown")
        message = data.get("message", "Unknown error.")
        if reason == "workspace_not_granted":
            return (
                f"Error: workspace '{target}' is not granted to this principal.\n"
                f"Use list_workspaces() to see available workspaces."
            )
        if reason == "workspace_not_found":
            return f"Error: workspace '{target}' does not exist."
        if reason == "workspace_archived":
            return (
                f"Error: workspace '{target}' is archived and cannot be used.\n"
                f"Contact admin to restore it."
            )
        if reason == "workspace_deleted":
            return f"Error: workspace '{target}' has been deleted."
        return f"Error: {message}"

    if data.get("status") == "warning":
        old = _ws.active_workspace
        _ws.active_workspace = target
        logger.info("Workspace switched: %s -> %s (frozen, read-only)", old, target)
        session_buffer.reset()
        session_buffer.record(
            tool_name="switch_workspace",
            tool_params={"name": name},
            result_summary=f"Switched {old} -> {target} (frozen)",
        )
        return (
            f"Switched to workspace: {target} ({data.get('display_name', target)})\n"
            f"Warning: this workspace is frozen (read-only access).\n"
            f"Previous workspace: {old}"
        )

    old = _ws.active_workspace
    _ws.active_workspace = target
    display = data.get("display_name", target)
    access = data.get("access_level", "read")
    logger.info("Workspace switched: %s -> %s", old, target)
    session_buffer.reset()
    session_buffer.record(
        tool_name="switch_workspace",
        tool_params={"name": name},
        result_summary=f"Switched {old} -> {target}",
    )
    return (
        f"Switched to workspace: {target} ({display})\n"
        f"Access level: {access}\n"
        f"Previous workspace: {old}\n"
        f"Verification: OK"
    )


@mcp.tool()
def set_default_workspace(name: str) -> str:
    """Set the default workspace for future sessions.

    This persists the default workspace to the settings file so it is
    loaded on startup. It does NOT change the current active workspace —
    use switch_workspace() for that.

    The target workspace must be granted to this principal.

    Args:
        name: Workspace name to set as default.
    """
    if not name:
        return "Error: workspace name is required."

    target = name.strip().lower()

    try:
        with _client() as c:
            r = c.post("/connection/verify-workspace", json={"name": target})
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "set_default_workspace")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (set_default_workspace): cannot reach Cornerstone API — {e}"

    if data.get("status") == "failed":
        reason = data.get("reason_code", "unknown")
        message = data.get("message", "Unknown error.")
        if reason == "workspace_not_granted":
            return (
                f"Error: workspace '{target}' is not granted to this principal.\n"
                f"Use list_workspaces() to see available workspaces."
            )
        if reason == "workspace_not_found":
            return f"Error: workspace '{target}' does not exist."
        if reason == "workspace_archived":
            return (
                f"Error: workspace '{target}' is archived and cannot be set as default.\n"
                f"Contact admin to restore it."
            )
        return f"Error: {message}"

    old_default = _ws.default_workspace
    _ws.default_workspace = target
    if _ws._save_default():
        logger.info("Default workspace set: %s -> %s", old_default, target)
        session_buffer.record(
            tool_name="set_default_workspace",
            tool_params={"name": name},
            result_summary=f"Default set: {old_default} -> {target}",
        )
        return (
            f"Default workspace set to: {target} ({data.get('display_name', target)})\n"
            f"Previous default: {old_default}\n"
            f"Current active workspace: {_ws.active_workspace} (unchanged)\n"
            f"Verification: OK\n"
            f"Persisted to: {_SETTINGS_PATH}"
        )
    else:
        _ws.default_workspace = old_default
        return (
            f"Error: failed to persist default workspace to {_SETTINGS_PATH}.\n"
            f"The settings file may not be writable. Check permissions."
        )


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_context(query: str, namespace: str = "", detail_level: str = "auto") -> str:
    """Retrieve assembled memory context for a query.

    This is the primary retrieval tool. It returns facts, notes, semantic
    memories, and episodic memories relevant to the query, assembled into
    a single context block ready for injection into a conversation.

    Args:
        query: Natural language query describing what context you need.
        namespace: Memory namespace (defaults to active workspace).
        detail_level: How much context to retrieve:
                     - "auto" (default): Cornerstone decides based on your query
                     - "minimal": Quick fact lookup
                     - "standard": Balanced context
                     - "comprehensive": Everything relevant
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        with _client() as c:
            r = c.post(
                "/context",
                json={
                    "query": query,
                    "namespace": ns,
                    "detail_level": detail_level,
                },
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "get_context")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (get_context): cannot reach Cornerstone API — {e}"

    context_text = data.get("context", "")
    context_request_id = data.get("context_request_id")
    stats = data.get("stats", {})
    tokens = stats.get("total_tokens", 0)
    used = stats.get("used_memory", [])
    summary_parts = [f"[workspace: {ns}]", context_text]
    if used:
        summary_parts.append(
            f"\n--- {len(used)} memory items retrieved, {tokens} tokens ---"
        )
    if context_request_id:
        summary_parts.append(f"context_request_id: {context_request_id}")

    session_buffer.record(
        tool_name="get_context",
        tool_params={
            "query": query,
            "namespace": namespace,
            "detail_level": detail_level,
        },
        result_summary=f"{len(used)} items, {tokens} tokens",
    )
    return "\n".join(summary_parts)


@mcp.tool()
def add_fact(
    key: str,
    value: str,
    category: str = "general",
    namespace: str = "",
    confidence: float = 0.9,
) -> str:
    """Store or update a structured fact in long-term memory.

    Facts are key-value pairs that persist across sessions. If a fact with
    the same key already exists in the namespace, it will be updated.

    Args:
        key: Unique identifier for this fact (e.g. "user_timezone", "project_deadline").
        value: The fact content.
        category: Fact category (e.g. "preference", "project", "personal", "general").
        namespace: Memory namespace (defaults to active workspace).
        confidence: Confidence score 0-1.
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        with _client() as c:
            r = c.post(
                "/memory/fact",
                json={
                    "key": key,
                    "value": value,
                    "category": category,
                    "namespace": ns,
                    "confidence": confidence,
                    "agent_id": DEFAULT_AGENT_ID,
                },
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "add_fact")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (add_fact): cannot reach Cornerstone API — {e}"

    session_buffer.record(
        tool_name="add_fact",
        tool_params={
            "key": key,
            "value": value,
            "category": category,
            "namespace": namespace,
        },
        result_summary=f"Fact saved: {key}",
    )
    return f"[workspace: {ns}] Fact saved: {data.get('key', key)} (status: {data.get('status', 'ok')})"


@mcp.tool()
def add_note(content: str, tags: list[str] | None = None, namespace: str = "") -> str:
    """Save a freeform note to long-term memory.

    Notes are timestamped text entries with optional tags. Use for session
    summaries, meeting notes, decisions, action items, or anything that
    doesn't fit a structured fact.

    Args:
        content: The note text.
        tags: Optional list of tags for categorisation.
        namespace: Memory namespace (defaults to active workspace).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        with _client() as c:
            r = c.post(
                "/memory/note",
                json={
                    "content": content,
                    "tags": tags or [],
                    "namespace": ns,
                    "agent_id": DEFAULT_AGENT_ID,
                },
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "add_note")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (add_note): cannot reach Cornerstone API — {e}"

    note_id = data.get("note_id", "unknown")
    session_buffer.record(
        tool_name="add_note",
        tool_params={"content": content, "tags": tags, "namespace": namespace},
        result_summary=f"Note saved: {note_id}",
    )
    return f"[workspace: {ns}] Note saved (id: {note_id}, status: {data.get('status', 'ok')})"


@mcp.tool()
def list_facts(namespace: str = "", limit: int = 25) -> str:
    """List recent facts from memory.

    Args:
        namespace: Memory namespace (defaults to active workspace).
        limit: Max number of facts to return (1-25).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        with _client() as c:
            r = c.get(
                "/memory/recent", params={"namespace": ns, "limit": min(limit, 25)}
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "list_facts")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (list_facts): cannot reach Cornerstone API — {e}"

    facts = data.get("facts", [])
    if not facts:
        session_buffer.record(
            tool_name="list_facts",
            tool_params={"namespace": namespace, "limit": limit},
            result_summary="No facts found",
        )
        return f"[workspace: {ns}] No facts found."
    lines = [f"[workspace: {ns}]"]
    for f in facts:
        lines.append(
            f"- [{f.get('category', '?')}] {f.get('key', '?')}: {f.get('value', '')}"
        )
    session_buffer.record(
        tool_name="list_facts",
        tool_params={"namespace": namespace, "limit": limit},
        result_summary=f"Found {len(facts)} facts",
    )
    return "\n".join(lines)


@mcp.tool()
def search(query: str, namespace: str = "") -> str:
    """Search memory for relevant information.

    Returns facts, notes, episodic and semantic memories matching the query.
    Lighter than get_context — returns raw memory items without full assembly.

    Args:
        query: Search query.
        namespace: Memory namespace (defaults to active workspace).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        with _client() as c:
            r = c.get("/memory/recent", params={"namespace": ns, "limit": 25})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "search")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (search): cannot reach Cornerstone API — {e}"

    sections = [f"[workspace: {ns}]"]

    facts = data.get("facts", [])
    if facts:
        sections.append("## Facts")
        for f in facts:
            sections.append(
                f"- [{f.get('category', '?')}] {f.get('key', '?')}: {f.get('value', '')}"
            )

    notes = data.get("notes", [])
    if notes:
        sections.append("\n## Notes")
        for n in notes:
            tags = ", ".join(n.get("tags", []))
            preview = (n.get("content", ""))[:200]
            sections.append(f"- [{tags}] {preview}")

    sessions = data.get("sessions", [])
    if sessions:
        sections.append("\n## Recent Sessions")
        for s in sessions:
            sections.append(
                f"- {s.get('topic', 'untitled')}: {(s.get('summary', '') or '')[:150]}"
            )

    if len(sections) == 1:
        session_buffer.record(
            tool_name="search",
            tool_params={"query": query, "namespace": namespace},
            result_summary="No memory found",
        )
        return f"[workspace: {ns}] No memory found."

    session_buffer.record(
        tool_name="search",
        tool_params={"query": query, "namespace": namespace},
        result_summary=f"Found {len(facts)} facts, {len(notes)} notes, {len(sessions)} sessions",
    )
    return "\n".join(sections)


@mcp.tool()
def get_recent_sessions(namespace: str = "", limit: int = 5) -> str:
    """Get recent conversation sessions with summaries.

    Args:
        namespace: Memory namespace (defaults to active workspace).
        limit: Max sessions to return.
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        with _client() as c:
            r = c.get("/memory/load", params={"namespace": ns})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "get_recent_sessions")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (get_recent_sessions): cannot reach Cornerstone API — {e}"

    sessions = data.get("sessions", [])[:limit]
    if not sessions:
        session_buffer.record(
            tool_name="get_recent_sessions",
            tool_params={"namespace": namespace, "limit": limit},
            result_summary="No recent sessions",
        )
        return f"[workspace: {ns}] No recent sessions."
    lines = [f"[workspace: {ns}]"]
    for s in sessions:
        topic = s.get("topic", "untitled")
        summary = (s.get("summary", "") or "")[:300]
        started = s.get("started_at", "?")
        lines.append(f"### {topic}\n{started}\n{summary}\n")

    session_buffer.record(
        tool_name="get_recent_sessions",
        tool_params={"namespace": namespace, "limit": limit},
        result_summary=f"Found {len(sessions)} sessions",
    )
    return "\n".join(lines)


@mcp.tool()
def report_context_feedback(
    context_request_id: str,
    quality: str = "helpful",
    comment: str = "",
) -> str:
    """Report feedback on the quality of retrieved context.

    After using get_context, you can report whether the context was helpful.
    This helps Cornerstone improve retrieval over time.

    Args:
        context_request_id: The ID returned by get_context.
        quality: "helpful", "partially_helpful", or "not_helpful".
        comment: Optional explanation of what was good or missing.
    """
    valid_qualities = {"helpful", "partially_helpful", "not_helpful"}
    if quality not in valid_qualities:
        return f"Error: quality must be one of: {', '.join(sorted(valid_qualities))}"

    try:
        with _client() as c:
            r = c.post(
                "/context/feedback",
                json={
                    "context_request_id": context_request_id,
                    "feedback_type": "overall",
                    "quality": quality,
                    "comment": comment,
                },
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "report_context_feedback")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (report_context_feedback): cannot reach Cornerstone API — {e}"

    session_buffer.record(
        tool_name="report_context_feedback",
        tool_params={"context_request_id": context_request_id, "quality": quality},
        result_summary=f"Feedback: {quality}",
    )
    return "Feedback recorded. Thank you."


@mcp.tool()
def save_conversation(
    messages: list[dict],
    topic: str | None = None,
    namespace: str = "",
) -> str:
    """Save a conversation to memory. Cornerstone will extract key information,
    create a summary, and link it to related conversations.

    Call this at the end of a meaningful conversation to preserve it.
    The AI should NOT call this for every turn — only when the user
    explicitly asks to save the conversation, or at natural conversation
    endpoints where the user has shared important information.

    Args:
        messages: The conversation messages as a list of dicts with
                  "role" and "content" keys.
                  Example: [{"role": "user", "content": "..."},
                            {"role": "assistant", "content": "..."}]
        topic: Optional topic name for the conversation.
        namespace: Memory namespace (defaults to active workspace).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()

    if not messages:
        return "Error: no messages provided."

    # Combine messages into the ingest format.
    # The ingest API takes user_message + assistant_response per call.
    # We merge all user turns into user_message and all assistant turns
    # into assistant_response to capture the full conversation in one pass.
    user_parts: list[str] = []
    assistant_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not content:
            continue
        if role == "user":
            user_parts.append(content)
        elif role == "assistant":
            assistant_parts.append(content)

    if not user_parts and not assistant_parts:
        return "Error: messages contain no user or assistant content."

    user_message = "\n\n".join(user_parts) if user_parts else None
    assistant_response = "\n\n".join(assistant_parts) if assistant_parts else None

    try:
        with _client() as c:
            payload: dict = {
                "user_message": user_message,
                "assistant_response": assistant_response,
                "namespace": ns,
                "agent_id": DEFAULT_AGENT_ID,
                "source": "mcp-save-conversation",
                "force": True,
            }
            if topic:
                payload["topic"] = topic
            r = c.post("/ingest", json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "save_conversation")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (save_conversation): cannot reach Cornerstone API — {e}"

    session_id = data.get("session_id", "unknown")
    episodic = data.get("episodic_count", 0)
    semantic = data.get("semantic_count", 0)
    entities = data.get("entities_staged", 0)
    relations = data.get("relations_staged", 0)
    gated = data.get("gated", False)
    errors = data.get("errors", [])

    if gated:
        return (
            f"[{ns}] Conversation saved (session {session_id[:8]}...) "
            f"but extraction was gated. Use explicit remember/add_fact "
            f"for key items."
        )

    parts = [f"[{ns}] Conversation saved (session {session_id[:8]}...):"]
    parts.append(f"  Episodic memories: {episodic}")
    parts.append(f"  Semantic memories: {semantic}")
    if entities:
        parts.append(f"  Entities staged: {entities}")
    if relations:
        parts.append(f"  Relations staged: {relations}")
    if errors:
        parts.append(f"  Errors: {'; '.join(errors)}")

    summary_note = []
    if episodic == 0 and semantic == 0 and entities == 0:
        summary_note.append(
            "Note: no memories extracted. The conversation may have been too "
            "short or lacked durable information. Use remember() or add_fact() "
            "for specific items."
        )

    session_buffer.record(
        tool_name="save_conversation",
        tool_params={
            "topic": topic,
            "namespace": namespace,
            "message_count": len(messages),
        },
        result_summary=f"Saved session {session_id[:8]}, {episodic} episodic, {semantic} semantic",
    )
    return "\n".join(parts + summary_note)


@mcp.tool()
def list_threads(namespace: str = "") -> str:
    """List conversation threads — groups of related conversations about the same topic.

    Args:
        namespace: Memory namespace (defaults to active workspace).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        with _client() as c:
            r = c.get("/memory/threads", params={"namespace": ns})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "list_threads")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (list_threads): cannot reach Cornerstone API — {e}"

    threads = data.get("threads", [])
    if not threads:
        session_buffer.record(
            tool_name="list_threads",
            tool_params={"namespace": namespace},
            result_summary="No threads found",
        )
        return f"[workspace: {ns}] No conversation threads found."

    lines = [f"[workspace: {ns}] Found {len(threads)} conversation threads:\n"]
    for t in threads:
        topic = t.get("topic") or "Untitled"
        count = t.get("session_count", 1)
        last = (t.get("last_session_at") or "")[:10]
        lines.append(f"  {topic} ({count} sessions, last active {last})")

    session_buffer.record(
        tool_name="list_threads",
        tool_params={"namespace": namespace},
        result_summary=f"Found {len(threads)} threads",
    )
    return "\n".join(lines)


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
