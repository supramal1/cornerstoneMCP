"""Identity-anchor tools: update_identity_anchor.

Thin wrapper over PATCH /admin/identity_anchors/{key}. Lets the user fix
the [IDENTITY] block (user_role, user_organization, pronoun_mapping,
etc.) from Claude Desktop without needing direct Supabase access. Admin
capability on the caller's credential is enforced server-side by the
auth middleware — no extra auth logic needed here.
"""

from __future__ import annotations

from typing import Optional

import httpx

from core import (
    _client,
    _format_http_error,
    mcp,
    session_buffer,
)


@mcp.tool()
def update_identity_anchor(
    key: str,
    value: str,
    namespace: str = "default",
    priority: Optional[int] = None,
    locked: Optional[bool] = None,
) -> str:
    """Update (or create) an identity_anchor row for the [IDENTITY] block.

    Identity anchors feed the [IDENTITY] section Cornerstone emits on every
    get_context/search response. Use this to correct a wrong user_role,
    user_organization, pronoun_mapping, user_name, self_entity_id, or
    self_entity_key.

    Requires admin capability on the calling credential.

    Args:
        key: Anchor key (e.g., "user_role", "pronoun_mapping",
             "user_organization").
        value: New value to store.
        namespace: Target namespace. Defaults to "default".
        priority: Optional priority override. Current convention:
                  100 for identity-name anchors, 95 for pronoun_mapping,
                  90 for user_role/user_organization.
        locked: Optional locked-flag override. Locked is advisory only
                — it does not block future updates.

    Examples:
        update_identity_anchor("user_role", "Head of AI Ops at Charlie Oscar")
        update_identity_anchor("pronoun_mapping", "he/him")
        update_identity_anchor("user_organization", "Charlie Oscar")
    """
    payload: dict = {"value": value}
    if priority is not None:
        payload["priority"] = priority
    if locked is not None:
        payload["locked"] = locked
    if namespace:
        payload["namespace"] = namespace

    try:
        with _client() as c:
            r = c.patch(f"/admin/identity_anchors/{key}", json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return _format_http_error(e, "update_identity_anchor")
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return f"Error (update_identity_anchor): cannot reach Cornerstone API — {e}"

    session_buffer.record(
        tool_name="update_identity_anchor",
        result_summary=f"Updated identity anchor '{key}' in namespace '{namespace}'",
    )

    return (
        f"Updated identity anchor `{key}` in namespace `{namespace}`.\n"
        f"  value:    {data.get('value')}\n"
        f"  priority: {data.get('priority')}\n"
        f"  locked:   {data.get('locked')}"
    )
