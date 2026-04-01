"""Retrieval tools: list_facts, list_notes, get_recent_sessions, list_threads, report_context_feedback."""

from __future__ import annotations

import httpx

from core import (
    _client,
    _format_http_error,
    _no_workspace_error,
    _resolve_tool_namespace,
    mcp,
    session_buffer,
)


@mcp.tool()
def list_facts(
    namespace: str = "", limit: int = 25, from_date: str = "", to_date: str = ""
) -> str:
    """List recent facts from memory.

    Args:
        namespace: Memory namespace (defaults to active workspace).
        limit: Max number of facts to return (1-25).
        from_date: Filter facts updated on or after this date (YYYY-MM-DD, inclusive).
        to_date: Filter facts updated on or before this date (YYYY-MM-DD, inclusive).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        params = {"namespace": ns, "limit": min(limit, 25)}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        with _client() as c:
            r = c.get("/memory/facts", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "list_facts")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (list_facts): cannot reach Cornerstone API — {e}"

    facts = data.get("facts", [])
    tool_params = {"namespace": namespace, "limit": limit}
    if from_date:
        tool_params["from_date"] = from_date
    if to_date:
        tool_params["to_date"] = to_date
    if not facts:
        session_buffer.record(
            tool_name="list_facts",
            tool_params=tool_params,
            result_summary="No facts found",
        )
        date_note = ""
        if from_date or to_date:
            date_note = f" (filtered: {from_date or '...'} to {to_date or '...'})"
        return f"[workspace: {ns}] No facts found{date_note}."
    lines = [f"[workspace: {ns}]"]
    if from_date or to_date:
        lines[0] += f" (filtered: {from_date or '...'} to {to_date or '...'})"
    for f in facts:
        ts = (f.get("updated_at", "") or "")[:10]
        lines.append(
            f"- [{f.get('category', '?')}] {f.get('key', '?')}: "
            f"{f.get('value', '')} (updated: {ts})"
        )
    session_buffer.record(
        tool_name="list_facts",
        tool_params=tool_params,
        result_summary=f"Found {len(facts)} facts",
    )
    return "\n".join(lines)


@mcp.tool()
def list_notes(
    namespace: str = "", limit: int = 25, from_date: str = "", to_date: str = ""
) -> str:
    """List recent notes from memory.

    Args:
        namespace: Memory namespace (defaults to active workspace).
        limit: Max number of notes to return (1-25).
        from_date: Filter notes created on or after this date (YYYY-MM-DD, inclusive).
        to_date: Filter notes created on or before this date (YYYY-MM-DD, inclusive).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        params: dict = {"namespace": ns, "limit": min(limit, 25)}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        with _client() as c:
            r = c.get("/memory/notes", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "list_notes")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (list_notes): cannot reach Cornerstone API — {e}"

    notes = data.get("notes", [])
    tool_params: dict = {"namespace": namespace, "limit": limit}
    if from_date:
        tool_params["from_date"] = from_date
    if to_date:
        tool_params["to_date"] = to_date
    if not notes:
        session_buffer.record(
            tool_name="list_notes",
            tool_params=tool_params,
            result_summary="No notes found",
        )
        date_note = ""
        if from_date or to_date:
            date_note = f" (filtered: {from_date or '...'} to {to_date or '...'})"
        return f"[workspace: {ns}] No notes found{date_note}."
    lines = [f"[workspace: {ns}]"]
    if from_date or to_date:
        lines[0] += f" (filtered: {from_date or '...'} to {to_date or '...'})"
    for n in notes:
        tags = ", ".join(n.get("tags", []) or [])
        ts = (n.get("created_at", "") or "")[:10]
        preview = (n.get("content", "") or "")[:200]
        tag_label = f" [{tags}]" if tags else ""
        lines.append(f"- ({ts}){tag_label} {preview}")

    session_buffer.record(
        tool_name="list_notes",
        tool_params=tool_params,
        result_summary=f"Found {len(notes)} notes",
    )
    return "\n".join(lines)


@mcp.tool()
def get_recent_sessions(
    namespace: str = "", limit: int = 5, from_date: str = "", to_date: str = ""
) -> str:
    """Get recent conversation sessions with summaries.

    Args:
        namespace: Memory namespace (defaults to active workspace).
        limit: Max sessions to return (1-25).
        from_date: Filter sessions started on or after this date (YYYY-MM-DD, inclusive).
        to_date: Filter sessions started on or before this date (YYYY-MM-DD, inclusive).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        params: dict = {"namespace": ns, "limit": min(limit, 25)}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        with _client() as c:
            r = c.get("/memory/sessions", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "get_recent_sessions")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (get_recent_sessions): cannot reach Cornerstone API — {e}"

    sessions = data.get("sessions", [])
    tool_params: dict = {"namespace": namespace, "limit": limit}
    if from_date:
        tool_params["from_date"] = from_date
    if to_date:
        tool_params["to_date"] = to_date
    if not sessions:
        session_buffer.record(
            tool_name="get_recent_sessions",
            tool_params=tool_params,
            result_summary="No recent sessions",
        )
        date_note = ""
        if from_date or to_date:
            date_note = f" (filtered: {from_date or '...'} to {to_date or '...'})"
        return f"[workspace: {ns}] No recent sessions{date_note}."
    lines = [f"[workspace: {ns}]"]
    if from_date or to_date:
        lines[0] += f" (filtered: {from_date or '...'} to {to_date or '...'})"
    for s in sessions:
        topic = s.get("topic", "untitled")
        summary = (s.get("summary", "") or "")[:300]
        started = s.get("started_at", "?")
        lines.append(f"### {topic}\n{started}\n{summary}\n")

    session_buffer.record(
        tool_name="get_recent_sessions",
        tool_params=tool_params,
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
def list_threads(namespace: str = "", from_date: str = "", to_date: str = "") -> str:
    """List conversation threads — groups of related conversations about the same topic.

    Args:
        namespace: Memory namespace (defaults to active workspace).
        from_date: Filter threads last updated on or after this date (YYYY-MM-DD, inclusive).
        to_date: Filter threads last updated on or before this date (YYYY-MM-DD, inclusive).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        params: dict = {"namespace": ns}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        with _client() as c:
            r = c.get("/memory/threads", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "list_threads")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (list_threads): cannot reach Cornerstone API — {e}"

    threads = data.get("threads", [])
    tool_params: dict = {"namespace": namespace}
    if from_date:
        tool_params["from_date"] = from_date
    if to_date:
        tool_params["to_date"] = to_date
    if not threads:
        session_buffer.record(
            tool_name="list_threads",
            tool_params=tool_params,
            result_summary="No threads found",
        )
        date_note = ""
        if from_date or to_date:
            date_note = f" (filtered: {from_date or '...'} to {to_date or '...'})"
        return f"[workspace: {ns}] No conversation threads found{date_note}."

    lines = [f"[workspace: {ns}] Found {len(threads)} conversation threads:\n"]
    if from_date or to_date:
        lines[0] = (
            f"[workspace: {ns}] Found {len(threads)} threads "
            f"(filtered: {from_date or '...'} to {to_date or '...'}):\n"
        )
    for t in threads:
        topic = t.get("topic") or "Untitled"
        count = t.get("session_count", 1)
        last = (t.get("last_session_at") or "")[:10]
        lines.append(f"  {topic} ({count} sessions, last active {last})")

    session_buffer.record(
        tool_name="list_threads",
        tool_params=tool_params,
        result_summary=f"Found {len(threads)} threads",
    )
    return "\n".join(lines)
