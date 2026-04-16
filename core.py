"""
Cornerstone MCP — shared state and configuration.

All tool modules import from here. This module owns:
- Configuration constants
- Logger
- SessionBuffer (used by every tool for event recording)
- WorkspaceState (grant-aware namespace resolution)
- Helper functions (namespace resolution, error formatting, classification)
- FastMCP instance + auth setup
- HTTP client factory
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from auth.oauth import (
    MCP_PUBLIC_URL,
    CornerstoneOAuthProvider,
    get_api_key_from_token,
    register_login_routes,
)

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
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
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
    if os.environ.get("CODEX_CLI"):
        return "codex"
    if os.environ.get("CURSOR_SESSION"):
        return "cursor"
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

    def end(self):
        """Signal session end to backend so the finalizer picks it up immediately."""
        with self._lock:
            session_id = self.current_session_id
        if not session_id:
            return
        try:
            httpx.post(
                f"{self.api_url}/session-buffer/end",
                headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
                json={"session_id": session_id},
                timeout=3,
            )
        except Exception:
            pass  # Best-effort on exit


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

atexit.register(session_buffer.end)


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
        """Resolve workspace from backend grants at startup."""
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

                self._is_governed = True

                ws_resp = client.get("/connection/workspaces")
                ws_resp.raise_for_status()
                ws_data = ws_resp.json()

                workspaces = ws_data.get("workspaces", [])
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


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def active_namespace() -> str:
    """Return the current active workspace for this session."""
    return _ws.active_workspace


def _resolve_tool_namespace(namespace: str = "") -> str:
    """Resolve the namespace for a tool call.

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


def _classify_memory(content: str) -> tuple[str, dict]:
    """Classify content as fact or note."""
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
            if len(key_raw.split()) <= 5:
                key = _slugify(key_raw)
                return "fact", {"key": key, "value": value, "display_key": key_raw}

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

_oauth_provider = CornerstoneOAuthProvider()

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
    auth_server_provider=_oauth_provider,
    stateless_http=True,
    streamable_http_path="/",
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(MCP_PUBLIC_URL),
        resource_server_url=AnyHttpUrl(MCP_PUBLIC_URL),
        required_scopes=["memory"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["memory"],
            default_scopes=["memory"],
        ),
    ),
)

register_login_routes(mcp)

# ---------------------------------------------------------------------------
# Friendly error handling for tool validation errors
# ---------------------------------------------------------------------------

_original_call_tool = mcp.call_tool


async def _friendly_call_tool(name, arguments):
    """Wrap tool calls to catch Pydantic validation errors and return
    user-friendly messages instead of raw tracebacks."""
    try:
        return await _original_call_tool(name, arguments)
    except Exception as e:
        err_str = str(e)
        if "validation error" in err_str.lower():
            missing = []
            import re as _re

            for m in _re.finditer(r"(\w+)\s+Field required", err_str):
                missing.append(m.group(1))
            if missing:
                return [
                    {
                        "type": "text",
                        "text": (
                            f"Tool '{name}' is missing required parameter(s): "
                            f"{', '.join(missing)}.\n"
                            f"Please provide: {', '.join(missing)}."
                        ),
                    }
                ]
            return [
                {
                    "type": "text",
                    "text": f"Invalid parameters for tool '{name}'. Please check the tool's parameter names and try again.",
                }
            ]
        raise


mcp.call_tool = _friendly_call_tool


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        access_token = get_access_token()
        if access_token:
            token_type = "csk" if access_token.token.startswith("csk_") else "jwt"
            api_key = get_api_key_from_token(access_token.token)
            if api_key:
                import hashlib

                key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:8]
                logger.debug(
                    "Auth: bearer=%s, key_fingerprint=%s",
                    token_type,
                    key_fingerprint,
                )
                h["X-API-Key"] = api_key
                return h
            else:
                logger.warning(
                    "get_api_key_from_token returned None for %s token",
                    token_type,
                )
        else:
            logger.debug("No access_token in request context — falling back to env var")
    except Exception as e:
        logger.warning("get_access_token() failed: %s", e)
    if CORNERSTONE_API_KEY:
        logger.debug("Using CORNERSTONE_API_KEY env var fallback")
        h["X-API-Key"] = CORNERSTONE_API_KEY
    return h


def _client() -> httpx.Client:
    return httpx.Client(base_url=CORNERSTONE_URL, headers=_headers(), timeout=30)
