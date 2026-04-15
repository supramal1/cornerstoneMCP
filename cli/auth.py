"""Authentication flows for the Cornerstone CLI.

Two paths:

1. **OAuth 2.1** (default, interactive)
   The MCP server is a standards-compliant OAuth 2.1 Authorization Server
   exposed via FastMCP's ``auth_server_provider``. The CLI acts as a real
   OAuth client: discovers the endpoints via
   ``/.well-known/oauth-authorization-server``, dynamically registers
   itself, generates PKCE, opens the browser to the server's login page,
   captures ``?code=&state=`` on a loopback callback, and exchanges the
   code at ``/token`` for an access JWT.

   This path works against the current API-key login form *and* the
   forthcoming Google sign-in login page — the OAuth surface stays the
   same; only the inside of the login page changes.

2. **API key paste** (``--key`` flag, headless/CI)
   Skip OAuth entirely. Take a ``csk_...`` key, validate it by making a
   real MCP ``tools/list`` JSON-RPC call against ``/mcp`` with the key in
   the Authorization header. 200 = good, 401 = bad.

Verification (used by both flows and by ``health``)
---------------------------------------------------
``verify_token`` always validates by calling the MCP server's actual
``/mcp`` endpoint with a ``tools/list`` JSON-RPC request. We never call
the backend ``/connection/verify`` directly — the CLI only ever talks to
the MCP server URL.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import urllib.parse
import webbrowser
from typing import Any

import httpx
import questionary

from . import ui

CALLBACK_PATH = "/callback"
OAUTH_TIMEOUT_SECONDS = 300
MCP_PATH = "/mcp"
DISCOVERY_PATH = "/.well-known/oauth-authorization-server"


class AuthError(RuntimeError):
    """Raised when authentication cannot complete."""


# ── Verification (shared) ────────────────────────────────────────────────


def verify_token(instance_url: str, token: str, method: str = "oauth") -> bool:
    """Validate a token by issuing tools/list against the MCP HTTP endpoint.

    Returns True on 200, False on 401/403. Raises AuthError on transport
    failures so the caller can distinguish unreachable from rejected.
    """
    try:
        _mcp_tools_list(instance_url, token)
    except _Unauthorized:
        return False
    except AuthError:
        raise
    return True


class _Unauthorized(RuntimeError):
    """Internal: raised when the MCP server returns 401/403."""


def _mcp_tools_list(instance_url: str, token: str) -> dict[str, Any]:
    """POST a tools/list JSON-RPC request to the MCP server.

    Streamable-HTTP transport requires Accept to advertise both JSON and
    SSE. We don't actually consume the SSE stream — we just parse whatever
    body comes back as JSON, which works for non-streaming responses.
    """
    url = f"{instance_url.rstrip('/')}{MCP_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=15.0)
    except httpx.HTTPError as exc:
        raise AuthError(f"could not reach {url}: {exc}") from exc

    if resp.status_code in (401, 403):
        raise _Unauthorized()
    if resp.status_code >= 400:
        raise AuthError(f"MCP /mcp returned {resp.status_code}: {resp.text[:200]}")

    return _parse_jsonrpc_or_sse(resp.text)


def _parse_jsonrpc_or_sse(text: str) -> dict[str, Any]:
    """Best-effort parser for either a JSON body or a tiny SSE event stream."""
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    # SSE: scan for the first `data: {...}` line
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload.startswith("{"):
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    continue
    return {}


# ── Reachability (used by connect.py before auth) ────────────────────────


def ping_health(instance_url: str) -> tuple[bool, int | None, str | None]:
    """Hit /health and return (ok, status_code, error_str)."""
    url = f"{instance_url.rstrip('/')}/health"
    try:
        resp = httpx.get(url, timeout=8.0)
    except httpx.HTTPError as exc:
        return False, None, str(exc)
    return (200 <= resp.status_code < 300), resp.status_code, None


# ── API key flow ─────────────────────────────────────────────────────────


def api_key_flow(instance_url: str, *, prefill: str | None = None) -> dict[str, Any]:
    """Prompt for (or accept) a csk_ key and validate with a real MCP call."""
    key = prefill
    if not key:
        ui.info("Paste a Cornerstone API key (starts with csk_).")
        ui.hint("Get one from your Cornerstone dashboard or an admin.")
        key = questionary.password("API key:").unsafe_ask()
    if not key:
        raise AuthError("no API key provided")

    with ui.spinner("Validating key against MCP server..."):
        try:
            _mcp_tools_list(instance_url, key)
        except _Unauthorized as exc:
            raise AuthError("API key rejected by MCP server (401)") from exc

    ui.ok("API key accepted")
    # csk_ keys don't carry an email; use a placeholder the user can override.
    return {
        "token": key,
        "email": "api-key-user",
        "method": "api_key",
        "expires_at": None,
    }


# ── OAuth 2.1 flow ───────────────────────────────────────────────────────


def oauth_login_flow(instance_url: str) -> dict[str, Any]:
    """Run a full OAuth 2.1 + PKCE authorization-code flow against the MCP server."""
    base = instance_url.rstrip("/")

    # 1. Discovery
    with ui.spinner("Discovering OAuth endpoints..."):
        meta = _discover(base)

    auth_endpoint = meta.get("authorization_endpoint")
    token_endpoint = meta.get("token_endpoint")
    reg_endpoint = meta.get("registration_endpoint")
    if not auth_endpoint or not token_endpoint:
        raise AuthError("server is missing authorization_endpoint/token_endpoint")

    # 2. Spin up loopback server BEFORE registering — we need the redirect URI.
    port = _pick_free_port()
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    state_obj = _CallbackState()
    server = http.server.HTTPServer(("127.0.0.1", port), _build_handler(state_obj))
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    try:
        # 3. Dynamic client registration
        if reg_endpoint:
            with ui.spinner("Registering CLI as an OAuth client..."):
                client_id = _register_client(reg_endpoint, redirect_uri)
        else:
            client_id = "cornerstone-cli"

        # 4. PKCE
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state_str = secrets.token_urlsafe(24)

        # 5. Build authorize URL and open the browser
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state_str,
            "scope": "memory",
        }
        url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"
        ui.info("Opening your browser to sign in...")
        ui.hint(f"If it doesn't open, visit:\n    {url}")
        try:
            webbrowser.open(url, new=1, autoraise=True)
        except webbrowser.Error:
            pass

        with ui.spinner(f"Waiting for sign-in (up to {OAUTH_TIMEOUT_SECONDS}s)..."):
            got = state_obj.done.wait(timeout=OAUTH_TIMEOUT_SECONDS)
    finally:
        try:
            server.server_close()
        except OSError:
            pass

    if not got:
        raise AuthError("timed out waiting for OAuth callback")
    if state_obj.error:
        raise AuthError(f"sign-in failed: {state_obj.error}")
    if not state_obj.code:
        raise AuthError("callback returned no authorization code")
    if state_obj.state != state_str:
        raise AuthError("OAuth state mismatch — possible CSRF, aborting")

    # 6. Exchange the code for an access token
    with ui.spinner("Exchanging authorization code for token..."):
        token_payload = _exchange_code(
            token_endpoint=token_endpoint,
            code=state_obj.code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            code_verifier=verifier,
        )

    access_token = token_payload.get("access_token")
    if not access_token:
        raise AuthError(f"token endpoint returned no access_token: {token_payload}")

    expires_in = token_payload.get("expires_in")
    expires_at: int | None = None
    if isinstance(expires_in, int) and expires_in > 0:
        import time as _t

        expires_at = int(_t.time()) + expires_in

    # 7. Sanity-check the token by calling tools/list
    with ui.spinner("Verifying new token..."):
        try:
            _mcp_tools_list(base, access_token)
        except _Unauthorized as exc:
            raise AuthError("server returned a token but it failed verification") from exc

    email = _extract_principal_name(access_token) or "cornerstone-user"
    ui.ok(f"Signed in as {email}")
    return {
        "token": access_token,
        "email": email,
        "method": "oauth",
        "expires_at": expires_at,
    }


# ── OAuth helpers ────────────────────────────────────────────────────────


def _discover(base_url: str) -> dict[str, Any]:
    url = f"{base_url}{DISCOVERY_PATH}"
    try:
        resp = httpx.get(url, timeout=10.0)
    except httpx.HTTPError as exc:
        raise AuthError(f"discovery failed: could not reach {url}: {exc}") from exc
    if resp.status_code != 200:
        raise AuthError(
            f"discovery returned {resp.status_code} from {url} — server may not "
            f"have OAuth enabled"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise AuthError(f"discovery returned non-JSON: {exc}") from exc


def _register_client(reg_endpoint: str, redirect_uri: str) -> str:
    body = {
        "client_name": "Cornerstone CLI",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",  # public client (PKCE)
        "scope": "memory",
    }
    try:
        resp = httpx.post(reg_endpoint, json=body, timeout=10.0)
    except httpx.HTTPError as exc:
        raise AuthError(f"client registration failed: {exc}") from exc
    if resp.status_code not in (200, 201):
        raise AuthError(
            f"client registration returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise AuthError(f"client registration returned non-JSON: {exc}") from exc
    client_id = data.get("client_id")
    if not client_id:
        raise AuthError("client registration response missing client_id")
    return client_id


def _exchange_code(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
) -> dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    try:
        resp = httpx.post(
            token_endpoint,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"token exchange failed: {exc}") from exc
    if resp.status_code != 200:
        raise AuthError(f"token exchange returned {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise AuthError(f"token exchange returned non-JSON: {exc}") from exc


def _extract_principal_name(jwt_token: str) -> str | None:
    """Best-effort extraction of the human name from a JWT payload (no verify)."""
    try:
        parts = jwt_token.split(".")
        if len(parts) != 3:
            return None
        body = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body))
        return payload.get("name") or payload.get("email") or payload.get("sub")
    except (ValueError, json.JSONDecodeError, IndexError):
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# ── Loopback callback server ─────────────────────────────────────────────


class _CallbackState:
    code: str | None = None
    state: str | None = None
    error: str | None = None

    def __init__(self) -> None:
        self.done = threading.Event()


def _build_handler(state: _CallbackState) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any, **_kwargs: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            if "error" in params:
                state.error = params["error"][0]
                self._respond("Authentication failed", state.error, ok=False)
                state.done.set()
                return
            code = params.get("code", [None])[0]
            st = params.get("state", [None])[0]
            if not code:
                state.error = "missing authorization code"
                self._respond("Authentication failed", state.error, ok=False)
                state.done.set()
                return
            state.code = code
            state.state = st
            self._respond(
                "Cornerstone authenticated",
                "You can close this tab and return to the terminal.",
                ok=True,
            )
            state.done.set()

        def _respond(self, title: str, body: str, *, ok: bool) -> None:
            colour = "#10b981" if ok else "#ef4444"
            html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>{title}</title><style>
body {{ font: 16px -apple-system, system-ui, sans-serif; background: #0b0f14;
        color: #e5e7eb; height: 100vh; margin: 0;
        display: flex; align-items: center; justify-content: center; }}
.card {{ padding: 40px 56px; border: 1px solid #1f2937; border-radius: 12px;
         background: #0f1520; text-align: center; max-width: 420px; }}
h1 {{ margin: 0 0 12px; color: {colour}; font-size: 20px; }}
p  {{ margin: 0; color: #9ca3af; line-height: 1.5; }}
</style></head><body><div class='card'><h1>{title}</h1><p>{body}</p></div></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

    return Handler


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
