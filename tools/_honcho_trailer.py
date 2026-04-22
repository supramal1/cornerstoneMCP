"""Shared Honcho synthesis helpers for MCP tools.

The Cornerstone backend returns peer-card data in
``response["stats"]["peer_card"]`` with three fields of interest:

    content      — the ABOUT block text, or None when no card
    peer_id      — the Honcho-safe peer id we actually asked about
    refreshed_at — unix timestamp of the last ingest observed in the
                   backend process, or None (process-local cache miss)

These helpers render that dict into display strings. They never call
Honcho themselves and never raise — a missing / incomplete peer_card
block just produces an empty string, which callers append/prepend as a
no-op. That keeps retrieval paths synthesis-layer clean: if Honcho
flaked or this principal has no card yet, every MCP tool renders
cleanly without personalisation.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

_ABOUT_OPEN = "=== ABOUT THE PERSON ASKING ==="
_ABOUT_CLOSE = "=== END ABOUT THE PERSON ASKING ==="


def format_about_block(peer_card: Mapping[str, Any] | None) -> str:
    """Render the ABOUT block from a ``stats.peer_card`` dict.

    Returns "" when the block should be suppressed (no peer_card,
    content missing, content empty/whitespace).
    """
    if not peer_card:
        return ""
    content = peer_card.get("content")
    if not content:
        return ""
    text = str(content).strip()
    if not text:
        return ""
    return f"{_ABOUT_OPEN}\n{text}\n{_ABOUT_CLOSE}"


def format_trailer(peer_card: Mapping[str, Any] | None) -> str:
    """Render the Honcho observability trailer.

    Format::

        Honcho peer: <peer_id>, summary refreshed <relative time>

    Returns "" when no peer_id is available — there is nothing
    debuggable to surface in that case. ``refreshed_at`` missing
    renders as ``"unknown"`` so operators can spot cold instances /
    horizontally scaled-out pods where the ingest-seen cache is empty.
    """
    if not peer_card:
        return ""
    peer_id = peer_card.get("peer_id")
    if not peer_id:
        return ""
    relative = _format_relative(peer_card.get("refreshed_at"))
    return f"Honcho peer: {peer_id}, summary refreshed {relative}"


def _format_relative(refreshed_at: float | None, now: float | None = None) -> str:
    """Best-effort short human delta. Returns ``"unknown"`` for missing
    or future timestamps (clock skew shouldn't render as negative).
    """
    if refreshed_at is None:
        return "unknown"
    current = now if now is not None else time.time()
    delta = current - float(refreshed_at)
    if delta < 0:
        return "unknown"
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
