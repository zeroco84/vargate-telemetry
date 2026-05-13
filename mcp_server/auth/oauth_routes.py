# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — OAuth 2.1 endpoints for the MCP authorization server.

Five endpoints:

- ``GET /.well-known/oauth-authorization-server`` — RFC 8414 server
  metadata. Tells the client (Claude) where to register, authorize,
  and exchange tokens.
- ``GET /.well-known/oauth-protected-resource`` — RFC 9728 resource
  metadata. Tells the client which authorization servers issue
  tokens for this resource.
- ``POST /register`` — RFC 7591 Dynamic Client Registration.
  Claude POSTs its metadata; we issue a ``client_id`` +
  ``client_secret`` + persist the row.
- ``GET /authorize`` — OAuth 2.1 authorization endpoint. **Spike-
  mode-gated**: when ``MCP_SPIKE_MODE`` is unset returns 501.
  When set, logs a WARNING + issues an auth code bound to the
  static MCP_TEST_IDENTITY_* env vars.
- ``POST /token`` — Exchanges auth code (or refresh token) for a
  bearer access token. Validates PKCE.

The spike-mode gate is the single conscious shortcut. Real SSO
bridge ships in TM2 if §6 goes Green. See ``config.py`` and the
WARNING log emitted on every spike-mode /authorize call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from mcp_server import config
from mcp_server.auth.token_verifier import (
    hash_bearer,
    reset_cache_for_test,
)
from vargate_telemetry.db import scheduler_session_scope


_log = logging.getLogger(__name__)

router = APIRouter()


# ───────────────────────────────────────────────────────────────────────────
# In-memory short-lived stores (auth codes, PKCE pairs)
# ───────────────────────────────────────────────────────────────────────────
#
# Both are ≤10-minute TTL so process-local memory is acceptable for
# TM1. A multi-replica deploy would move these to Redis — `pull_state`
# precedent — but TM1 is single-replica behind nginx.
#
# Schema: { code_or_jti: { ...payload, "expires_at_epoch": float } }


_AUTH_CODE_STORE: dict[str, dict] = {}
_REFRESH_TOKEN_STORE: dict[str, dict] = {}

_AUTH_CODE_TTL_SECONDS = 10 * 60


def _store_put(
    store: dict, key: str, payload: dict, ttl_seconds: int
) -> None:
    payload = dict(payload)
    payload["expires_at_epoch"] = (
        datetime.now(timezone.utc).timestamp() + ttl_seconds
    )
    # Lazy GC — sweep entries when storing.
    now = datetime.now(timezone.utc).timestamp()
    expired = [
        k for k, v in store.items() if v["expires_at_epoch"] <= now
    ]
    for k in expired:
        store.pop(k, None)
    store[key] = payload


def _store_pop(store: dict, key: str) -> Optional[dict]:
    payload = store.pop(key, None)
    if payload is None:
        return None
    if payload["expires_at_epoch"] <= datetime.now(
        timezone.utc
    ).timestamp():
        return None
    return payload


def reset_stores_for_test() -> None:
    _AUTH_CODE_STORE.clear()
    _REFRESH_TOKEN_STORE.clear()
    reset_cache_for_test()


# ───────────────────────────────────────────────────────────────────────────
# Metadata endpoints
# ───────────────────────────────────────────────────────────────────────────


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata() -> dict:
    """RFC 8414 — tells Claude where to register / authorize / token."""
    base = config.server_url().rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
        ],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
            "none",  # PKCE-only public client
        ],
        "scopes_supported": ["log_interaction"],
    }


@router.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata() -> dict:
    """RFC 9728 — tells Claude which authorization server issues tokens."""
    base = config.server_url().rstrip("/")
    return {
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": ["log_interaction"],
        "bearer_methods_supported": ["header"],
    }


# ───────────────────────────────────────────────────────────────────────────
# Dynamic Client Registration (RFC 7591)
# ───────────────────────────────────────────────────────────────────────────


class ClientRegistrationRequest(BaseModel):
    """Subset of RFC 7591 fields we accept from Claude.

    Extra fields are ignored — Authlib / Claude may send a bunch
    we don't care about. Reject only on missing required fields.
    """

    model_config = {"extra": "allow"}

    client_name: Optional[str] = None
    redirect_uris: list[str] = Field(..., min_length=1)
    grant_types: list[str] = Field(
        default_factory=lambda: ["authorization_code", "refresh_token"]
    )
    response_types: list[str] = Field(
        default_factory=lambda: ["code"]
    )
    token_endpoint_auth_method: Optional[str] = "none"


def _redirect_uri_allowed(uri: str) -> bool:
    """Validate against the configured allowlist of Claude callbacks."""
    return any(
        uri == prefix or uri.startswith(prefix)
        for prefix in config.ALLOWED_REDIRECT_URI_PREFIXES
    )


@router.post("/register")
def register_client(body: ClientRegistrationRequest) -> dict:
    """Issue a client_id + client_secret for Claude's auto-DCR.

    Validates the supplied ``redirect_uris`` against the
    Anthropic-allowlisted prefixes. Persists the row + returns the
    canonical RFC 7591 response.
    """
    for uri in body.redirect_uris:
        if not _redirect_uri_allowed(uri):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_redirect_uri",
                    "error_description": (
                        f"redirect_uri {uri!r} is not on the "
                        "Anthropic allowlist. Allowed prefixes: "
                        f"{config.ALLOWED_REDIRECT_URI_PREFIXES!r}"
                    ),
                },
            )

    client_id = secrets.token_urlsafe(24)
    client_secret = secrets.token_urlsafe(48)
    client_secret_hash = hashlib.sha256(
        client_secret.encode("utf-8")
    ).hexdigest()
    client_name = body.client_name or "Unnamed MCP Client"

    with scheduler_session_scope() as s:
        s.execute(
            sql_text(
                """
                INSERT INTO mcp_oauth_clients (
                    client_id, client_secret_hash, client_name,
                    redirect_uris, grant_types, response_types,
                    token_endpoint_auth_method
                ) VALUES (
                    :cid, :csh, :cname, :ruris, :gtypes, :rtypes, :am
                )
                """
            ),
            {
                "cid": client_id,
                "csh": client_secret_hash,
                "cname": client_name,
                "ruris": list(body.redirect_uris),
                "gtypes": list(body.grant_types),
                "rtypes": list(body.response_types),
                "am": body.token_endpoint_auth_method,
            },
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,  # returned ONCE; not persisted
        "client_name": client_name,
        "redirect_uris": body.redirect_uris,
        "grant_types": body.grant_types,
        "response_types": body.response_types,
        "token_endpoint_auth_method": body.token_endpoint_auth_method,
    }


# ───────────────────────────────────────────────────────────────────────────
# /authorize — SPIKE-MODE GATED
# ───────────────────────────────────────────────────────────────────────────


_SPIKE_WARNING_TEMPLATE = (
    "═══════════════════════════════════════════════════════════════\n"
    "SPIKE MODE: returning static test identity, not a real SSO flow.\n"
    "Do not promote past TM1. Disable by unsetting MCP_SPIKE_MODE.\n"
    "Request: client_id=%s redirect_uri=%s identity=%s/%s\n"
    "═══════════════════════════════════════════════════════════════"
)


@router.get("/authorize")
def authorize(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
    state: Optional[str] = None,
    resource: Optional[str] = None,
    scope: Optional[str] = None,
):
    """Authorization endpoint. SPIKE-MODE ONLY for TM1.

    Production hardening (the SSO bridge described in TM1 §10) is
    deferred to TM2 if §6 goes Green. Without ``MCP_SPIKE_MODE``,
    this endpoint returns 501 — forcing the next sprint to consciously
    enable the spike gate or build the real bridge.
    """
    if not config.spike_mode_enabled():
        # Hard fail. Production builds without MCP_SPIKE_MODE must
        # not pretend the OAuth flow works.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": "authorize_not_implemented",
                "error_description": (
                    "/authorize is gated behind MCP_SPIKE_MODE=true. "
                    "Production deployments must build the SSO bridge "
                    "described in TM1 §10 before enabling this endpoint."
                ),
            },
        )

    # Validate basic OAuth-2.1 protocol shape.
    if response_type != "code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unsupported_response_type",
                "error_description": (
                    "Only response_type=code is supported."
                ),
            },
        )
    if not code_challenge or code_challenge_method != "S256":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_request",
                "error_description": (
                    "PKCE required: code_challenge + "
                    "code_challenge_method=S256."
                ),
            },
        )
    if not _redirect_uri_allowed(redirect_uri):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_redirect_uri",
                "error_description": (
                    "redirect_uri not on the Claude allowlist."
                ),
            },
        )

    # Validate the client is registered.
    with scheduler_session_scope() as s:
        client_row = s.execute(
            sql_text(
                "SELECT client_id, redirect_uris FROM mcp_oauth_clients "
                "WHERE client_id = :cid AND deleted_at IS NULL"
            ),
            {"cid": client_id},
        ).first()
    if client_row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_client",
                "error_description": "Unknown client_id.",
            },
        )
    if redirect_uri not in client_row.redirect_uris:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_redirect_uri",
                "error_description": (
                    "redirect_uri does not match the registered set."
                ),
            },
        )

    # Pull the static test identity.
    identity = config.test_identity()
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "spike_misconfigured",
                "error_description": (
                    "MCP_SPIKE_MODE is on but "
                    "MCP_TEST_IDENTITY_{TENANT_ID,USER_ID,USER_EMAIL} "
                    "is not fully populated."
                ),
            },
        )

    # LOUD warning. See module docstring.
    _log.warning(
        _SPIKE_WARNING_TEMPLATE,
        client_id,
        redirect_uri,
        identity.tenant_id,
        identity.user_id,
    )

    # Mint a one-time auth code; bind to client_id + identity +
    # code_challenge + resource so /token can verify.
    auth_code = secrets.token_urlsafe(32)
    _store_put(
        _AUTH_CODE_STORE,
        auth_code,
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "user_email": identity.user_email,
            "resource": resource or config.resource_indicator(),
            "scope": scope or "log_interaction",
        },
        _AUTH_CODE_TTL_SECONDS,
    )

    # Redirect Claude back to its callback with the code.
    params = {"code": auth_code}
    if state is not None:
        params["state"] = state
    location = f"{redirect_uri}?{urlencode(params)}"
    return RedirectResponse(location, status_code=302)


# ───────────────────────────────────────────────────────────────────────────
# /token — exchange auth code (or refresh token) for an access token
# ───────────────────────────────────────────────────────────────────────────


def _verify_pkce(verifier: str, expected_challenge: str) -> bool:
    """S256: base64url(SHA-256(verifier)) without padding."""
    import base64

    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    computed = (
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    )
    return secrets.compare_digest(computed, expected_challenge)


@router.post("/token")
async def token_endpoint(request: Request):
    """Exchange a code for an access token, or refresh.

    Form-encoded body per OAuth 2.1 §4.1.3:
      grant_type=authorization_code → code + code_verifier + client_id
      grant_type=refresh_token       → refresh_token + client_id
    """
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        return await _token_authorization_code(form)
    if grant_type == "refresh_token":
        return await _token_refresh(form)

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": "unsupported_grant_type",
            "error_description": (
                "Only authorization_code and refresh_token are "
                "supported."
            ),
        },
    )


async def _token_authorization_code(form) -> JSONResponse:
    code = form.get("code")
    code_verifier = form.get("code_verifier")
    client_id = form.get("client_id")
    redirect_uri = form.get("redirect_uri")

    if not (code and code_verifier and client_id):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "invalid_request",
                "error_description": (
                    "code, code_verifier, and client_id are "
                    "all required."
                ),
            },
        )

    payload = _store_pop(_AUTH_CODE_STORE, code)
    if payload is None:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "invalid_grant",
                "error_description": "Unknown / expired code.",
            },
        )

    if payload["client_id"] != client_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "invalid_grant",
                "error_description": "client_id mismatch.",
            },
        )
    if redirect_uri and payload["redirect_uri"] != redirect_uri:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "invalid_grant",
                "error_description": "redirect_uri mismatch.",
            },
        )
    if not _verify_pkce(code_verifier, payload["code_challenge"]):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "invalid_grant",
                "error_description": "PKCE verifier mismatch.",
            },
        )

    return _issue_token_pair(
        client_id=client_id,
        tenant_id=payload["tenant_id"],
        user_id=payload["user_id"],
        user_email=payload["user_email"],
        resource=payload["resource"],
        scopes=[payload["scope"]],
    )


async def _token_refresh(form) -> JSONResponse:
    refresh_token = form.get("refresh_token")
    client_id = form.get("client_id")
    if not (refresh_token and client_id):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "invalid_request",
                "error_description": (
                    "refresh_token and client_id required."
                ),
            },
        )
    refresh_hash = hash_bearer(refresh_token)
    payload = _store_pop(_REFRESH_TOKEN_STORE, refresh_hash)
    if payload is None or payload["client_id"] != client_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "invalid_grant",
                "error_description": "Unknown / expired refresh token.",
            },
        )
    # OAuth 2.1 §4.3.1: rotate refresh on use.
    return _issue_token_pair(
        client_id=client_id,
        tenant_id=payload["tenant_id"],
        user_id=payload["user_id"],
        user_email=payload["user_email"],
        resource=payload["resource"],
        scopes=payload.get("scopes", ["log_interaction"]),
    )


def _issue_token_pair(
    *,
    client_id: str,
    tenant_id: str,
    user_id: str,
    user_email: str,
    resource: str,
    scopes: list[str],
) -> JSONResponse:
    """Build + persist the access + refresh tokens, return RFC 6749 body."""
    access_token = secrets.token_urlsafe(48)
    refresh_token = secrets.token_urlsafe(48)
    access_hash = hash_bearer(access_token)
    refresh_hash = hash_bearer(refresh_token)
    now = datetime.now(timezone.utc)
    access_expires = now + timedelta(seconds=config.ACCESS_TOKEN_TTL_SECONDS)

    with scheduler_session_scope() as s:
        s.execute(
            sql_text(
                """
                INSERT INTO mcp_access_tokens (
                    token_hash, client_id, tenant_id, user_id,
                    user_email, resource, scopes, expires_at,
                    refresh_token_hash
                ) VALUES (
                    :th, :cid, :t, :u, :e, :r, :sc, :exp, :rh
                )
                """
            ),
            {
                "th": access_hash,
                "cid": client_id,
                "t": tenant_id,
                "u": user_id,
                "e": user_email,
                "r": resource,
                "sc": scopes,
                "exp": access_expires,
                "rh": refresh_hash,
            },
        )

    # Stash the refresh-token → identity mapping in memory. (We
    # don't persist refresh-token rows separately; the
    # `refresh_token_hash` column on `mcp_access_tokens` is for
    # audit lookup, not validation.)
    _store_put(
        _REFRESH_TOKEN_STORE,
        refresh_hash,
        {
            "client_id": client_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "user_email": user_email,
            "resource": resource,
            "scopes": scopes,
        },
        config.REFRESH_TOKEN_TTL_SECONDS,
    )

    return JSONResponse(
        content={
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": config.ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
        }
    )
