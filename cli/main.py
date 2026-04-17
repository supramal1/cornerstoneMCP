"""Entry point: ``python3 -m cli.main``.

Shows the banner, dispatches to one of five modes, and handles graceful
Ctrl-C / error reporting.

Modes
-----
1. Connect to existing instance     → cli.connect.run_connect
2. Fresh install                    → cli.wizard.run_fresh_install
3. Health check                     → cli.health.run_health_check
4. Manage workspaces                → inlined below
5. Reconfigure tools                → inlined below
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx
import questionary

from . import connect, health, tools, ui, wizard
from .config import TOOLS, clear_credentials, load_credentials, load_instance_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cornerstone",
        description="Cornerstone CLI — setup, connection, and diagnostics wizard.",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument(
        "--mode",
        choices=["connect", "install", "health", "workspaces", "tools"],
        help="Skip the menu and jump directly to a mode",
    )
    parser.add_argument(
        "--key",
        help="API key for headless mode (skips interactive auth, connect mode only)",
    )
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print(f"cornerstone-cli {__version__}")
        return 0

    try:
        ui.banner()
        mode = args.mode or _choose_mode()
        if mode is None:
            ui.skip("Goodbye")
            return 0
        _dispatch(mode, api_key=args.key)
    except KeyboardInterrupt:
        ui.console.print()
        ui.skip("Interrupted — nothing saved beyond the last confirmed step")
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level guard
        ui.error_panel("Unexpected error", f"{type(exc).__name__}: {exc}")
        return 1
    return 0


# ── Mode selection ───────────────────────────────────────────────────────


_MODE_CHOICES = [
    ("connect", "Connect to existing instance", "Fastest — configures your tools against a running Cornerstone"),
    ("install", "Fresh install", "Stand up a brand-new Cornerstone instance end-to-end"),
    ("health", "Health check", "Verify your current credentials and tool configuration"),
    ("workspaces", "Manage workspaces", "List / switch the active Cornerstone workspace"),
    ("tools", "Reconfigure tools", "Add, update, or remove the Cornerstone MCP entry in each IDE"),
]


def _choose_mode() -> str | None:
    creds = load_credentials()
    if creds:
        ui.ok(f"Signed in as [bold]{creds.get('email', 'unknown')}[/bold]")
        ui.hint(f"Instance: {creds.get('instance_url', '—')}")
    else:
        ui.info("Not signed in yet. Pick 'Connect to existing instance' or 'Fresh install'.")

    choice = questionary.select(
        "What would you like to do?",
        choices=[
            questionary.Choice(title=f"{label}  —  {hint}", value=value)
            for value, label, hint in _MODE_CHOICES
        ] + [questionary.Choice(title="Quit", value=None)],
    ).unsafe_ask()
    return choice


def _dispatch(mode: str, *, api_key: str | None = None) -> None:
    if mode == "connect":
        connect.run_connect(api_key=api_key)
    elif mode == "install":
        wizard.run_fresh_install()
    elif mode == "health":
        health.run_health_check()
    elif mode == "workspaces":
        _run_workspaces()
    elif mode == "tools":
        _run_reconfigure_tools()


# ── Manage workspaces ────────────────────────────────────────────────────


def _run_workspaces() -> None:
    ui.divider("Manage Workspaces")
    creds = load_credentials()
    if not creds:
        ui.fail("Not signed in. Run 'Connect to existing instance' first.")
        return

    instance_url = creds.get("instance_url") or load_instance_url()
    headers = _auth_headers(creds)

    workspaces = _fetch_workspaces(instance_url, headers)
    if workspaces is None:
        ui.fail("Could not fetch workspaces from the instance.")
        return
    if not workspaces:
        ui.info("No workspaces found.")
        return

    tbl = ui.table("Name", "Namespace", "Role")
    for ws in workspaces:
        tbl.add_row(
            str(ws.get("name") or ws.get("label") or "—"),
            str(ws.get("namespace") or ws.get("id") or "—"),
            str(ws.get("role") or "—"),
        )
    ui.render(tbl)

    action = questionary.select(
        "Action:",
        choices=[
            questionary.Choice("Set default workspace", value="default"),
            questionary.Choice("Done", value="done"),
        ],
    ).unsafe_ask()

    if action == "default":
        target = questionary.select(
            "Which workspace?",
            choices=[
                questionary.Choice(title=ws.get("name", ws.get("namespace", "?")), value=ws.get("namespace"))
                for ws in workspaces
                if ws.get("namespace")
            ],
        ).unsafe_ask()
        if not target:
            return
        try:
            resp = httpx.post(
                f"{instance_url}/workspace/default",
                json={"namespace": target},
                headers=headers,
                timeout=10.0,
            )
            if 200 <= resp.status_code < 300:
                ui.ok(f"Default workspace set to {target}")
            else:
                ui.fail(f"Server returned {resp.status_code}: {resp.text[:200]}")
        except httpx.HTTPError as exc:
            ui.fail(f"Request failed: {exc}")


def _fetch_workspaces(instance_url: str, headers: dict[str, str]) -> list[dict[str, Any]] | None:
    for path in ("/workspaces", "/admin/namespaces", "/namespaces"):
        try:
            resp = httpx.get(f"{instance_url}{path}", headers=headers, timeout=10.0)
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                continue
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("workspaces", "namespaces", "items", "data"):
                    if key in data and isinstance(data[key], list):
                        return data[key]
    return None


# ── Reconfigure tools ────────────────────────────────────────────────────


def _run_reconfigure_tools() -> None:
    ui.divider("Reconfigure Tools")
    creds = load_credentials()
    if not creds:
        ui.fail("Not signed in. Run 'Connect to existing instance' first.")
        return

    instance_url = creds.get("instance_url", "")
    token = creds["token"]

    action = questionary.select(
        "What would you like to do?",
        choices=[
            questionary.Choice("Add/update the Cornerstone entry in one or more tools", value="add"),
            questionary.Choice("Remove the Cornerstone entry from one or more tools", value="remove"),
            questionary.Choice("Clear saved CLI credentials", value="clear"),
            questionary.Choice("Back", value="back"),
        ],
    ).unsafe_ask()

    if action == "back" or action is None:
        return

    if action == "clear":
        if questionary.confirm(
            "Really delete ~/.cornerstone/credentials.json?", default=False
        ).unsafe_ask():
            if clear_credentials():
                ui.ok("Credentials cleared")
            else:
                ui.info("No credentials file to clear")
        return

    statuses = tools.detect_all()
    choices = [
        questionary.Choice(
            title=f"{s.name}  ({'configured' if s.configured else 'not configured'})",
            value=s.key,
            checked=(action == "add" and not s.configured) or (action == "remove" and s.configured),
        )
        for s in statuses
        if s.detected
    ]
    if not choices:
        ui.warn("No detected tools to modify.")
        return

    selected = questionary.checkbox("Select tools:", choices=choices).unsafe_ask() or []

    for key in selected:
        spec = TOOLS[key]
        try:
            if action == "add":
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
                        ui.skip(f"{spec.name}: see instructions above")
                        continue
                    path = tools.configure_tool(spec, instance_url, token, bridge=True)
                    ui.ok(f"{spec.name} bridge → {path}")
                else:
                    path = tools.configure_tool(spec, instance_url, token)
                    ui.ok(f"{spec.name} → {path}")
            else:
                removed = tools.unconfigure_tool(spec)
                if removed:
                    ui.ok(f"{spec.name}: entry removed")
                else:
                    ui.skip(f"{spec.name}: no entry found")
        except Exception as exc:  # noqa: BLE001
            ui.fail(f"{spec.name}: {exc}")


# ── Helpers ──────────────────────────────────────────────────────────────


def _auth_headers(creds: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if creds.get("method") == "google":
        headers["Authorization"] = f"Bearer {creds['token']}"
    else:
        headers["X-API-Key"] = creds["token"]
    return headers


if __name__ == "__main__":
    sys.exit(main())
