"""Detect supported CLI/IDE tools and write their MCP connector config.

Detection looks at two signals — binary on PATH and config file existence —
so we can distinguish "installed but unconfigured" from "not present" from
"configured but stale."

Config writers are deliberately non-destructive: they merge the Cornerstone
MCP entry into an existing config file rather than overwriting it, and they
back the file up to ``<path>.bak`` before the first write.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import TOOLS, ToolSpec


@dataclass
class ToolStatus:
    key: str
    name: str
    installed: bool
    config_exists: bool
    configured: bool
    version: str | None
    config_path: Path

    @property
    def detected(self) -> bool:
        return self.installed or self.config_exists


# ── Detection ────────────────────────────────────────────────────────────


def _binary_version(binary: str | None) -> str | None:
    if not binary:
        return None
    if not shutil.which(binary):
        return None
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (result.stdout or result.stderr or "").strip()
    return out.split("\n")[0] if out else "installed"


def _is_cornerstone_configured(spec: ToolSpec) -> bool:
    if not spec.config_path.exists():
        return False
    try:
        if spec.config_format == "json":
            data = json.loads(spec.config_path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers") or data.get("mcp_servers") or {}
            return spec.server_key in servers
        if spec.config_format == "toml":
            text = spec.config_path.read_text(encoding="utf-8")
            return f"[mcp_servers.{spec.server_key}]" in text
    except (OSError, json.JSONDecodeError):
        return False
    return False


def detect_tool(spec: ToolSpec) -> ToolStatus:
    version = _binary_version(spec.binary) if spec.binary else None
    # Claude Desktop has no binary but is "installed" if the app dir exists.
    if spec.binary is None:
        installed = spec.config_path.parent.exists()
    else:
        installed = version is not None
    return ToolStatus(
        key=spec.key,
        name=spec.name,
        installed=installed,
        config_exists=spec.config_path.exists(),
        configured=_is_cornerstone_configured(spec),
        version=version,
        config_path=spec.config_path,
    )


def detect_all() -> list[ToolStatus]:
    return [detect_tool(spec) for spec in TOOLS.values()]


# ── Config writers ───────────────────────────────────────────────────────


def _backup_once(path: Path) -> None:
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_once(path)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _configure_http_json(
    path: Path,
    mcp_url: str,
    token: str,
    server_key: str,
) -> None:
    data = _load_json(path)
    servers = data.setdefault("mcpServers", {})
    servers[server_key] = {
        "type": "http",
        "url": mcp_url,
        "headers": {"Authorization": f"Bearer {token}"},
    }
    _write_json(path, data)


def _configure_claude_desktop_bridge(
    path: Path, mcp_url: str, token: str, server_key: str
) -> None:
    """Opt-in stdio bridge for users who can't use the Connectors UI.

    Writes an ``npx mcp-remote`` launcher into ``claude_desktop_config.json``.
    Requires Node on the user's PATH. Only used when the user explicitly
    asks for the bridge — the default Claude Desktop path is the in-app
    Connectors UI (see ``CLAUDE_DESKTOP_INSTRUCTIONS``).
    """
    data = _load_json(path)
    servers = data.setdefault("mcpServers", {})
    servers[server_key] = {
        "command": "npx",
        "args": [
            "-y",
            "mcp-remote",
            mcp_url,
            "--header",
            f"Authorization: Bearer {token}",
        ],
    }
    _write_json(path, data)


CLAUDE_DESKTOP_INSTRUCTIONS = (
    "Claude Desktop natively supports remote MCP via its Connectors UI —\n"
    "no config file editing required.\n"
    "\n"
    "  1. Open Claude Desktop\n"
    "  2. Settings  →  Connectors  →  Add custom connector\n"
    "  3. Paste this URL:\n"
    "       {mcp_url}\n"
    "  4. Sign in when prompted (Claude Desktop runs the OAuth flow for you)\n"
)


def _configure_codex_toml(path: Path, mcp_url: str, token: str, server_key: str) -> None:
    """Codex uses TOML. We do a naive section-replace rather than pulling in a
    full TOML writer — Codex's config stays tiny and this keeps deps lean."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_once(path)

    section_header = f"[mcp_servers.{server_key}]"
    new_section = "\n".join(
        [
            section_header,
            'type = "http"',
            f'url  = "{mcp_url}"',
            f'headers = {{ Authorization = "Bearer {token}" }}',
            "",
        ]
    )

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if section_header in existing:
        lines = existing.splitlines()
        out: list[str] = []
        skipping = False
        for line in lines:
            if line.strip() == section_header:
                skipping = True
                out.append(new_section.rstrip())
                continue
            if skipping and line.startswith("[") and line.strip() != section_header:
                skipping = False
            if not skipping:
                out.append(line)
        body = "\n".join(out).rstrip() + "\n"
    else:
        body = (existing.rstrip() + "\n\n" + new_section).lstrip() if existing else new_section

    path.write_text(body, encoding="utf-8")


def configure_tool(
    spec: ToolSpec,
    mcp_url: str,
    token: str,
    *,
    bridge: bool = False,
) -> Path | None:
    """Write the Cornerstone MCP entry into the tool's config.

    Returns the config path when a file was written, or ``None`` when the
    tool is handled out-of-band (currently only Claude Desktop's default
    Connectors-UI path, which the caller is expected to announce via
    ``CLAUDE_DESKTOP_INSTRUCTIONS``).

    ``bridge=True`` forces Claude Desktop to use the opt-in ``npx
    mcp-remote`` stdio bridge instead of the in-app connector flow.
    """
    if spec.key == "claude_desktop":
        if not bridge:
            return None
        _configure_claude_desktop_bridge(spec.config_path, mcp_url, token, spec.server_key)
    elif spec.config_format == "json":
        _configure_http_json(spec.config_path, mcp_url, token, spec.server_key)
    elif spec.config_format == "toml":
        _configure_codex_toml(spec.config_path, mcp_url, token, spec.server_key)
    else:
        raise ValueError(f"unknown config format: {spec.config_format}")
    return spec.config_path


def unconfigure_tool(spec: ToolSpec) -> bool:
    """Remove the cornerstone entry from a tool's config. Returns True if removed."""
    if not spec.config_path.exists():
        return False
    _backup_once(spec.config_path)

    if spec.config_format == "json":
        data = _load_json(spec.config_path)
        servers = data.get("mcpServers") or data.get("mcp_servers")
        if not servers or spec.server_key not in servers:
            return False
        del servers[spec.server_key]
        _write_json(spec.config_path, data)
        return True

    if spec.config_format == "toml":
        section_header = f"[mcp_servers.{spec.server_key}]"
        text = spec.config_path.read_text(encoding="utf-8")
        if section_header not in text:
            return False
        lines = text.splitlines()
        out: list[str] = []
        skipping = False
        for line in lines:
            if line.strip() == section_header:
                skipping = True
                continue
            if skipping and line.startswith("["):
                skipping = False
            if not skipping:
                out.append(line)
        spec.config_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        return True

    return False
