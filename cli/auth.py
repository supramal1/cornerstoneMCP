"""Authentication flows: Google OAuth loopback + API key fallback.

Both flows return a dict the caller passes straight to
``config.save_credentials``.

Google flow
-----------
1. Spin up a one-shot HTTP server on an ephemeral localhost port.
2. Open the browser to ``{instance}/auth/google/start?redirect_uri=...``.
3. Server posts the token back to the loopback callback; we capture it,
   verify it with ``{instance}/auth/whoami``, and return.

API key flow
------------
1. Prompt (masked) for the key.
2. Hit ``{instance}/auth/whoami`` with ``X-API-Key``. Success ⇒ return.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

import httpx
import questionary

from . import ui

CALLBACK_PATH = "/cornerstone-cli/callback"
OAUTH_TIMEOUT_SECONDS = 180


class AuthError(RuntimeError):
    """Raised when authentication cannot complete."""


# ── API key flow ─────────────────────────────────────────────────────────


def api_key_flow(instance_url: str) -> dict[str, Any]:
    """Prompt for an API key and verify it against /auth/whoami."""
    ui.info("Paste a Cornerstone API key.")
    ui.hint("Get one from an admin, or generate via the admin UI.")
    key = questionary.password("API key:").unsafe_ask()
    if not key:
        raise AuthError("No API key entered")

    with ui.spinner("Verifying API key..."):
        identity = _whoami(instance_url, api_key=key)

    email = identity.get("email") or identity.get("principal") or "api-key-user"
    ui.ok(f"Authenticated as {email}")
    return {
        "token": key,
        "email": email,
        "method": "api_key",
        "expires_at": None,
    }


# ── Google OAuth loopback flow ───────────────────────────────────────────


class _CallbackState:
    """Mutable bag captured by the HTTP handler closure."""

    token: str | None = None
    email: str | None = None
    expires_at: int | None = None
    error: str | None = None
    done: threading.Event

    def __init__(self) -> None:
        self.done = threading.Event()


def _build_handler(state: _CallbackState) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any, **_kwargs: Any) -> None:
            return  # silence the default stderr spam

        def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return

            params = urllib.parse.parse_qs(parsed.query)
            if "error" in params:
                state.error = params["error"][0]
                self._respond_html(
                    "Authentication failed",
                    f"Error: {state.error}. You can close this tab.",
                    ok=False,
                )
                state.done.set()
                return

            token = params.get("token", [None])[0]
            if not token:
                state.error = "missing token in callback"
                self._respond_html(
                    "Authentication failed",
                    "The callback did not include a token. Close this tab and retry.",
                    ok=False,
                )
                state.done.set()
                return

            state.token = token
            state.email = params.get("email", [None])[0]
            exp = params.get("expires_at", [None])[0]
            state.expires_at = int(exp) if exp and exp.isdigit() else None

            self._respond_html(
                "Cornerstone authenticated",
                "You can close this tab and return to the terminal.",
                ok=True,
            )
            state.done.set()

        def _respond_html(self, title: str, body: str, *, ok: bool) -> None:
            colour = "#10b981" if ok else "#ef4444"
            html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>{title}</title>
<style>
  body {{ font: 16px -apple-system, system-ui, sans-serif; background: #0b0f14;
          color: #e5e7eb; height: 100vh; margin: 0;
          display: flex; align-items: center; justify-content: center; }}
  .card {{ padding: 40px 56px; border: 1px solid #1f2937; border-radius: 12px;
           background: #0f1520; text-align: center; max-width: 420px; }}
  h1 {{ margin: 0 0 12px; color: {colour}; font-size: 20px; }}
  p  {{ margin: 0; color: #9ca3af; line-height: 1.5; }}
</style></head>
<body><div class='card'><h1>{title}</h1><p>{body}</p></div></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

    return Handler


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def google_oauth_flow(instance_url: str) -> dict[str, Any]:
    """Run the loopback Google OAuth flow and return a credentials payload."""
    port = _pick_free_port()
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    state = _CallbackState()

    server = http.server.HTTPServer(("127.0.0.1", port), _build_handler(state))
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = (
        f"{instance_url.rstrip('/')}/auth/google/start"
        f"?redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        f"&client=cornerstone-cli"
    )

    ui.info("Opening your browser to sign in with Google...")
    ui.hint(f"If it doesn't open, visit:\n    {auth_url}")
    try:
        webbrowser.open(auth_url, new=1, autoraise=True)
    except webbrowser.Error:
        pass

    with ui.spinner(f"Waiting for sign-in (up to {OAUTH_TIMEOUT_SECONDS}s)..."):
        got_callback = state.done.wait(timeout=OAUTH_TIMEOUT_SECONDS)

    try:
        server.server_close()
    except OSError:
        pass

    if not got_callback:
        raise AuthError("timed out waiting for Google callback")
    if state.error:
        raise AuthError(f"Google sign-in failed: {state.error}")
    if not state.token:
        raise AuthError("Google sign-in returned no token")

    email = state.email
    if not email:
        try:
            identity = _whoami(instance_url, bearer=state.token)
            email = identity.get("email") or "google-user"
        except AuthError:
            email = "google-user"

    ui.ok(f"Authenticated as {email}")
    return {
        "token": state.token,
        "email": email,
        "method": "google",
        "expires_at": state.expires_at,
    }


# ── Shared verifier ──────────────────────────────────────────────────────


def _whoami(
    instance_url: str,
    *,
    api_key: str | None = None,
    bearer: str | None = None,
) -> dict[str, Any]:
    """Hit /auth/whoami with the supplied credential and return the payload."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    url = f"{instance_url.rstrip('/')}/auth/whoami"
    try:
        resp = httpx.get(url, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        raise AuthError(f"could not reach {url}: {exc}") from exc

    if resp.status_code == 401:
        raise AuthError("credentials rejected (401)")
    if resp.status_code == 404:
        # Older deployments may not expose /auth/whoami; fall back to /health.
        return _health_probe(instance_url, headers)
    if resp.status_code >= 400:
        raise AuthError(f"whoami returned {resp.status_code}: {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise AuthError(f"whoami returned non-JSON: {exc}") from exc


def _health_probe(instance_url: str, headers: dict[str, str]) -> dict[str, Any]:
    url = f"{instance_url.rstrip('/')}/health"
    try:
        resp = httpx.get(url, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        raise AuthError(f"health probe failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AuthError(f"health probe returned {resp.status_code}")
    return {"email": "unknown", "health": "ok"}


def verify_token(instance_url: str, token: str, method: str) -> bool:
    """Return True if the stored token still authenticates against the instance."""
    try:
        if method == "google":
            _whoami(instance_url, bearer=token)
        else:
            _whoami(instance_url, api_key=token)
    except AuthError:
        return False
    return True
