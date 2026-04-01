"""
Cornerstone OAuth 2.1 Authorization Server + Token Verifier.

Implements the MCP OAuth spec so non-technical users can connect via
Claude Desktop (or any MCP client) without editing config files.

Architecture:
  - Stateless tokens (JWTs) — works across Cloud Run instances
  - In-memory client registrations (re-created on reconnect)
  - Login page where users enter their Cornerstone API key
  - Backward-compatible: csk_ Bearer tokens still work (Claude Code)
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger("cornerstone.oauth")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Secret for signing JWTs — MUST be set in production
JWT_SECRET = os.environ.get("OAUTH_JWT_SECRET", "")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    logger.warning(
        "OAUTH_JWT_SECRET not set — generated ephemeral secret (tokens won't survive restart)"
    )

# Public URL of this MCP server (for issuer_url / resource_server_url)
MCP_PUBLIC_URL = os.environ.get(
    "MCP_PUBLIC_URL",
    "https://cornerstone-mcp-34862349933.europe-west2.run.app",
)

# Cornerstone backend URL (for validating API keys)
CORNERSTONE_URL = os.environ.get("CORNERSTONE_URL", "http://127.0.0.1:8000")

# Token lifetimes
ACCESS_TOKEN_TTL = 24 * 3600  # 24 hours
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
AUTH_CODE_TTL = 120  # 2 minutes


# ---------------------------------------------------------------------------
# Lightweight JWT (no PyJWT dependency — just HMAC-SHA256)
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def jwt_encode(payload: dict, secret: str = JWT_SECRET) -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url_encode(json.dumps(payload).encode())
    sig_input = f"{header}.{body}"
    signature = _b64url_encode(
        hashlib.new("sha256", sig_input.encode(), usedforsecurity=True).digest()
        if False
        else _hmac_sha256(secret.encode(), sig_input.encode())
    )
    return f"{header}.{body}.{signature}"


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    import hmac

    return hmac.new(key, msg, hashlib.sha256).digest()


def jwt_decode(token: str, secret: str = JWT_SECRET) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, body, sig = parts
        expected_sig = _b64url_encode(
            _hmac_sha256(secret.encode(), f"{header}.{body}".encode())
        )
        if not secrets.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(_b64url_decode(body))
        if payload.get("exp") and payload["exp"] < time.time():
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Simple XOR-based obfuscation for API keys in JWTs
# (The JWT is already HMAC-signed, so this just prevents casual reading)
# ---------------------------------------------------------------------------


def _obfuscate_key(api_key: str) -> str:
    key_bytes = api_key.encode()
    secret_bytes = JWT_SECRET.encode()
    obfuscated = bytes(
        b ^ secret_bytes[i % len(secret_bytes)] for i, b in enumerate(key_bytes)
    )
    return _b64url_encode(obfuscated)


def _deobfuscate_key(obfuscated: str) -> str:
    obfuscated_bytes = _b64url_decode(obfuscated)
    secret_bytes = JWT_SECRET.encode()
    original = bytes(
        b ^ secret_bytes[i % len(secret_bytes)] for i, b in enumerate(obfuscated_bytes)
    )
    return original.decode()


# ---------------------------------------------------------------------------
# Data types for the OAuth provider
# ---------------------------------------------------------------------------


@dataclass
class CornerstoneAuthCode:
    code_challenge: str
    redirect_uri: AnyUrl
    redirect_uri_provided_explicitly: bool
    client_id: str
    expires_at: float
    scopes: list[str]
    principal_id: str
    principal_name: str
    api_key_obf: str


@dataclass
class CornerstoneStoredToken:
    token: str
    client_id: str
    scopes: list[str]
    expires_at: int
    principal_id: str
    principal_name: str
    api_key_obf: str


# ---------------------------------------------------------------------------
# Validate API key against Cornerstone backend
# ---------------------------------------------------------------------------


async def validate_api_key(api_key: str) -> dict | None:
    """Validate an API key against the Cornerstone backend.
    Returns principal info dict or None if invalid."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # First try /connection/verify with namespace "default"
            # (multi-grant principals need explicit namespace)
            r = await client.post(
                f"{CORNERSTONE_URL}/connection/verify",
                headers={"X-API-Key": api_key},
                json={"namespace": "default"},
            )
            # If 403 (namespace not granted), try without namespace
            # for single-grant principals
            if r.status_code == 403:
                r = await client.post(
                    f"{CORNERSTONE_URL}/connection/verify",
                    headers={"X-API-Key": api_key},
                )
            if r.status_code == 200:
                data = r.json()
                return {
                    "principal_id": data.get("principal_id", ""),
                    "principal_name": data.get("principal", "unknown"),
                    "workspaces": data.get("allowed_workspaces", []),
                }
            return None
    except Exception as e:
        logger.error("API key validation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# OAuth Authorization Server Provider
# ---------------------------------------------------------------------------


class CornerstoneOAuthProvider(
    OAuthAuthorizationServerProvider[
        CornerstoneAuthCode, CornerstoneStoredToken, AccessToken
    ]
):
    def __init__(self) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        # Pending auth sessions: session_id -> {client, params, ...}
        self._auth_sessions: dict[str, dict[str, Any]] = {}

    # --- Client registration ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        logger.info(
            "Registered OAuth client: %s (%s)",
            client_info.client_name,
            client_info.client_id,
        )

    # --- Authorization ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        session_id = secrets.token_urlsafe(32)
        # Encode auth session as JWT so it works across instances
        session_jwt = jwt_encode(
            {
                "type": "auth_session",
                "sid": session_id,
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_explicit": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or ["memory"],
                "exp": time.time() + 600,  # 10 min to complete login
            }
        )
        login_url = f"{MCP_PUBLIC_URL}/oauth/login?session={session_jwt}"
        return login_url

    # --- Authorization code ---

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> CornerstoneAuthCode | None:
        payload = jwt_decode(authorization_code)
        if not payload or payload.get("type") != "auth_code":
            return None
        if payload.get("client_id") != client.client_id:
            return None
        return CornerstoneAuthCode(
            code_challenge=payload["code_challenge"],
            redirect_uri=AnyUrl(payload["redirect_uri"]),
            redirect_uri_provided_explicitly=payload.get("redirect_uri_explicit", True),
            client_id=payload["client_id"],
            expires_at=payload["exp"],
            scopes=payload.get("scopes", ["memory"]),
            principal_id=payload["principal_id"],
            principal_name=payload["principal_name"],
            api_key_obf=payload["api_key_obf"],
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: CornerstoneAuthCode,
    ) -> OAuthToken:
        now = int(time.time())
        access_payload = {
            "type": "access",
            "sub": authorization_code.principal_id,
            "name": authorization_code.principal_name,
            "client_id": client.client_id,
            "scopes": authorization_code.scopes,
            "akob": authorization_code.api_key_obf,
            "exp": now + ACCESS_TOKEN_TTL,
            "iat": now,
            "jti": secrets.token_urlsafe(16),
        }
        refresh_payload = {
            "type": "refresh",
            "sub": authorization_code.principal_id,
            "name": authorization_code.principal_name,
            "client_id": client.client_id,
            "scopes": authorization_code.scopes,
            "akob": authorization_code.api_key_obf,
            "exp": now + REFRESH_TOKEN_TTL,
            "iat": now,
            "jti": secrets.token_urlsafe(16),
        }
        return OAuthToken(
            access_token=jwt_encode(access_payload),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(authorization_code.scopes),
            refresh_token=jwt_encode(refresh_payload),
        )

    # --- Refresh token ---

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> CornerstoneStoredToken | None:
        payload = jwt_decode(refresh_token)
        if not payload or payload.get("type") != "refresh":
            return None
        if payload.get("client_id") != client.client_id:
            return None
        return CornerstoneStoredToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload.get("scopes", ["memory"]),
            expires_at=payload["exp"],
            principal_id=payload["sub"],
            principal_name=payload.get("name", "unknown"),
            api_key_obf=payload["akob"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: CornerstoneStoredToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = int(time.time())
        use_scopes = scopes if scopes else refresh_token.scopes
        access_payload = {
            "type": "access",
            "sub": refresh_token.principal_id,
            "name": refresh_token.principal_name,
            "client_id": client.client_id,
            "scopes": use_scopes,
            "akob": refresh_token.api_key_obf,
            "exp": now + ACCESS_TOKEN_TTL,
            "iat": now,
            "jti": secrets.token_urlsafe(16),
        }
        new_refresh_payload = {
            "type": "refresh",
            "sub": refresh_token.principal_id,
            "name": refresh_token.principal_name,
            "client_id": client.client_id,
            "scopes": use_scopes,
            "akob": refresh_token.api_key_obf,
            "exp": now + REFRESH_TOKEN_TTL,
            "iat": now,
            "jti": secrets.token_urlsafe(16),
        }
        return OAuthToken(
            access_token=jwt_encode(access_payload),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(use_scopes),
            refresh_token=jwt_encode(new_refresh_payload),
        )

    # --- Access token ---

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Legacy: Claude Code sends csk_ API keys directly as Bearer tokens
        if token.startswith("csk_"):
            info = await validate_api_key(token)
            if info:
                return AccessToken(
                    token=token,
                    client_id="claude-code-legacy",
                    scopes=["memory"],
                )
            return None

        # OAuth: decode JWT access token
        payload = jwt_decode(token)
        if not payload or payload.get("type") != "access":
            return None
        return AccessToken(
            token=token,
            client_id=payload.get("client_id", "unknown"),
            scopes=payload.get("scopes", ["memory"]),
            expires_at=payload.get("exp"),
        )

    # --- Revocation ---

    async def revoke_token(self, token: AccessToken | CornerstoneStoredToken) -> None:
        # JWTs can't be revoked without a blocklist. For now, no-op.
        # Tokens are short-lived (24h) which limits exposure.
        logger.info("Token revocation requested (no-op for JWTs)")


# ---------------------------------------------------------------------------
# Helper: extract API key from current request's token
# ---------------------------------------------------------------------------


def get_api_key_from_token(token_str: str) -> str | None:
    """Extract the Cornerstone API key from a Bearer token.
    For csk_ tokens, the token IS the key.
    For OAuth JWTs, the key is obfuscated in the payload."""
    if token_str.startswith("csk_"):
        return token_str
    payload = jwt_decode(token_str)
    if payload and payload.get("akob"):
        try:
            return _deobfuscate_key(payload["akob"])
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Login page HTML
# ---------------------------------------------------------------------------

LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cornerstone — Connect</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
            background: #0a0a0a;
            color: #fafafa;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .container {
            width: 100%;
            max-width: 420px;
            padding: 2rem;
        }
        .logo {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            margin-bottom: 0.5rem;
        }
        .subtitle {
            color: #888;
            font-size: 0.875rem;
            margin-bottom: 2rem;
        }
        .card {
            background: #141414;
            border: 1px solid #262626;
            border-radius: 12px;
            padding: 1.5rem;
        }
        label {
            display: block;
            font-size: 0.8125rem;
            font-weight: 500;
            color: #a1a1a1;
            margin-bottom: 0.5rem;
        }
        input[type="password"], input[type="text"] {
            width: 100%;
            padding: 0.625rem 0.75rem;
            background: #0a0a0a;
            border: 1px solid #333;
            border-radius: 8px;
            color: #fafafa;
            font-size: 0.875rem;
            font-family: 'SF Mono', 'Fira Code', monospace;
            outline: none;
            transition: border-color 0.15s;
        }
        input:focus {
            border-color: #666;
        }
        .help {
            font-size: 0.75rem;
            color: #666;
            margin-top: 0.5rem;
        }
        button {
            width: 100%;
            padding: 0.625rem;
            margin-top: 1.25rem;
            background: #fafafa;
            color: #0a0a0a;
            border: none;
            border-radius: 8px;
            font-size: 0.875rem;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.15s;
        }
        button:hover { opacity: 0.9; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .error {
            background: #1a0000;
            border: 1px solid #4a1515;
            color: #f87171;
            padding: 0.75rem;
            border-radius: 8px;
            font-size: 0.8125rem;
            margin-bottom: 1rem;
            display: none;
        }
        .error.visible { display: block; }
        .footer {
            text-align: center;
            margin-top: 1.5rem;
            font-size: 0.75rem;
            color: #555;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">Cornerstone</div>
        <div class="subtitle">Connect your memory to Claude Desktop</div>
        <div class="card">
            <div id="error" class="error"></div>
            <form id="loginForm" method="POST" action="/oauth/login">
                <input type="hidden" name="session" value="{session_jwt}" />
                <label for="api_key">API Key</label>
                <input
                    type="password"
                    id="api_key"
                    name="api_key"
                    placeholder="csk_..."
                    autocomplete="off"
                    required
                />
                <div class="help">
                    Find your API key in the Cornerstone dashboard under Settings.
                </div>
                <button type="submit" id="submitBtn">Connect</button>
            </form>
        </div>
        <div class="footer">
            Your key is validated and never stored in plaintext.
        </div>
    </div>
    <script>
        const form = document.getElementById('loginForm');
        const btn = document.getElementById('submitBtn');
        const errorDiv = document.getElementById('error');
        form.addEventListener('submit', () => {
            btn.disabled = true;
            btn.textContent = 'Connecting...';
        });
    </script>
</body>
</html>"""

LOGIN_ERROR_REDIRECT = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Error</title>
<style>
body { font-family: -apple-system, sans-serif; background: #0a0a0a; color: #fafafa;
display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.msg { text-align: center; max-width: 400px; }
h2 { color: #f87171; margin-bottom: 1rem; }
a { color: #60a5fa; }
</style></head>
<body><div class="msg"><h2>Authentication Failed</h2>
<p>{message}</p>
<p style="margin-top:1rem;"><a href="javascript:history.back()">Try again</a></p>
</div></body></html>"""

LOGIN_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cornerstone — Connected</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
            background: #0a0a0a;
            color: #fafafa;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .container { text-align: center; max-width: 420px; padding: 2rem; }
        .check {
            width: 64px; height: 64px;
            margin: 0 auto 1.5rem;
            border-radius: 50%;
            background: #052e16;
            border: 2px solid #16a34a;
            display: flex;
            align-items: center;
            justify-content: center;
            animation: pop 0.3s ease-out;
        }
        .check svg { width: 32px; height: 32px; color: #4ade80; }
        @keyframes pop {
            0% { transform: scale(0.5); opacity: 0; }
            100% { transform: scale(1); opacity: 1; }
        }
        h2 { font-size: 1.25rem; font-weight: 700; margin-bottom: 0.5rem; }
        .name { color: #4ade80; font-weight: 600; }
        .sub { color: #888; font-size: 0.875rem; margin-top: 0.75rem; }
        .dots { display: inline-block; }
        .dots span { animation: blink 1.4s infinite; opacity: 0; }
        .dots span:nth-child(2) { animation-delay: 0.2s; }
        .dots span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes blink { 0%,80%,100% { opacity: 0; } 40% { opacity: 1; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="check">
            <svg fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>
            </svg>
        </div>
        <h2>Connected</h2>
        <p>Signed in as <span class="name">{principal_name}</span></p>
        <p class="sub">Returning to Claude Desktop<span class="dots"><span>.</span><span>.</span><span>.</span></span></p>
    </div>
    <script>setTimeout(function(){ window.location.href = "{redirect_url}"; }, 2000);</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Login page route handlers (registered via custom_route)
# ---------------------------------------------------------------------------


def register_login_routes(mcp_server: Any) -> None:
    """Register the OAuth login page routes on the FastMCP server."""

    @mcp_server.custom_route("/oauth/login", methods=["GET"])
    async def login_page(request: Request) -> Response:
        session_jwt = request.query_params.get("session", "")
        if not session_jwt:
            return HTMLResponse(
                LOGIN_ERROR_REDIRECT.replace(
                    "{message}",
                    "Missing session. Please start the connection from Claude Desktop.",
                ),
                status_code=400,
            )
        # Validate session JWT is well-formed (don't decode fully — user hasn't authed yet)
        payload = jwt_decode(session_jwt)
        if not payload or payload.get("type") != "auth_session":
            return HTMLResponse(
                LOGIN_ERROR_REDIRECT.replace(
                    "{message}",
                    "Invalid or expired session. Please try connecting again.",
                ),
                status_code=400,
            )
        return HTMLResponse(
            LOGIN_PAGE_HTML.replace("{session_jwt}", html.escape(session_jwt))
        )

    @mcp_server.custom_route("/oauth/login", methods=["POST"])
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        session_jwt = str(form.get("session", ""))
        api_key = str(form.get("api_key", "")).strip()

        # Validate session
        session = jwt_decode(session_jwt)
        if not session or session.get("type") != "auth_session":
            return HTMLResponse(
                LOGIN_ERROR_REDIRECT.replace(
                    "{message}",
                    "Session expired. Please try connecting again from Claude Desktop.",
                ),
                status_code=400,
            )

        # Validate API key
        if not api_key or not api_key.startswith("csk_"):
            return HTMLResponse(
                LOGIN_ERROR_REDIRECT.replace(
                    "{message}", "Invalid API key format. Keys start with csk_."
                ),
                status_code=400,
            )

        principal_info = await validate_api_key(api_key)
        if not principal_info:
            return HTMLResponse(
                LOGIN_ERROR_REDIRECT.replace(
                    "{message}", "Invalid API key. Please check your key and try again."
                ),
                status_code=401,
            )

        # Create authorization code as JWT
        auth_code_jwt = jwt_encode(
            {
                "type": "auth_code",
                "client_id": session["client_id"],
                "redirect_uri": session["redirect_uri"],
                "redirect_uri_explicit": session.get("redirect_uri_explicit", True),
                "code_challenge": session["code_challenge"],
                "scopes": session.get("scopes", ["memory"]),
                "principal_id": principal_info["principal_id"],
                "principal_name": principal_info["principal_name"],
                "api_key_obf": _obfuscate_key(api_key),
                "exp": time.time() + AUTH_CODE_TTL,
            }
        )

        # Build redirect URL with auth code
        redirect_uri = session["redirect_uri"]
        parsed = urlparse(redirect_uri)
        params = {"code": auth_code_jwt}
        if session.get("state"):
            params["state"] = session["state"]
        separator = "&" if parsed.query else "?"
        redirect_url = f"{redirect_uri}{separator}{urlencode(params)}"

        # Show success page before redirecting
        # Use json.dumps for the JS string context (escapes quotes/backslashes)
        # and html.escape only for the HTML text context
        js_safe_url = json.dumps(redirect_url)[1:-1]  # strip outer quotes
        principal_name = html.escape(principal_info.get("principal_name", ""))
        return HTMLResponse(
            LOGIN_SUCCESS_HTML.replace("{redirect_url}", js_safe_url).replace(
                "{principal_name}", principal_name
            ),
            status_code=200,
        )
