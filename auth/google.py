"""
Google OAuth 2.0 helpers for the Cornerstone MCP server.

Implements the minimum surface needed for the authorization-code flow:
  - exchange_code_for_tokens: POST /token
  - verify_id_token: POST /tokeninfo

Design notes:
  - Uses the existing httpx dependency, no google-auth library, to keep
    the dep surface minimal (matches auth/oauth.py's hand-rolled HMAC JWT).
  - verify_id_token enforces aud, email_verified, and hd claims as
    defence-in-depth on top of the Google Workspace "Internal" consent
    screen. If any check fails, returns None — callers must never treat
    a None result as "maybe valid".
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("cornerstone.oauth.google")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def _env_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def _env_client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def _env_redirect_uri() -> str:
    return os.environ.get("GOOGLE_REDIRECT_URI", "")


def _env_hosted_domain() -> str:
    return os.environ.get("GOOGLE_HOSTED_DOMAIN", "")


def is_configured() -> bool:
    return bool(_env_client_id() and _env_client_secret())


async def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": _env_client_id(),
                "client_secret": _env_client_secret(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        return response.json()


async def verify_id_token(id_token: str) -> Optional[dict]:
    if not id_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                GOOGLE_TOKENINFO_URL,
                params={"id_token": id_token},
            )
            if response.status_code != 200:
                logger.warning("tokeninfo non-200: %s", response.status_code)
                return None
            claims = response.json()
    except Exception as e:
        logger.warning("tokeninfo call failed: %s", e)
        return None

    expected_aud = _env_client_id()
    if claims.get("aud") != expected_aud:
        logger.warning("id_token aud mismatch: got %s", claims.get("aud"))
        return None

    email_verified = claims.get("email_verified")
    if not (email_verified is True or str(email_verified).lower() == "true"):
        logger.warning("id_token email not verified")
        return None

    expected_hd = _env_hosted_domain()
    if expected_hd and claims.get("hd") != expected_hd:
        logger.warning(
            "id_token hd mismatch: expected %s, got %s",
            expected_hd,
            claims.get("hd"),
        )
        return None

    return {
        "email": claims.get("email", "").lower().strip(),
        "name": claims.get("name") or claims.get("email", "").split("@")[0],
        "sub": claims.get("sub"),
        "hd": claims.get("hd"),
    }


def build_authorization_url(state_jwt: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": _env_client_id(),
        "redirect_uri": _env_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state_jwt,
        "access_type": "online",
        "prompt": "select_account",
    }
    hd = _env_hosted_domain()
    if hd:
        params["hd"] = hd
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
