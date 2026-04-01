"""Workspace tools: list, get, switch, set_default."""

from __future__ import annotations

import httpx

from core import (
    _client,
    _format_http_error,
    _no_workspace_error,
    _resolve_tool_namespace,
    _ws,
    logger,
    mcp,
    session_buffer,
)


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
            r = c.post(
                "/connection/verify-workspace",
                json={"name": target, "namespace": target},
            )
            r.raise_for_status()
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

    from core import _SETTINGS_PATH

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
