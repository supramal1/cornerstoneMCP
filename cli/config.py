"""Paths, tool config locations, and credential storage for the Cornerstone CLI.

Credentials live under ~/.cornerstone/ with 0600 perms. Tool configs are written
to each IDE's conventional location (Claude Code, Claude Desktop, Cursor, Codex,
Windsurf).
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DOT_DIR = Path.home() / ".cornerstone"
CREDENTIALS_FILE = DOT_DIR / "credentials.json"
INSTANCE_FILE = DOT_DIR / "instance.json"

DEFAULT_INSTANCE_URL = "https://cornerstone-mcp-34862349933.europe-west2.run.app"
DEFAULT_API_URL = "https://cornerstone-api-34862349933.europe-west2.run.app"


def _claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


@dataclass(frozen=True)
class ToolSpec:
    """Metadata for a CLI/IDE that can host the Cornerstone MCP connector."""

    key: str
    name: str
    binary: str | None
    config_path: Path
    config_format: str  # "json" | "toml"
    supports_http: bool  # True if it can speak remote MCP directly
    server_key: str = "cornerstone"


TOOLS: dict[str, ToolSpec] = {
    "claude_code": ToolSpec(
        key="claude_code",
        name="Claude Code",
        binary="claude",
        config_path=Path.home() / ".claude" / "settings.json",
        config_format="json",
        supports_http=True,
    ),
    "claude_desktop": ToolSpec(
        key="claude_desktop",
        name="Claude Desktop",
        binary=None,
        config_path=_claude_desktop_config_path(),
        config_format="json",
        supports_http=False,  # uses stdio bridge via mcp-remote
    ),
    "cursor": ToolSpec(
        key="cursor",
        name="Cursor",
        binary="cursor",
        config_path=Path.home() / ".cursor" / "mcp.json",
        config_format="json",
        supports_http=True,
    ),
    "codex": ToolSpec(
        key="codex",
        name="Codex CLI",
        binary="codex",
        config_path=Path.home() / ".codex" / "config.toml",
        config_format="toml",
        supports_http=True,
    ),
    "windsurf": ToolSpec(
        key="windsurf",
        name="Windsurf",
        binary="windsurf",
        config_path=Path.home() / ".windsurf" / "mcp.json",
        config_format="json",
        supports_http=True,
    ),
}


def ensure_dot_dir() -> Path:
    DOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        DOT_DIR.chmod(0o700)
    except OSError:
        pass
    return DOT_DIR


def save_credentials(
    token: str,
    email: str,
    instance_url: str,
    expires_at: int | None = None,
    method: str = "api_key",
) -> Path:
    """Write credentials to disk with 0600 perms. Returns the path."""
    ensure_dot_dir()
    payload: dict[str, Any] = {
        "token": token,
        "email": email,
        "instance_url": instance_url.rstrip("/"),
        "method": method,
        "saved_at": int(time.time()),
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at
    CREDENTIALS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        CREDENTIALS_FILE.chmod(0o600)
    except OSError:
        pass
    return CREDENTIALS_FILE


def load_credentials() -> dict[str, Any] | None:
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_credentials() -> bool:
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
        return True
    return False


def credentials_are_fresh(creds: dict[str, Any]) -> bool:
    """Return True if credentials either don't expire or haven't expired yet."""
    exp = creds.get("expires_at")
    if exp is None:
        return True
    return int(time.time()) < int(exp) - 60


def save_instance_url(url: str) -> None:
    ensure_dot_dir()
    INSTANCE_FILE.write_text(json.dumps({"url": url.rstrip("/")}), encoding="utf-8")


def load_instance_url() -> str | None:
    if not INSTANCE_FILE.exists():
        return None
    try:
        return json.loads(INSTANCE_FILE.read_text(encoding="utf-8")).get("url")
    except (OSError, json.JSONDecodeError):
        return None


@dataclass
class WizardState:
    """Transient state passed between phases of the install/connect wizards."""

    instance_url: str = DEFAULT_INSTANCE_URL
    api_url: str = DEFAULT_API_URL
    token: str | None = None
    email: str | None = None
    auth_method: str = "api_key"
    configured_tools: list[str] = field(default_factory=list)
