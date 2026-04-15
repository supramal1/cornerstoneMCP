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


def run_connect() -> None:
    ui.divider("Connect to Existing Cornerstone Instance")
    ui.info("Target: under 5 minutes.")

    instance_url = _prompt_instance_url()
    if not _ping_instance(instance_url):
        if not questionary.confirm(
            "Instance didn't respond to /health. Continue anyway?", default=False
        ).unsafe_ask():
            ui.skip("Connect cancelled")
            return

    creds = _run_auth(instance_url)
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
    with ui.spinner(f"Checking {url}..."):
        try:
            resp = httpx.get(f"{url}/health", timeout=8.0)
        except httpx.HTTPError as exc:
            ui.fail(f"Could not reach {url}: {exc}")
            return False
    if 200 <= resp.status_code < 300:
        ui.ok(f"Instance reachable (HTTP {resp.status_code})")
        return True
    ui.fail(f"Instance returned HTTP {resp.status_code}")
    return False


# ── Auth dispatch ────────────────────────────────────────────────────────


def _run_auth(instance_url: str) -> dict | None:
    method = questionary.select(
        "How do you want to sign in?",
        choices=[
            questionary.Choice("Google (recommended — uses browser)", value="google"),
            questionary.Choice("API key (paste from an admin)", value="api_key"),
        ],
        default="google",
    ).unsafe_ask()

    try:
        if method == "google":
            return auth.google_oauth_flow(instance_url)
        return auth.api_key_flow(instance_url)
    except auth.AuthError as exc:
        ui.fail(f"Authentication failed: {exc}")
        if method == "google" and questionary.confirm(
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
