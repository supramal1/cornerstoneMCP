"""Connect-to-existing flow — the fast path for staff onboarding.

Target end-to-end time: under 5 minutes, from launching the wizard to
having every detected IDE configured with a working Cornerstone MCP
connector.

Flow
----
1. Prompt (or recall) the instance URL, ping its /health.
2. Ask auth method: Google OAuth (default) or API key.
3. Run the chosen auth flow, verify, save credentials at ~/.cornerstone.
4. Detect all five supported tools, render a status table.
5. Offer to configure each detected tool; write the MCP entry.
6. Render final summary with next steps.
"""

from __future__ import annotations

import httpx
import questionary

from . import auth, tools, ui
from .config import (
    DEFAULT_INSTANCE_URL,
    TOOLS,
    load_instance_url,
    save_credentials,
    save_instance_url,
)


def run_connect(*, api_key: str | None = None) -> None:
    ui.divider("Connect to Existing Cornerstone Instance")
    ui.info("Target: under 5 minutes.")

    instance_url = _prompt_instance_url()
    if not _ping_instance(instance_url):
        if not questionary.confirm(
            "Instance didn't respond to /health. Continue anyway?", default=False
        ).unsafe_ask():
            ui.skip("Connect cancelled")
            return

    creds = _run_auth(instance_url, prefill_key=api_key)
    if creds is None:
        return

    save_credentials(
        token=creds["token"],
        email=creds["email"],
        instance_url=instance_url,
        expires_at=creds.get("expires_at"),
        method=creds["method"],
    )
    save_instance_url(instance_url)
    ui.ok("Credentials saved to ~/.cornerstone/credentials.json")

    configured = _configure_detected_tools(instance_url, creds["token"])
    _render_summary(instance_url, creds["email"], configured)


# ── URL + health ─────────────────────────────────────────────────────────


def _prompt_instance_url() -> str:
    remembered = load_instance_url()
    default = remembered or DEFAULT_INSTANCE_URL
    url = questionary.text(
        "Cornerstone MCP URL",
        default=default,
        instruction="https://<your-instance>.run.app",
    ).unsafe_ask()
    return url.rstrip("/") if url else default


def _ping_instance(url: str) -> bool:
    # The MCP server doesn't expose /health — use the OAuth 2.1 discovery
    # endpoint instead. A 200 there proves the service is up AND has the
    # auth surface the CLI depends on.
    probe = f"{url}/.well-known/oauth-authorization-server"
    with ui.spinner(f"Checking {url}..."):
        try:
            resp = httpx.get(probe, timeout=8.0)
        except httpx.HTTPError as exc:
            ui.fail(f"Could not reach {url}: {exc}")
            return False
    if 200 <= resp.status_code < 300:
        ui.ok(f"Instance reachable (HTTP {resp.status_code})")
        return True
    ui.fail(f"Instance returned HTTP {resp.status_code}")
    return False


# ── Auth dispatch ────────────────────────────────────────────────────────


def _run_auth(instance_url: str, *, prefill_key: str | None = None) -> dict | None:
    # Headless / CI path: key supplied up front, skip the menu entirely.
    if prefill_key:
        try:
            return auth.api_key_flow(instance_url, prefill=prefill_key)
        except auth.AuthError as exc:
            ui.fail(f"API key rejected: {exc}")
            return None

    method = questionary.select(
        "How do you want to sign in?",
        choices=[
            questionary.Choice(
                "Browser sign-in (recommended — OAuth 2.1 via MCP server)",
                value="oauth",
            ),
            questionary.Choice("API key (paste from an admin)", value="api_key"),
        ],
        default="oauth",
    ).unsafe_ask()

    try:
        if method == "oauth":
            return auth.oauth_login_flow(instance_url)
        return auth.api_key_flow(instance_url)
    except auth.AuthError as exc:
        ui.fail(f"Authentication failed: {exc}")
        if method == "oauth" and questionary.confirm(
            "Fall back to API key?", default=True
        ).unsafe_ask():
            try:
                return auth.api_key_flow(instance_url)
            except auth.AuthError as exc2:
                ui.fail(f"API key also failed: {exc2}")
        return None


# ── Tool detection + configuration ───────────────────────────────────────


def _configure_detected_tools(instance_url: str, token: str) -> list[tuple[str, bool]]:
    ui.divider("Detected Tools")
    statuses = tools.detect_all()
    _render_tool_table(statuses)

    detected = [s for s in statuses if s.detected]
    if not detected:
        ui.warn("No supported MCP tools detected.")
        ui.hint("Install Claude Code, Claude Desktop, Cursor, Codex, or Windsurf and re-run.")
        return []

    choices = [
        questionary.Choice(
            title=(
                f"{s.name}"
                + (" [already configured]" if s.configured else "")
                + (f"  ({s.version})" if s.version else "")
            ),
            value=s.key,
            checked=not s.configured,
        )
        for s in detected
    ]

    to_configure = questionary.checkbox(
        "Select tools to configure (space toggles, enter confirms):",
        choices=choices,
    ).unsafe_ask() or []

    results: list[tuple[str, bool]] = []
    for key in to_configure:
        spec = TOOLS[key]
        if key == "claude_desktop":
            ui.panel(
                tools.CLAUDE_DESKTOP_INSTRUCTIONS.format(mcp_url=instance_url),
                title="Claude Desktop — use the Connectors UI",
                style="blue",
            )
            use_bridge = questionary.confirm(
                "Alternatively, configure a local bridge? "
                "(for older Claude Desktop versions without Connectors UI)",
                default=False,
            ).unsafe_ask()
            if not use_bridge:
                results.append((spec.name, True))
                continue
            try:
                path = tools.configure_tool(spec, instance_url, token, bridge=True)
                ui.ok(f"{spec.name} bridge → {path}")
                results.append((spec.name, True))
            except Exception as exc:
                ui.fail(f"{spec.name}: {exc}")
                results.append((spec.name, False))
            continue
        try:
            path = tools.configure_tool(spec, instance_url, token)
            ui.ok(f"{spec.name} → {path}")
            results.append((spec.name, True))
        except Exception as exc:
            ui.fail(f"{spec.name}: {exc}")
            results.append((spec.name, False))
    return results


def _render_tool_table(statuses: list[tools.ToolStatus]) -> None:
    tbl = ui.table("Tool", "Installed", "Configured", "Version")
    for s in statuses:
        installed = "[green]yes[/green]" if s.installed else "[dim]no[/dim]"
        configured = "[green]yes[/green]" if s.configured else "[yellow]no[/yellow]"
        version = s.version or "[dim]—[/dim]"
        tbl.add_row(s.name, installed, configured, version)
    ui.render(tbl)


# ── Summary ──────────────────────────────────────────────────────────────


def _render_summary(instance_url: str, email: str, configured: list[tuple[str, bool]]) -> None:
    ui.divider("Connected")
    body_lines = [
        f"[bold]Instance:[/bold]  {instance_url}",
        f"[bold]Signed in:[/bold] {email}",
        "",
        "[bold]Tools configured:[/bold]",
    ]
    if configured:
        for name, ok in configured:
            marker = "[green]✓[/green]" if ok else "[red]✗[/red]"
            body_lines.append(f"  {marker} {name}")
    else:
        body_lines.append("  [dim]none[/dim]")

    body_lines.extend(
        [
            "",
            "[bold]Next steps:[/bold]",
            "  1. Restart any tool you just configured",
            "  2. Ask it: 'what facts do you have about me in Cornerstone?'",
            "  3. Run this wizard again in 'Health check' mode to verify",
        ]
    )
    ui.panel("\n".join(body_lines), title="Ready", style="green")
