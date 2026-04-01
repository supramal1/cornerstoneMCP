"""
OAuth 2.1 Proof Script — verifies the full PKCE flow against the live MCP server.

Exercises every stage:
  1. Dynamic client registration
  2. Authorization request → login page URL
  3. API key validation + auth code issuance (simulated form POST)
  4. Token exchange (code → access + refresh tokens)
  5. Access token verification (JWT decode)
  6. Refresh token exchange (new access + refresh tokens)
  7. csk_ legacy Bearer token passthrough

Usage:
    python proof/oauth_v1_proof.py [--url URL] [--api-key KEY]

Defaults:
    --url   https://cornerstone-mcp-34862349933.europe-west2.run.app
    --api-key  (reads from CORNERSTONE_API_KEY env var or .env file)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import base64
from urllib.parse import urlparse, parse_qs

import httpx


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_api_key() -> str:
    key = os.environ.get("CORNERSTONE_API_KEY", "")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line.startswith("CORNERSTONE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main():
    parser = argparse.ArgumentParser(description="OAuth 2.1 PKCE proof script")
    parser.add_argument(
        "--url",
        default="https://cornerstone-mcp-34862349933.europe-west2.run.app",
    )
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    api_key = args.api_key or _load_api_key()
    if not api_key:
        print("FAIL: No API key provided. Set CORNERSTONE_API_KEY or use --api-key")
        sys.exit(1)

    results = []

    def check(name: str, passed: bool, detail: str = ""):
        status = "PASS" if passed else "FAIL"
        results.append((name, passed))
        msg = f"  [{status}] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        return passed

    client = httpx.Client(base_url=base_url, timeout=30, follow_redirects=False)
    print(f"\nOAuth 2.1 Proof — {base_url}\n{'=' * 60}")

    # ---------------------------------------------------------------
    # Step 1: Dynamic Client Registration
    # ---------------------------------------------------------------
    print("\n1. Dynamic Client Registration")
    reg_resp = client.post(
        "/register",
        json={
            "client_name": "oauth-proof-script",
            "redirect_uris": ["http://127.0.0.1:9999/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    check(
        "Registration HTTP 200",
        reg_resp.status_code in (200, 201),
        f"got {reg_resp.status_code}",
    )
    reg_data = reg_resp.json()
    client_id = reg_data.get("client_id", "")
    check(
        "client_id returned",
        bool(client_id),
        client_id[:20] + "..." if client_id else "missing",
    )

    # ---------------------------------------------------------------
    # Step 2: Authorization Request (PKCE)
    # ---------------------------------------------------------------
    print("\n2. Authorization Request (PKCE)")
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = secrets.token_urlsafe(16)

    auth_resp = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:9999/callback",
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "memory",
        },
    )
    check(
        "Authorization redirects",
        auth_resp.status_code in (302, 303, 307),
        f"got {auth_resp.status_code}",
    )
    login_url = auth_resp.headers.get("location", "")
    check(
        "Login URL contains /oauth/login",
        "/oauth/login" in login_url,
        login_url[:80] + "...",
    )

    # Extract session JWT from login URL
    parsed = urlparse(login_url)
    session_jwt = parse_qs(parsed.query).get("session", [""])[0]
    check("Session JWT present", bool(session_jwt), f"{len(session_jwt)} chars")

    # ---------------------------------------------------------------
    # Step 3: Login form submission (API key validation)
    # ---------------------------------------------------------------
    print("\n3. Login (API key validation + auth code)")
    login_resp = client.post(
        "/oauth/login",
        data={"session": session_jwt, "api_key": api_key},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    check(
        "Login returns 200",
        login_resp.status_code == 200,
        f"got {login_resp.status_code}",
    )

    # Extract redirect URL from the success page JavaScript
    html_body = login_resp.text
    redirect_match = re.search(r'window\.location\.href\s*=\s*"([^"]+)"', html_body)
    redirect_url = redirect_match.group(1) if redirect_match else ""
    check(
        "Redirect URL in success page",
        bool(redirect_url),
        redirect_url[:80] + "..." if redirect_url else "missing",
    )

    # Extract auth code and state from redirect URL
    redirect_parsed = urlparse(redirect_url)
    redirect_params = parse_qs(redirect_parsed.query)
    auth_code = redirect_params.get("code", [""])[0]
    returned_state = redirect_params.get("state", [""])[0]
    check("Auth code returned", bool(auth_code), f"{len(auth_code)} chars")
    check(
        "State matches",
        returned_state == state,
        f"expected={state[:12]}... got={returned_state[:12]}...",
    )

    # ---------------------------------------------------------------
    # Step 4: Token Exchange (code → tokens)
    # ---------------------------------------------------------------
    print("\n4. Token Exchange")
    token_resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": "http://127.0.0.1:9999/callback",
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    check(
        "Token exchange HTTP 200",
        token_resp.status_code == 200,
        f"got {token_resp.status_code}",
    )
    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    check("access_token returned", bool(access_token), f"{len(access_token)} chars")
    check("refresh_token returned", bool(refresh_token), f"{len(refresh_token)} chars")
    check("token_type is Bearer", token_data.get("token_type") == "Bearer")
    check(
        "expires_in present",
        token_data.get("expires_in", 0) > 0,
        f"{token_data.get('expires_in')}s",
    )

    # ---------------------------------------------------------------
    # Step 5: Decode JWT (verify structure)
    # ---------------------------------------------------------------
    print("\n5. JWT Access Token Structure")
    parts = access_token.split(".")
    check("JWT has 3 parts", len(parts) == 3)
    if len(parts) == 3:
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        check("type=access", payload.get("type") == "access")
        check(
            "sub (principal_id) present",
            bool(payload.get("sub")),
            payload.get("sub", "")[:20],
        )
        check("akob (obfuscated key) present", bool(payload.get("akob")))
        check("scopes include memory", "memory" in payload.get("scopes", []))

    # ---------------------------------------------------------------
    # Step 6: Refresh Token Exchange
    # ---------------------------------------------------------------
    print("\n6. Refresh Token Exchange")
    refresh_resp = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    check(
        "Refresh exchange HTTP 200",
        refresh_resp.status_code == 200,
        f"got {refresh_resp.status_code}",
    )
    refresh_data = refresh_resp.json()
    new_access = refresh_data.get("access_token", "")
    new_refresh = refresh_data.get("refresh_token", "")
    check("New access_token returned", bool(new_access))
    check("New refresh_token returned", bool(new_refresh))
    check(
        "New tokens differ from original",
        new_access != access_token and new_refresh != refresh_token,
    )

    # ---------------------------------------------------------------
    # Step 7: csk_ Legacy Bearer Passthrough
    # ---------------------------------------------------------------
    print("\n7. csk_ Legacy Bearer Token")
    # The /mcp endpoint should accept csk_ tokens via Bearer header
    # A POST to /mcp with proper MCP protocol should get past auth
    legacy_resp = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1,
        },
    )
    check(
        "csk_ Bearer accepted (not 401)",
        legacy_resp.status_code != 401,
        f"got {legacy_resp.status_code}",
    )

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    client.close()
    total = len(results)
    passed = sum(1 for _, p in results if p)
    failed = total - passed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed:
        print("\nFailed checks:")
        for name, p in results:
            if not p:
                print(f"  - {name}")
        sys.exit(1)
    else:
        print("All checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
