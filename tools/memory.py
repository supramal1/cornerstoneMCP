"""Memory tools: remember, recall, forget, get_context, add_fact, add_note, search."""

from __future__ import annotations

import re

import httpx

from core import (
    DEFAULT_AGENT_ID,
    _classify_memory,
    _client,
    _format_http_error,
    _looks_like_fact_key,
    _no_workspace_error,
    _resolve_tool_namespace,
    _slugify,
    mcp,
    session_buffer,
)


# ---------------------------------------------------------------------------
# remember
# ---------------------------------------------------------------------------


@mcp.tool()
def remember(content: str, type: str = "auto") -> str:
    """Save something to memory. Cornerstone decides whether to store as a
    fact or note based on content.

    If the content is a specific piece of information (a date, a name, a
    number, a decision), it becomes a fact. Follow the same rules as
    add_fact: include a date, one topic only, descriptive key.

    If the content is a summary, observation, or multi-topic note, it
    becomes a note.

    Args:
        content: What to remember. Can be a fact ("Malik's email is malik@co.com"),
                 a note ("Meeting decided to go with option B"), or any text.
        type: How to store it. Options:
              - "auto" (default): Cornerstone decides based on content
              - "fact": Key-value information (e.g., "Project deadline is March 15")
              - "note": Freeform observation or note

    Examples:
        remember('Nike QBR is scheduled for 15 May 2026') → fact
        remember('Meeting with James covered equity terms, office
                 move timeline, and onboarding plan') → note
        remember("Malik prefers morning meetings", type="fact")
    """
    ns = _resolve_tool_namespace()
    if not ns:
        return _no_workspace_error()

    # --- Forge prefix detection: route to Forge queue silently ---
    forge_match = re.match(r"^forge:\s*", content, re.IGNORECASE)
    if forge_match:
        return _remember_forge_opportunity(content[forge_match.end() :].strip(), ns)

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

    buf_id = session_buffer.current_session_id

    if memory_type == "fact":
        try:
            payload: dict = {
                "key": metadata["key"],
                "value": metadata["value"],
                "namespace": ns,
                "category": "general",
                "confidence": 0.9,
                "agent_id": DEFAULT_AGENT_ID,
            }
            if buf_id:
                payload["buffer_session_id"] = buf_id
            with _client() as c:
                r = c.post("/memory/fact", json=payload)
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
            note_payload: dict = {
                "content": metadata["content"],
                "namespace": ns,
                "tags": ["remember"],
                "agent_id": DEFAULT_AGENT_ID,
            }
            if buf_id:
                note_payload["buffer_session_id"] = buf_id
            with _client() as c:
                r = c.post("/memory/note", json=note_payload)
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
    key = _slugify(content[:50])
    return {"key": key, "value": content, "display_key": content[:50]}


def _remember_forge_opportunity(description: str, ns: str) -> str:
    """Route a Forge-prefixed remember call to the Forge queue as a note."""
    from datetime import date

    if not description:
        return f"[{ns}] Forge prefix detected but no description provided."

    iso_date = date.today().isoformat()
    label = f"AI Ops Opportunity: {description}"
    note_content = (
        f"# {label}\n\n"
        f"**Source**: Claude Desktop (MCP)\n"
        f"**Date**: {iso_date}\n\n"
        f"{description}"
    )

    try:
        payload: dict = {
            "content": note_content,
            "namespace": ns,
            "tags": ["forge-opportunity", "ai-ops-request", "pending"],
            "agent_id": DEFAULT_AGENT_ID,
        }
        buf_id = session_buffer.current_session_id
        if buf_id:
            payload["buffer_session_id"] = buf_id
        with _client() as c:
            r = c.post("/memory/note", json=payload)
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "remember")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (remember): cannot reach Cornerstone API — {e}"

    preview = description[:80] + "..." if len(description) > 80 else description
    session_buffer.record(
        tool_name="remember",
        tool_params={"content": f"Forge: {description}", "type": "auto"},
        result_summary=f"Forge opportunity logged: {preview[:60]}",
    )
    return f"[{ns}] Logged Forge opportunity: {preview}"


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


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
# get_context (with temporal filtering — Task 2)
# ---------------------------------------------------------------------------


@mcp.tool()
def get_context(
    query: str,
    namespace: str = "",
    detail_level: str = "auto",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Retrieve assembled memory context for a query.

    This is the primary retrieval tool. It returns facts, notes, semantic
    memories, and episodic memories relevant to the query, assembled into
    a single context block ready for injection into a conversation.

    Temporal queries are auto-detected from natural language. Expressions
    like "last week", "yesterday", "in March 2026", "3 days ago", or
    "since 2026-03-01" are parsed automatically — the resolved date range
    filters retrieval and the temporal expression is stripped from the
    semantic search query. Explicit from_date/to_date params override
    any auto-detected intent.

    Args:
        query: Natural language query describing what context you need.
               Temporal expressions are auto-detected (e.g. "last week",
               "in March 2026", "3 days ago").
        namespace: Memory namespace (defaults to active workspace).
        detail_level: How much context to retrieve:
                     - "auto" (default): Cornerstone decides based on your query
                     - "minimal": Quick fact lookup
                     - "standard": Balanced context
                     - "comprehensive": Everything relevant
        from_date: Inclusive start date for temporal filtering (YYYY-MM-DD or
                   relative: today, yesterday, last_7_days, last_7d, this_week,
                   last_30_days, last_30d, this_month, last_month). When set,
                   retrieval is time-bounded. Overrides auto-detected intent.
        to_date: Inclusive end date for temporal filtering (YYYY-MM-DD or
                 relative shorthand). Auto-defaults to today when from_date is
                 a relative shorthand and to_date is omitted.
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        body: dict = {
            "query": query,
            "namespace": ns,
            "detail_level": detail_level,
        }
        if from_date:
            body["from_date"] = from_date
        if to_date:
            body["to_date"] = to_date
        with _client() as c:
            r = c.post("/context", json=body)
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

    tool_params: dict = {
        "query": query,
        "namespace": namespace,
        "detail_level": detail_level,
    }
    if from_date:
        tool_params["from_date"] = from_date
    if to_date:
        tool_params["to_date"] = to_date
    session_buffer.record(
        tool_name="get_context",
        tool_params=tool_params,
        result_summary=f"{len(used)} items, {tokens} tokens",
    )
    return "\n".join(summary_parts)


# ---------------------------------------------------------------------------
# add_fact / add_note
# ---------------------------------------------------------------------------


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

    FORMATTING RULES (the model MUST follow these):

    VALUE:
    - MUST include a date (e.g. '2026-04-19', 'April 2026',
      'as of Q2 2026'). Every fact needs a temporal anchor so
      retrieval can distinguish current from outdated information.
    - MUST be one topic only. If you have multiple pieces of
      information, call add_fact multiple times.
    - MUST be under 200 tokens. If longer, split into multiple
      facts.
    - Use factual declarative language, not narrative.
      Good: 'Kim Berkin is MD at Charlie Oscar as of April 2026.
            Previously MD at DentsuX and Fetch.'
      Bad:  'Kim is basically the boss and she used to work at
            some other agencies'
    - Include enough context to be self-contained. A fact
      retrieved on its own should make sense without needing
      the conversation that created it.

    KEY:
    - Use snake_case with descriptive names.
    - Describe WHAT the fact is about, not WHEN.
      Good: 'kim_berkin_role', 'nike_q3_budget'
      Bad:  'latest_update', 'most_recent_sprint', 'current_status'
    - Temporal words in keys (latest, current, most_recent, last)
      create facts that rot — the key implies currency that the
      value can't maintain.

    BEFORE WRITING:
    - Check if a fact with this key already exists using
      get_context or search. If it does and your new value is
      a subset of the existing value, DO NOT overwrite — the
      existing fact is more comprehensive.

    Examples:
      Good: add_fact('nike_q3_budget', 'Nike Q3 2026 budget
            approved at £200k on 2026-04-19')
      Good: add_fact('kim_berkin_role', 'Kim Berkin is Managing
            Director at Charlie Oscar as of April 2026. Previously
            MD at DentsuX and Fetch.')
      Bad:  add_fact('latest_budget', 'budget is 200k')
      Bad:  add_fact('kim_info', 'Kim is MD and she used to work
            at Fetch and DentsuX and she also worked at Heineken
            and she interviewed Malik with Dan')

    Args:
        key: Unique identifier. Use snake_case. Describe the topic, not the
            time. No temporal words (latest, current, most_recent, last).
            Examples: 'nike_q3_budget', 'kim_berkin_role',
            'cornerstone_sprint_rr_complete'
        value: The fact content. MUST include a date. MUST be one topic only.
            MUST be under 200 tokens. Use factual declarative language. Must
            be self-contained — readable without conversation context.
        category: Fact category. Options: 'personal' (user info), 'agency'
            (org info), 'project' (build work), 'infrastructure'
            (technical/deployment), 'general' (everything else). Choose the
            most specific.
        namespace: Memory namespace (defaults to active workspace).
        confidence: Confidence score 0-1.
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        payload: dict = {
            "key": key,
            "value": value,
            "category": category,
            "namespace": ns,
            "confidence": confidence,
            "agent_id": DEFAULT_AGENT_ID,
        }
        if session_buffer.current_session_id:
            payload["buffer_session_id"] = session_buffer.current_session_id
        with _client() as c:
            r = c.post("/memory/fact", json=payload)
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
        payload: dict = {
            "content": content,
            "tags": tags or [],
            "namespace": ns,
            "agent_id": DEFAULT_AGENT_ID,
        }
        if session_buffer.current_session_id:
            payload["buffer_session_id"] = session_buffer.current_session_id
        with _client() as c:
            r = c.post("/memory/note", json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "add_note")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (add_note): cannot reach Cornerstone API — {e}"

    note_id = data.get("note_id") or "unknown"
    session_buffer.record(
        tool_name="add_note",
        tool_params={"content": content, "tags": tags, "namespace": namespace},
        result_summary=f"Note saved: {note_id}",
    )
    return f"[workspace: {ns}] Note saved (id: {note_id}, status: {data.get('status', 'ok')})"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


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
            r = c.post(
                "/context",
                json={"query": query, "namespace": ns, "detail_level": "standard"},
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "search")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (search): cannot reach Cornerstone API — {e}"

    stats = data.get("stats", {})
    used = stats.get("used_memory", [])

    if not used:
        session_buffer.record(
            tool_name="search",
            tool_params={"query": query, "namespace": namespace},
            result_summary="No memory found",
        )
        return f"[workspace: {ns}] No memory found matching: {query}"

    # Group items by source_type
    grouped: dict[str, list] = {}
    for item in used:
        source_type = item.get("source_type", "unknown")
        grouped.setdefault(source_type, []).append(item)

    sections = [f"[workspace: {ns}]"]
    type_labels = {
        "fact": "Facts",
        "note": "Notes",
        "episodic": "Episodic Memories",
        "semantic": "Semantic Memories",
        "graph": "Knowledge Graph",
    }

    for source_type, items in grouped.items():
        label = type_labels.get(source_type, source_type.title())
        sections.append(f"\n## {label}")
        for item in items:
            preview = item.get("preview", "")[:300]
            score = item.get("score") or item.get("match_score")
            ts = (item.get("timestamp", "") or "")[:10]
            parts = [f"- ({ts})" if ts else "-"]
            parts.append(preview)
            if score is not None:
                parts.append(f"[score: {score:.2f}]")
            sections.append(" ".join(parts))

    session_buffer.record(
        tool_name="search",
        tool_params={"query": query, "namespace": namespace},
        result_summary=f"Found {len(used)} items across {len(grouped)} types",
    )
    return "\n".join(sections)
