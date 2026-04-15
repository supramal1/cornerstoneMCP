"""Health check mode — read-only instance diagnostics.

Pulls stats from the MCP server and its upstream API, then renders
a single Rich panel showing:

- Instance reachability + HTTP latency
- Who you're signed in as
- Workspace / namespace count
- Memory counts (facts, notes, documents)
- Embedding coverage %
- Duplicate / contradiction / staleness counts
- Per-tool MCP configuration status
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from . import auth, tools, ui
from .config import load_credentials, load_instance_url


def run_health_check() -> None:
    ui.divider("Health Check")

    creds = load_credentials()
    instance_url = (creds or {}).get("instance_url") or load_instance_url()
    if not creds or not instance_url:
        ui.fail("No saved credentials. Run 'Connect to existing instance' first.")
        return

    email = creds.get("email", "unknown")
    token = creds["token"]
    method = creds.get("method", "api_key")

    # 1. Reachability
    reach = _check_reachability(instance_url)

    # 2. Auth
    auth_ok = _check_auth(instance_url, token, method)

    # 3. Instance stats (best effort)
    stats = _fetch_stats(instance_url, token, method) if auth_ok else {}

    # 4. Tool configuration
    tool_statuses = tools.detect_all()

    _render_report(
        instance_url=instance_url,
        email=email,
        reach=reach,
        auth_ok=auth_ok,
        stats=stats,
        tool_statuses=tool_statuses,
    )


# ── Checks ───────────────────────────────────────────────────────────────


def _check_reachability(url: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        resp = httpx.get(f"{url}/health", timeout=8.0)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {"ok": 200 <= resp.status_code < 300, "status": resp.status_code, "latency_ms": latency_ms}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc), "latency_ms": None}


def _check_auth(url: str, token: str, method: str) -> bool:
    with ui.spinner("Verifying credentials..."):
        return auth.verify_token(url, token, method)


def _fetch_stats(url: str, token: str, method: str) -> dict[str, Any]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if method == "google":
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["X-API-Key"] = token

    # Try the canonical /ops/health first; fall back to /stats.
    for path in ("/ops/health", "/stats", "/context/stats"):
        try:
            resp = httpx.get(f"{url}{path}", headers=headers, timeout=10.0)
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                continue
    return {}


# ── Report ───────────────────────────────────────────────────────────────


def _render_report(
    *,
    instance_url: str,
    email: str,
    reach: dict[str, Any],
    auth_ok: bool,
    stats: dict[str, Any],
    tool_statuses: list[tools.ToolStatus],
) -> None:
    lines: list[str] = []

    lines.append(f"[bold]Instance:[/bold]  {instance_url}")
    lines.append(f"[bold]Identity:[/bold]  {email}")
    lines.append("")

    if reach.get("ok"):
        lines.append(f"  [green]✓[/green] reachable ([dim]{reach['latency_ms']} ms[/dim])")
    else:
        err = reach.get("error") or f"HTTP {reach.get('status')}"
        lines.append(f"  [red]✗[/red] unreachable ([dim]{err}[/dim])")

    lines.append(
        "  [green]✓[/green] authenticated" if auth_ok else "  [red]✗[/red] credentials rejected"
    )

    lines.append("")
    lines.append("[bold]Instance stats[/bold]")
    if not stats:
        lines.append("  [dim]no stats endpoint responded[/dim]")
    else:
        stat_rows = [
            ("Workspaces", _pluck(stats, "workspaces", "namespace_count", "namespaces")),
            ("Facts", _pluck(stats, "facts", "fact_count")),
            ("Notes", _pluck(stats, "notes", "note_count")),
            ("Documents", _pluck(stats, "documents", "document_count")),
            ("Embedding coverage", _pct(_pluck(stats, "embedding_coverage", "coverage"))),
            ("Duplicates", _pluck(stats, "duplicates", "duplicate_count")),
            ("Contradictions", _pluck(stats, "contradictions", "contradiction_count")),
            ("Stale", _pluck(stats, "stale", "stale_count")),
        ]
        for label, value in stat_rows:
            if value is None:
                continue
            lines.append(f"  [cyan]•[/cyan] {label:<20} {value}")

    lines.append("")
    lines.append("[bold]Tool configuration[/bold]")
    for s in tool_statuses:
        if s.configured:
            marker = "[green]✓[/green]"
            state = "configured"
        elif s.installed:
            marker = "[yellow]![/yellow]"
            state = "installed, not configured"
        else:
            marker = "[dim]○[/dim]"
            state = "not installed"
        lines.append(f"  {marker} {s.name:<18} [dim]{state}[/dim]")

    overall_ok = reach.get("ok") and auth_ok
    title = "Healthy" if overall_ok else "Attention needed"
    style = "green" if overall_ok else "yellow"
    ui.panel("\n".join(lines), title=title, style=style)


def _pluck(stats: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in stats:
            return stats[k]
        nested = stats.get("stats") or stats.get("counts")
        if isinstance(nested, dict) and k in nested:
            return nested[k]
    return None


def _pct(value: Any) -> str | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f <= 1.0:
        f *= 100
    return f"{f:.1f}%"
