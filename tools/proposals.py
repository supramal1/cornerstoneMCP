"""MC-2 proposal review tools: mc_list_proposals, mc_promote_facts, mc_reject_facts.

Propose-then-approve human gate for agent-proposed facts. Agents write
proposals via cornerstone-api; a human reviewer (HOD) lists them here,
then promotes or rejects in batches. On promote/reject the inbound
request's X-Forwarded-User-Email header (injected, JWT-verified, by the
co-mcp-gateway) is relayed verbatim to the API so the decision is
attributed to the human reviewer. The API only honours the header when
this service's API key is a trusted forwarder, so relaying is safe by
default; without the header the decision is recorded with no forwarded
identity — the email is never invented or defaulted here.
"""

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

_FORWARDED_USER_HEADER = "X-Forwarded-User-Email"

_REVIEW_HINT = (
    "Promote: mc_promote_facts(ids). "
    "Reject: mc_reject_facts(items=[{id, reason}]) — "
    "reason required, stored verbatim."
)


def _forwarded_identity_headers() -> dict[str, str]:
    """Relay the reviewer's identity header from the inbound MCP request.

    On the streamable-http transport the MCP SDK attaches the underlying
    Starlette request to the per-request context
    (mcp.get_context().request_context.request), so the co-mcp-gateway in
    front of this server can pass X-Forwarded-User-Email and we relay it
    on the outbound API call. The API only honours it when THIS service's
    API key is a trusted forwarder, so relaying is safe by default. On
    stdio transport there is no HTTP request and this returns {} — the
    API then records the decision without a forwarded identity rather
    than failing. Never invent or default the email.
    """
    try:
        ctx = mcp.get_context()
        request_context = getattr(ctx, "request_context", None)
        http_request = getattr(request_context, "request", None)
        if http_request is not None:
            email = (http_request.headers.get(_FORWARDED_USER_HEADER) or "").strip()
            if email:
                return {_FORWARDED_USER_HEADER: email}
    except Exception:
        pass
    return {}


@mcp.tool()
def mc_list_proposals(status: str = "proposed", namespace: str = "") -> str:
    """List agent-proposed facts awaiting human review (MC-2).

    This IS the review surface: each proposal shows its full value plus
    the citation link/locator and supersession framing, so the reviewer
    can promote or reject without re-reading the source unless they want
    to verify it.

    Args:
        status: Filter — 'proposed' (default, awaiting review),
            'promoted', or 'rejected'.
        namespace: Memory namespace (defaults to active workspace).
    """
    valid_statuses = {"proposed", "promoted", "rejected"}
    if status not in valid_statuses:
        return f"Error: status must be one of: {', '.join(sorted(valid_statuses))}"
    ns = _resolve_tool_namespace(namespace)
    if not ns:
        return _no_workspace_error()
    try:
        with _client() as c:
            r = c.get(
                "/memory/fact/proposals",
                params={"status": status, "namespace": ns},
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "mc_list_proposals")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (mc_list_proposals): cannot reach Cornerstone API — {e}"

    proposals = data.get("proposals", [])
    if not proposals:
        session_buffer.record(
            tool_name="mc_list_proposals",
            tool_params={"status": status, "namespace": ns},
            result_summary="No proposals found",
        )
        return f"[workspace: {ns}] No '{status}' proposals."

    lines = [f"[workspace: {ns}] {len(proposals)} '{status}' proposal(s):"]
    for p in proposals:
        citation = p.get("citation") or {}
        supersedes = p.get("supersedes") or None
        lines.append("")
        lines.append(f"- {p.get('id', '?')}  key: {p.get('key', '?')}")
        lines.append(f"  value: {p.get('value', '')}")
        link = citation.get("link") or "(no link)"
        locator = citation.get("locator") or ""
        lines.append(f"  citation: {link}" + (f" — {locator}" if locator else ""))
        if supersedes:
            lines.append(
                f"  SUPERSEDES: {supersedes.get('existing_key', '?')} — "
                f"{supersedes.get('framing', '')}"
            )
        if p.get("rejected_reason"):
            lines.append(f"  rejected_reason: {p['rejected_reason']}")
    lines.append("")
    lines.append(_REVIEW_HINT)
    session_buffer.record(
        tool_name="mc_list_proposals",
        tool_params={"status": status, "namespace": ns},
        result_summary=f"Found {len(proposals)} proposals",
    )
    return "\n".join(lines)


@mcp.tool()
def mc_promote_facts(ids: list[str]) -> str:
    """Promote proposed facts to current memory (MC-2 human gate).

    One batched call: each id is decided independently and the proposal's
    reviewed key/value lands via the canonical fact write — no re-entry,
    no transformation. Requires human (HOD) write authority; the
    reviewer's identity (forwarded by the gateway) is recorded as
    decided_by on every promoted proposal.

    Args:
        ids: Proposal ids (dp-...) to promote.
    """
    if not ids:
        return "Error: no proposal ids given. Pass ids=[...] from mc_list_proposals."
    try:
        with _client() as c:
            r = c.post(
                "/memory/fact/promote",
                json={"ids": ids},
                headers=_forwarded_identity_headers(),
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "mc_promote_facts")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (mc_promote_facts): cannot reach Cornerstone API — {e}"

    results = data.get("results", [])
    lines = [f"Promotion results ({len(results)}):"]
    for res in results:
        detail = res.get("detail")
        suffix = f" — {detail}" if detail else ""
        lines.append(f"- {res.get('id', '?')}: {res.get('outcome', '?')}{suffix}")
    session_buffer.record(
        tool_name="mc_promote_facts",
        tool_params={"ids": ids},
        result_summary=f"Promoted batch of {len(results)}",
    )
    return "\n".join(lines)


@mcp.tool()
def mc_reject_facts(items: list[dict]) -> str:
    """Reject proposed facts, each with a required reason (MC-2 human gate).

    Each item is {"id": "dp-...", "reason": "..."}. The reason is
    required per item and recorded VERBATIM on the proposal — it is eval
    feedstock, so write it deliberately. Any item missing a reason
    refuses the whole call before anything reaches the API. Requires
    human (HOD) write authority; the reviewer's identity (forwarded by
    the gateway) is recorded as decided_by.

    Args:
        items: List of {"id": str, "reason": str} dicts. reason is
            required per item.
    """
    if not items:
        return (
            "Error: no items given. "
            "Pass items=[{id, reason}] — reason required per item."
        )
    missing = [
        str((it or {}).get("id") or "(missing id)")
        for it in items
        if not str((it or {}).get("reason") or "").strip()
    ]
    if missing:
        return (
            "Error: rejection refused — every item needs a non-empty reason "
            "(it is stored verbatim and feeds evals). "
            f"Missing/empty reason for: {', '.join(missing)}. "
            "No proposals were rejected."
        )
    try:
        with _client() as c:
            r = c.post(
                "/memory/fact/reject",
                json={"items": items},
                headers=_forwarded_identity_headers(),
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "mc_reject_facts")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (mc_reject_facts): cannot reach Cornerstone API — {e}"

    results = data.get("results", [])
    submitted_reasons = {str((it or {}).get("id")): (it or {}).get("reason") for it in items}
    lines = [f"Rejection results ({len(results)}):"]
    for res in results:
        rid = res.get("id", "?")
        outcome = res.get("outcome", "?")
        detail = res.get("detail")
        if outcome == "rejected":
            recorded = detail or submitted_reasons.get(str(rid), "")
            lines.append(f"- {rid}: rejected — reason recorded: {recorded}")
        else:
            suffix = f" — {detail}" if detail else ""
            lines.append(f"- {rid}: {outcome}{suffix}")
    session_buffer.record(
        tool_name="mc_reject_facts",
        tool_params={"item_count": len(items)},
        result_summary=f"Rejected batch of {len(results)}",
    )
    return "\n".join(lines)
