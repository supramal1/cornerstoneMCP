"""Session tools: save_conversation."""

from __future__ import annotations

import httpx

from core import (
    DEFAULT_AGENT_ID,
    _client,
    _format_http_error,
    _no_workspace_error,
    _resolve_tool_namespace,
    mcp,
    session_buffer,
)


@mcp.tool()
def save_conversation(
    messages: list[dict],
    topic: str | None = None,
    namespace: str = "",
) -> str:
    """Save a conversation to memory. Cornerstone will extract key information,
    create a summary, and link it to related conversations. Each save also
    feeds the personalisation system, helping Cornerstone learn how each
    team member works.

    Only save business-relevant conversations. Do not save personal
    conversations (furniture shopping, cinema plans, recipes, personal
    finance) or general knowledge questions unrelated to agency work.
    The test: would another CO team member or a future session benefit
    from this being in the system? If yes, save. If no, don't.

    Call this proactively at natural conversation breakpoints when the
    topic is business-related:
    - After a meaningful exchange (decision made, information shared, problem solved)
    - When switching topics within a session (save what you've covered before moving on)
    - At natural breaks in long business sessions (every 15-20 substantive exchanges)
    - At the end of every business-relevant session
    - When the user explicitly asks

    Do NOT wait for the user to ask for business content. If you've had
    10-15 substantive business exchanges without saving, save now.
    Do NOT save purely personal or off-topic conversations.

    Args:
        messages: The conversation messages as a list of dicts with
                  "role" and "content" keys.
                  Example: [{"role": "user", "content": "..."},
                            {"role": "assistant", "content": "..."}]
        topic: Optional topic name for the conversation. Always provide
               a descriptive topic (e.g. "Nike Q2 budget discussion",
               "Supermemory migration architecture decisions",
               "Sprint planning April 21"). This helps with retrieval
               and organisation.
        namespace: Memory namespace (defaults to active workspace).
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()

    if not messages:
        return "Error: no messages provided."

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
                "async_mode": True,
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

    session_id = data.get("session_id") or "unknown"
    status = data.get("status", "")
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

    # Async mode: extraction is processing in background
    if status == "processing":
        parts = [f"[{ns}] Conversation saved (session {session_id[:8]}...)."]
        parts.append("  Extraction processing in background — memories will appear shortly.")
    else:
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
    if status != "processing" and episodic == 0 and semantic == 0 and entities == 0:
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
