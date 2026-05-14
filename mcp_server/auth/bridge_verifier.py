# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM2 Phase C2 — bridge JWT verifier (MCP-server side).

The MCP server NEVER holds the bridge JWT private key — that's
the entire point of asymmetric crypto. It receives the public
half as a JWK from Ogma's gateway via the well-known URL (Phase
B1) and uses that to verify the JWT a bridge response carries.

Lifecycle:

  - At startup (Phase C3 — to come), the MCP server fetches
    ``https://ogma.vargate.ai/.well-known/ogma-public-key.json``
    and calls ``set_jwk(...)`` with the response.
  - Daily (Phase C4 — to come), a Celery beat task re-fetches
    and refreshes the cache so a rotated key takes effect within
    24h without a redeploy.
  - On every ``/authorize/callback`` request, ``verify(token)``
    decodes the bridge JWT against the cached public key.

Algorithm posture: ``ES256`` only. pyjwt's ``algorithms=`` arg is
the line of defence against the classic JWT-confusion attacks
(``alg=none`` skipping the signature check; ``alg=HS256`` with
the public key smuggled in as an HMAC secret). Both are rejected
with ``InvalidAlgorithmError`` before signature verification
even starts.

If ``set_jwk()`` has never been called, ``verify()`` raises
RuntimeError. The /authorize/callback handler catches that and
returns 503 — we can't authenticate anything without a public
key. The Phase C3 lifespan refuses to enter the application
state if the fetch fails, so this branch should be unreachable
in production unless someone monkey-patches the cache for tests.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Any, Mapping, Optional

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec


_log = logging.getLogger(__name__)


BRIDGE_JWT_ALGORITHM = "ES256"
BRIDGE_JWT_AUDIENCE = "mcp-bridge"
BRIDGE_JWT_ISSUER = "ogma.vargate.ai"


# ───────────────────────────────────────────────────────────────────────────
# Cached JWK / reconstructed public key
# ───────────────────────────────────────────────────────────────────────────


_cache_lock = threading.Lock()
_cached_jwk: Optional[dict] = None
_cached_public_key: Optional[ec.EllipticCurvePublicKey] = None


def set_jwk(jwk: Mapping[str, Any]) -> None:
    """Populate the cache with a new JWK.

    Called once at startup (Phase C3) and on every Celery beat
    refresh (Phase C4). Also called by tests via a fixture that
    primes the verifier with the conftest-generated keypair's
    public JWK.

    Validates shape minimally: must be ``EC`` / ``P-256``, must
    carry ``x`` and ``y`` coordinates. Anything else raises and
    leaves the previous cache intact — a malformed refresh
    doesn't poison verification.
    """
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError(
            "Bridge JWK must be EC / P-256 "
            f"(got kty={jwk.get('kty')!r}, crv={jwk.get('crv')!r})."
        )
    pub_key = _public_key_from_jwk(jwk)

    global _cached_jwk, _cached_public_key
    with _cache_lock:
        _cached_jwk = dict(jwk)
        _cached_public_key = pub_key
    _log.info(
        "bridge_verifier: cached JWK kid=%s",
        jwk.get("kid", "<unknown>"),
    )


def cached_jwk() -> Optional[dict]:
    """Return the currently-cached JWK, or None if unset.

    Useful for ops introspection (e.g., a /_health line that
    surfaces the loaded kid).
    """
    return _cached_jwk


def reset_for_test() -> None:
    """Test hook — drop the cached JWK so the next call must reset it."""
    global _cached_jwk, _cached_public_key
    with _cache_lock:
        _cached_jwk = None
        _cached_public_key = None


# ───────────────────────────────────────────────────────────────────────────
# Verify
# ───────────────────────────────────────────────────────────────────────────


def verify(token: str) -> Mapping[str, Any]:
    """Verify a bridge JWT against the cached public key.

    Raises:
      RuntimeError — cache never populated. Callers should treat
        this as a 503 (server not initialized).
      pyjwt.PyJWTError or subclass — any signature / claim
        validation failure. The /authorize/callback handler
        collapses these into a single ``invalid_grant`` 400 so
        the caller can't infer which check failed.
    """
    with _cache_lock:
        public_key = _cached_public_key
    if public_key is None:
        raise RuntimeError(
            "bridge_verifier: no JWK cached — startup public-key "
            "fetch did not complete. Production deploys should not "
            "reach this branch; tests must call set_jwk(...) before "
            "exercising the callback."
        )

    return pyjwt.decode(
        token,
        public_key,
        # The single most important line in this module. pyjwt
        # checks the JWT header's `alg` against this list before
        # signature verification — `none` and `HS256` are
        # rejected here, killing the classic confusion attacks.
        algorithms=[BRIDGE_JWT_ALGORITHM],
        audience=BRIDGE_JWT_AUDIENCE,
        issuer=BRIDGE_JWT_ISSUER,
    )


# ───────────────────────────────────────────────────────────────────────────
# JWK → cryptography public key
# ───────────────────────────────────────────────────────────────────────────


def _b64url_uint(s: str) -> int:
    """Inverse of bridge_keys._b64url_uint — base64url-without-padding to int."""
    pad = "=" * (-len(s) % 4)
    return int.from_bytes(
        base64.urlsafe_b64decode((s + pad).encode("ascii")), "big"
    )


def _public_key_from_jwk(jwk: Mapping[str, Any]) -> ec.EllipticCurvePublicKey:
    """Reconstruct an EC P-256 public key from a JWK's x + y."""
    if "x" not in jwk or "y" not in jwk:
        raise ValueError(
            "Bridge JWK missing x or y coordinate."
        )
    x = _b64url_uint(jwk["x"])
    y = _b64url_uint(jwk["y"])
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
