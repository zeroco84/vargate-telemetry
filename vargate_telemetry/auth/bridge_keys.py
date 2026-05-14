# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 — Bridge JWT keypair loader + signer (Phase A2).

The TM2 SSO bridge between Ogma's gateway and the MCP server is
glued together by short-lived JWTs (60-second TTL). Those tokens
are signed asymmetrically so:

  - The MCP server doesn't need to share a signing secret with
    Ogma's gateway (kill the symmetric-secret trust boundary).
  - The public key can be served on a stable, cacheable
    ``/.well-known/ogma-public-key.json`` URL that the MCP server
    fetches at boot.

Algorithm choice: **ECDSA P-256 (ES256)**. Faster signing/verify
than RSA, smaller keys, native JWK support across pyjwt + Authlib.

Storage choice: **file-mounted PEM**, NOT HSM-backed. See the
CLAUDE.md memory rule "Bridge JWT keypair is file-mounted ECDSA
P-256". 60-second tokens have a bounded blast radius if a key
leaks; the operational cost of PKCS#11 signing doesn't earn its
complexity for that horizon. Move to HSM when general key-rotation
infrastructure exists across the stack.

The private key file is the SINGLE source of truth. The public
key is derived from it in-memory at load time. This kills the
"public-key file out of sync with private-key file" class of bug
that rotation churn produces.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


_log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Config
# ───────────────────────────────────────────────────────────────────────────


def _private_key_path() -> Path:
    """Read the env-configured path to the bridge JWT private PEM.

    Production default: ``/run/secrets/bridge_jwt_private.pem``,
    a bind-mount from the host's ``/home/vargate/secrets/...``.
    Tests override via ``conftest.py`` to a tmp-file path.
    """
    raw = os.environ.get(
        "OGMA_BRIDGE_JWT_PRIVATE_KEY_PATH",
        "/run/secrets/bridge_jwt_private.pem",
    )
    return Path(raw)


def _kid() -> str:
    """Read the bridge JWT key id (used in the JWK header).

    Rotation marker: when the private key file is replaced, also
    bump the kid so clients (the MCP server) can tell the active
    public key from a stale cache.
    """
    return os.environ.get("OGMA_BRIDGE_JWT_KID", "ogma-bridge-v1")


BRIDGE_JWT_ALGORITHM = "ES256"
BRIDGE_JWT_AUDIENCE = "mcp-bridge"
BRIDGE_JWT_ISSUER = "ogma.vargate.ai"
BRIDGE_JWT_TTL_SECONDS = 60


# ───────────────────────────────────────────────────────────────────────────
# Loader + cache
# ───────────────────────────────────────────────────────────────────────────


_cache_lock = threading.Lock()
_cached: Optional["_LoadedKeypair"] = None


class _LoadedKeypair:
    """One private key, its derived public key, and the kid + algorithm.

    Held by module singleton so re-reading the PEM file every time
    we sign or expose the JWK doesn't cost a syscall. Reset via
    :func:`reset_cache_for_test` when test fixtures replace the
    keypair file mid-process.
    """

    def __init__(
        self,
        private_key: ec.EllipticCurvePrivateKey,
        kid: str,
    ) -> None:
        self.private_key = private_key
        self.public_key = private_key.public_key()
        self.kid = kid


def _load_or_raise() -> _LoadedKeypair:
    """Read the PEM, parse it, return the cached keypair.

    Raises FileNotFoundError if the configured path doesn't exist.
    Production: the operator generates the file once and ensures
    the bind-mount is wired. Tests: the conftest fixture generates
    a tmp keypair and sets ``OGMA_BRIDGE_JWT_PRIVATE_KEY_PATH``
    before any module that calls this is imported.
    """
    global _cached
    with _cache_lock:
        if _cached is not None:
            return _cached

        path = _private_key_path()
        if not path.exists():
            raise FileNotFoundError(
                f"Bridge JWT private key not found at {path}. "
                "Generate one with "
                "`python scripts/generate_bridge_jwt_keypair.py "
                f"--out {path}` "
                "or set OGMA_BRIDGE_JWT_PRIVATE_KEY_PATH to an "
                "existing PEM."
            )

        pem_bytes = path.read_bytes()
        # `password=None` because the operator handles file-level
        # access control (0600 + chown root); encrypting the PEM
        # would just move the secret problem one level up.
        loaded = serialization.load_pem_private_key(
            pem_bytes, password=None
        )
        if not isinstance(loaded, ec.EllipticCurvePrivateKey):
            raise ValueError(
                "Bridge JWT key must be an EC private key "
                f"(got {type(loaded).__name__})."
            )
        # ES256 is locked to P-256. Anything else is a misconfiguration
        # — fail loud rather than silently sign with the wrong curve.
        curve_name = loaded.curve.name
        if curve_name != "secp256r1":
            raise ValueError(
                f"Bridge JWT key must use P-256 (secp256r1); "
                f"got {curve_name!r}. ES256 requires P-256."
            )

        _cached = _LoadedKeypair(loaded, _kid())
        _log.info(
            "bridge_keys: loaded ES256 keypair from %s (kid=%s)",
            path,
            _cached.kid,
        )
        return _cached


def reset_cache_for_test() -> None:
    """Test hook — drop the singleton so the next call re-reads the file."""
    global _cached
    with _cache_lock:
        _cached = None


# ───────────────────────────────────────────────────────────────────────────
# Sign + verify
# ───────────────────────────────────────────────────────────────────────────


def sign_bridge_token(
    *,
    tenant_id: str,
    user_id: str,
    user_email: str,
    mcp_state: str,
    ttl_seconds: int = BRIDGE_JWT_TTL_SECONDS,
) -> str:
    """Mint a TM2 bridge JWT.

    Claims:
      - ``iss`` = ``ogma.vargate.ai``
      - ``aud`` = ``mcp-bridge``  (RFC 8707 audience binding)
      - ``exp`` = now + ttl_seconds
      - ``iat`` = now
      - ``tenant_id`` / ``user_id`` / ``user_email`` — the identity
        the MCP server packs into the OAuth code on receipt
      - ``mcp_state`` — opaque round-trip value the MCP server uses
        to recover its pre-redirect state from Redis

    Header carries ``alg=ES256`` and the active ``kid`` so the
    consumer can pick the right public key from the JWKS when we
    eventually run with rotation.
    """
    keypair = _load_or_raise()
    now = datetime.now(timezone.utc)
    payload = {
        "iss": BRIDGE_JWT_ISSUER,
        "aud": BRIDGE_JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "user_email": user_email,
        "mcp_state": mcp_state,
    }
    return pyjwt.encode(
        payload,
        keypair.private_key,
        algorithm=BRIDGE_JWT_ALGORITHM,
        headers={"kid": keypair.kid},
    )


def verify_bridge_token(token: str) -> Mapping[str, Any]:
    """Verify + decode a bridge JWT against the active public key.

    Used by tests, and by the MCP server when it imports this
    module (it never holds the private key in production — it
    fetches the public-only JWK from /.well-known/ — but local
    unit tests verify against the same loaded keypair).

    Raises pyjwt's standard exceptions on failure (ExpiredSignatureError,
    InvalidAudienceError, InvalidSignatureError, etc.).
    """
    keypair = _load_or_raise()
    return pyjwt.decode(
        token,
        keypair.public_key,
        algorithms=[BRIDGE_JWT_ALGORITHM],
        audience=BRIDGE_JWT_AUDIENCE,
        issuer=BRIDGE_JWT_ISSUER,
    )


# ───────────────────────────────────────────────────────────────────────────
# JWK encoding for the /.well-known/ endpoint
# ───────────────────────────────────────────────────────────────────────────


def _b64url_uint(value: int) -> str:
    """Encode an unsigned int as base64url-without-padding (RFC 7518 §6.2.1.2)."""
    byte_len = (value.bit_length() + 7) // 8 or 1
    raw = value.to_bytes(byte_len, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_jwk() -> dict:
    """Return the active public key as a JWK (RFC 7517 §4).

    Shape for an ECDSA P-256 key (per RFC 7518 §6.2.1):
      {
        "kty": "EC",
        "crv": "P-256",
        "x":   "<base64url(public.x)>",
        "y":   "<base64url(public.y)>",
        "kid": "<kid>",
        "alg": "ES256",
        "use": "sig"
      }
    """
    keypair = _load_or_raise()
    numbers = keypair.public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url_uint(numbers.x),
        "y": _b64url_uint(numbers.y),
        "kid": keypair.kid,
        "alg": BRIDGE_JWT_ALGORITHM,
        "use": "sig",
    }


def public_jwks() -> dict:
    """Return a JWKS-formatted document — a list-of-keys wrapper.

    Single-key today; future-proofs the endpoint shape for the
    rotation case where two keys (old + new) are valid at once.
    """
    return {"keys": [public_jwk()]}
