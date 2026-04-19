"""Steward tools — AI-assisted workspace maintenance.

Exposes 5 tools for data quality inspection, AI-powered recommendations,
preview/apply mutations, and health dashboard.
"""

from __future__ import annotations

import json

import httpx

from core import (
    CORNERSTONE_URL,
    _client,
    _format_http_error,
    _headers,
    _no_workspace_error,
    _resolve_tool_namespace,
    mcp,
    session_buffer,
)

# WARNING: Adding a new inspect operation in the backend (cornerstone repo)
# also requires registration here. If you add there but not here, the
# operation works via API but not via MCP tools (Claude Desktop, Claude Code).
_STEWARD_INSPECT_OPS = {
    "duplicates",
    "contradictions",
    "stale",
    "expired",
    "orphans",
    "key-taxonomy",
    "stale-embeddings",
    "cross-workspace-duplicates",
    "retrieval-interference",
    "missing-dates",
    "composite-health",
    "fact-quality",
}

_STEWARD_ADVISE_OPS = {
    "merge",
    "consolidate",
    "stale-review",
    "key-taxonomy",
    "contradictions",
}

_STEWARD_MUTATE_OPS = {
    "merge-duplicates",
    "merge-notes",
    "archive-stale",
    "delete-by-filter",
    "consolidate-facts",
    "reembed-stale",
    "rename-keys",
    "resolve-contradictions",
}

_TOKEN_OPTIONAL_OPS = {"resolve-contradictions"}


@mcp.tool()
def steward_inspect(
    operation: str,
    namespace: str = "",
    threshold: float = 0.85,
    days_since_access: int = 90,
    type: str = "fact",
    filter: str = "",
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Inspect workspace data quality. Returns structured findings without making any changes.

    Operations: duplicates, contradictions, stale, expired, orphans,
    key-taxonomy, stale-embeddings, cross-workspace-duplicates.

    Args:
        operation: The inspection operation to run.
        namespace: Memory namespace (defaults to active workspace).
            Ignored for cross-workspace-duplicates (admin-only).
        threshold: Similarity threshold for duplicate detection (0-1). Default: 0.85.
        days_since_access: Number of days without access to consider stale. Default: 90.
        type: Item type to inspect ("fact" or "note"). Default: "fact".
        filter: Status filter for contradictions: "pending" (default), "resolved", or "all".
        limit: Maximum items to return. Default: 50.
        offset: Pagination offset. Default: 0.

    Examples:
        steward_inspect("duplicates")
        steward_inspect("stale", days_since_access=60)
        steward_inspect("contradictions", limit=10)
        steward_inspect("contradictions", filter="all")
        steward_inspect("cross-workspace-duplicates", threshold=0.9)
    """
    if operation not in _STEWARD_INSPECT_OPS:
        return (
            f"Error: unknown inspect operation '{operation}'. "
            f"Valid operations: {', '.join(sorted(_STEWARD_INSPECT_OPS))}"
        )

    if operation == "cross-workspace-duplicates":
        params: dict = {"threshold": threshold, "limit": limit}
    else:
        ns = _resolve_tool_namespace(namespace)
        if not ns:
            return _no_workspace_error()
        params = {"namespace": ns}

        if operation == "duplicates":
            params.update({"type": type, "threshold": threshold, "limit": limit})
        elif operation == "contradictions":
            status = filter if filter else "pending"
            params.update({"status": status, "limit": limit, "offset": offset})
        elif operation == "stale":
            params.update({"days_since_access": days_since_access, "limit": limit})
        elif operation == "expired":
            params.update({"limit": limit, "offset": offset})
        elif operation == "orphans":
            params.update({"type": type, "limit": limit, "offset": offset})
        elif operation == "stale-embeddings":
            params.update({"limit": limit, "offset": offset})

    try:
        with _client() as c:
            r = c.get(f"/ops/steward/inspect/{operation}", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "steward_inspect")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (steward_inspect): cannot reach Cornerstone API — {e}"

    total = data.get("total", 0)
    items = data.get(
        "items",
        data.get("groups", data.get("inconsistencies", data.get("pairs", []))),
    )
    ns_display = params.get("namespace", "all workspaces")

    lines = [f"Inspection: {operation} (namespace: {ns_display})"]
    lines.append(f"Total found: {total}")
    lines.append("")

    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                parts = []
                for k, v in item.items():
                    if isinstance(v, (list, dict)):
                        parts.append(f"  {k}: {json.dumps(v, default=str)[:200]}")
                    else:
                        parts.append(f"  {k}: {v}")
                lines.append("\n".join(parts))
                lines.append("---")
            else:
                lines.append(f"  {item}")
    elif isinstance(items, dict):
        for k, v in items.items():
            lines.append(f"  {k}: {v}")

    result = "\n".join(lines)
    session_buffer.record(
        tool_name="steward_inspect",
        tool_params={"operation": operation, "namespace": namespace, "limit": limit},
        result_summary=f"{operation}: {total} items found",
    )
    return result


@mcp.tool()
def steward_advise(
    operation: str,
    items: str,
    namespace: str = "",
    item_type: str = "fact",
) -> str:
    """Get AI-powered recommendations for maintenance decisions. Read-only — never modifies data.

    Pass items as a JSON string. The AI analyzes the items and returns
    actionable recommendations.

    Operations: merge, consolidate, stale-review, key-taxonomy, contradictions.

    Args:
        operation: The advise operation to run.
        items: JSON string containing the items to analyze. Structure depends
               on operation (e.g., list of duplicate pairs for merge, list of
               facts for consolidate).
        namespace: Memory namespace (defaults to active workspace).
        item_type: Item type context ("fact" or "note"). Default: "fact".
            Used by merge operation.

    Examples:
        steward_advise("merge", '[{"id": "a", "key": "foo"}, {"id": "b", "key": "foo_bar"}]')
        steward_advise("contradictions", '[{"fact_a": {...}, "fact_b": {...}}]')
    """
    if operation not in _STEWARD_ADVISE_OPS:
        return (
            f"Error: unknown advise operation '{operation}'. "
            f"Valid operations: {', '.join(sorted(_STEWARD_ADVISE_OPS))}"
        )

    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()

    try:
        parsed_items = json.loads(items)
    except (json.JSONDecodeError, TypeError) as e:
        return f"Error: invalid JSON in 'items' parameter: {e}"

    if operation == "merge":
        body = {"namespace": ns, "items": parsed_items, "item_type": item_type}
    elif operation == "consolidate":
        body = {"namespace": ns, "facts": parsed_items}
    elif operation == "stale-review":
        body = {"namespace": ns, "items": parsed_items}
    elif operation == "key-taxonomy":
        body = {"namespace": ns, "inconsistencies": parsed_items}
    elif operation == "contradictions":
        body = {"namespace": ns, "pairs": parsed_items}
    else:
        body = {"namespace": ns, "items": parsed_items}

    try:
        with httpx.Client(
            base_url=CORNERSTONE_URL, headers=_headers(), timeout=60
        ) as c:
            r = c.post(f"/ops/steward/advise/{operation}", json=body)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "steward_advise")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (steward_advise): cannot reach Cornerstone API — {e}"

    lines = [f"Advise: {operation} (namespace: {ns})", ""]

    if data.get("parse_error"):
        lines.append(f"Warning: backend parse error: {data['parse_error']}")
        lines.append(
            f"Raw response: {json.dumps(data.get('raw_response', ''), default=str)[:500]}"
        )
        lines.append("")

    recommendations = data.get(
        "recommendations", data.get("proposals", data.get("advice", []))
    )
    if isinstance(recommendations, list):
        for i, rec in enumerate(recommendations, 1):
            if isinstance(rec, dict):
                lines.append(f"Recommendation {i}:")
                for k, v in rec.items():
                    lines.append(f"  {k}: {v}")
                lines.append("")
            else:
                lines.append(f"  {i}. {rec}")
    elif isinstance(recommendations, dict):
        for k, v in recommendations.items():
            lines.append(f"  {k}: {v}")
    elif isinstance(recommendations, str):
        lines.append(recommendations)

    model_info_parts = []
    if data.get("model_used"):
        model_info_parts.append(f"Model: {data['model_used']}")
    if data.get("tokens_input") is not None:
        model_info_parts.append(f"Tokens in: {data['tokens_input']}")
    if data.get("tokens_output") is not None:
        model_info_parts.append(f"Tokens out: {data['tokens_output']}")
    if data.get("reasoning_time_ms"):
        model_info_parts.append(f"Reasoning time: {data['reasoning_time_ms']}ms")
    if model_info_parts:
        lines.append("")
        lines.append(" | ".join(model_info_parts))

    result = "\n".join(lines)
    session_buffer.record(
        tool_name="steward_advise",
        tool_params={
            "operation": operation,
            "namespace": namespace,
            "item_type": item_type,
        },
        result_summary=f"{operation}: advise complete",
    )
    return result


@mcp.tool()
def steward_preview(
    operation: str,
    namespace: str = "",
    params: str = "{}",
) -> str:
    """Preview a maintenance operation without executing it. Read-only simulation.

    Returns the exact changes that would be made and a confirmation_token
    needed to apply. No data is modified.

    Operations: merge-duplicates, merge-notes, archive-stale, delete-by-filter,
    consolidate-facts, reembed-stale, rename-keys, resolve-contradictions.

    Note: resolve-contradictions skips preview — use steward_apply directly.

    Args:
        operation: The mutate operation to preview.
        namespace: Memory namespace (defaults to active workspace).
        params: JSON string of operation-specific parameters.

    Examples:
        steward_preview("merge-duplicates", params='{"threshold": 0.9}')
        steward_preview("archive-stale", params='{"days_since_access": 60}')
    """
    if operation not in _STEWARD_MUTATE_OPS:
        return (
            f"Error: unknown mutate operation '{operation}'. "
            f"Valid operations: {', '.join(sorted(_STEWARD_MUTATE_OPS))}"
        )

    if operation in _TOKEN_OPTIONAL_OPS:
        return (
            f"{operation} does not use a preview step. "
            f"Use steward_apply(\"{operation}\", params='...') directly."
        )

    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()

    try:
        parsed_params = json.loads(params)
    except (json.JSONDecodeError, TypeError) as e:
        return f"Error: invalid JSON in 'params' parameter: {e}"

    parsed_params["namespace"] = ns

    try:
        with _client() as c:
            r = c.post(
                f"/ops/steward/mutate/{operation}/preview", json=parsed_params
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "steward_preview")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (steward_preview): cannot reach Cornerstone API — {e}"

    lines = [f"Preview: {operation} (namespace: {ns})", ""]

    confirmation_token = data.get("confirmation_token", "")
    expires_at = data.get("expires_at", "")

    for k, v in data.items():
        if k in ("confirmation_token", "expires_at"):
            continue
        if isinstance(v, (list, dict)):
            lines.append(f"{k}: {json.dumps(v, default=str)[:500]}")
        else:
            lines.append(f"{k}: {v}")

    lines.append("")
    if confirmation_token:
        lines.append(f"confirmation_token: {confirmation_token}")
    if expires_at:
        lines.append(f"expires_at: {expires_at}")
    lines.append("")
    lines.append(
        "This is a preview. To apply, use steward_apply with the confirmation_token."
    )

    result = "\n".join(lines)
    session_buffer.record(
        tool_name="steward_preview",
        tool_params={"operation": operation, "namespace": namespace},
        result_summary=(
            f"{operation}: preview generated, token={confirmation_token[:12]}..."
            if confirmation_token
            else f"{operation}: preview generated"
        ),
    )
    return result


@mcp.tool()
def steward_apply(
    operation: str,
    confirmation_token: str,
    namespace: str = "",
    params: str = "{}",
) -> str:
    """Execute a previously previewed maintenance operation. DESTRUCTIVE — this modifies data.

    Requires a valid confirmation_token from steward_preview. Tokens expire
    after 10 minutes.

    Operations: merge-duplicates, merge-notes, archive-stale, delete-by-filter,
    consolidate-facts, reembed-stale, rename-keys, resolve-contradictions.

    resolve-contradictions does not require a token. Pass params with:
      {"ids": ["uuid1", ...], "resolution": "keep_new"}  — resolve by ID
      {"filter": "pending", "resolution": "keep_new"}     — bulk resolve

    Args:
        operation: The mutate operation to apply.
        confirmation_token: Token from steward_preview (required except resolve-contradictions).
        namespace: Memory namespace (defaults to active workspace).
        params: JSON string of operation-specific parameters.

    Examples:
        steward_apply("merge-duplicates", confirmation_token="tok_abc123...")
        steward_apply("resolve-contradictions", params='{"ids": ["uuid1"], "resolution": "keep_new"}')
    """
    if operation not in _STEWARD_MUTATE_OPS:
        return (
            f"Error: unknown mutate operation '{operation}'. "
            f"Valid operations: {', '.join(sorted(_STEWARD_MUTATE_OPS))}"
        )

    if not confirmation_token and operation not in _TOKEN_OPTIONAL_OPS:
        return "Error: confirmation_token is required. Run steward_preview first to get one."

    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()

    try:
        parsed_params = json.loads(params)
    except (json.JSONDecodeError, TypeError) as e:
        return f"Error: invalid JSON in 'params' parameter: {e}"

    parsed_params["namespace"] = ns
    parsed_params["confirmation_token"] = confirmation_token

    try:
        with _client() as c:
            r = c.post(
                f"/ops/steward/mutate/{operation}/apply", json=parsed_params
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "steward_apply")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (steward_apply): cannot reach Cornerstone API — {e}"

    lines = [f"Applied: {operation} (namespace: {ns})", ""]

    for k, v in data.items():
        if isinstance(v, (list, dict)):
            lines.append(f"{k}: {json.dumps(v, default=str)[:500]}")
        else:
            lines.append(f"{k}: {v}")

    result = "\n".join(lines)
    session_buffer.record(
        tool_name="steward_apply",
        tool_params={"operation": operation, "namespace": namespace},
        result_summary=f"{operation}: applied successfully",
    )
    return result


@mcp.tool()
def steward_status(namespace: str = "") -> str:
    """Get a summary of workspace health across all steward dimensions.

    Returns counts of duplicates, contradictions, stale items, expired facts,
    orphans, key inconsistencies, and stale embeddings. Use this as a
    starting point to identify areas that need maintenance.

    Args:
        namespace: Memory namespace (defaults to active workspace).

    Examples:
        steward_status()
        steward_status(namespace="my-workspace")
    """
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()

    dimensions = [
        ("duplicates", "Duplicate candidates"),
        ("contradictions", "Contradictions"),
        ("stale", "Stale items (90+ days)"),
        ("expired", "Expired facts"),
        ("orphans", "Orphan notes"),
        ("key-taxonomy", "Key inconsistencies"),
        ("stale-embeddings", "Stale embeddings"),
    ]

    counts: dict[str, int | str] = {}
    for op, _label in dimensions:
        try:
            inspect_params: dict = {"namespace": ns, "limit": 1}
            if op == "contradictions":
                inspect_params["status"] = "pending"
            with _client() as c:
                r = c.get(
                    f"/ops/steward/inspect/{op}",
                    params=inspect_params,
                )
                r.raise_for_status()
                data = r.json()
                counts[op] = data.get("total", 0)
        except Exception:
            counts[op] = "error"

    total_issues = sum(v for v in counts.values() if isinstance(v, int))

    lines = [
        f"Workspace Health Summary (namespace: {ns})",
        "\u2500" * 37,
    ]
    for op, label in dimensions:
        val = counts[op]
        if isinstance(val, int):
            lines.append(f"  {label + ':':<28s} {val:>5}")
        else:
            lines.append(f"  {label + ':':<28s} {'ERR':>5}")
    lines.append("\u2500" * 37)
    lines.append(f"  {'Total issues:':<28s} {total_issues:>5}")

    result = "\n".join(lines)
    session_buffer.record(
        tool_name="steward_status",
        tool_params={"namespace": namespace},
        result_summary=f"Health summary: {total_issues} total issues",
    )
    return result
