# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 Phase A2 — bridge JWT keypair loader + JWK + sign/verify tests.

The conftest fixture pre-generates a tmp keypair and sets the
two env vars (``OGMA_BRIDGE_JWT_PRIVATE_KEY_PATH``,
``OGMA_BRIDGE_JWT_KID``) before any test module is imported. These
tests exercise:

  - Loader returns a cached ES256 keypair from the configured PEM.
  - JWK shape matches RFC 7517 §4 + RFC 7518 §6.2.1 (EC P-256).
  - Sign/verify round-trip succeeds.
  - Tampered payload fails verify.
  - Wrong-audience JWT fails verify.
  - Expired JWT fails verify.
  - Wrong-curve PEM is rejected at load time (fail loud).
"""

from __future__ import annotations

import base64
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _decode_b64url_uint(s: str) -> int:
    """Inverse of bridge_keys._b64url_uint, for JWK shape assertions."""
    pad = "=" * (-len(s) % 4)
    raw = base64.urlsafe_b64decode((s + pad).encode("ascii"))
    return int.from_bytes(raw, "big")


# ───────────────────────────────────────────────────────────────────────────
# Loader + caching
# ───────────────────────────────────────────────────────────────────────────


def test_load_returns_es256_keypair() -> None:
    """The conftest-seeded PEM loads as an ES256 P-256 keypair."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    keypair = bridge_keys._load_or_raise()
    assert isinstance(keypair.private_key, ec.EllipticCurvePrivateKey)
    assert keypair.private_key.curve.name == "secp256r1"
    assert keypair.kid == "ogma-bridge-test"


def test_load_is_cached() -> None:
    """Two calls return the same object — no re-reading the file."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    a = bridge_keys._load_or_raise()
    b = bridge_keys._load_or_raise()
    assert a is b


def test_load_missing_file_raises_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured env var should produce a useful FileNotFoundError."""
    from vargate_telemetry.auth import bridge_keys

    monkeypatch.setenv(
        "OGMA_BRIDGE_JWT_PRIVATE_KEY_PATH",
        "/tmp/this-path-does-not-exist-for-tests.pem",
    )
    bridge_keys.reset_cache_for_test()
    with pytest.raises(FileNotFoundError, match="Bridge JWT private key"):
        bridge_keys._load_or_raise()
    # restore the cache for downstream tests
    bridge_keys.reset_cache_for_test()


def test_load_wrong_curve_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A P-384 key in the configured slot must fail loud, not silently sign with the wrong curve."""
    from vargate_telemetry.auth import bridge_keys

    bad_key = ec.generate_private_key(ec.SECP384R1())
    bad_pem = bad_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    bad_path = tmp_path / "bad_curve.pem"
    bad_path.write_bytes(bad_pem)

    monkeypatch.setenv("OGMA_BRIDGE_JWT_PRIVATE_KEY_PATH", str(bad_path))
    bridge_keys.reset_cache_for_test()
    with pytest.raises(ValueError, match="must use P-256"):
        bridge_keys._load_or_raise()
    bridge_keys.reset_cache_for_test()


# ───────────────────────────────────────────────────────────────────────────
# JWK shape (RFC 7517 / 7518)
# ───────────────────────────────────────────────────────────────────────────


def test_public_jwk_has_required_fields() -> None:
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    jwk = bridge_keys.public_jwk()

    assert jwk["kty"] == "EC"
    assert jwk["crv"] == "P-256"
    assert jwk["alg"] == "ES256"
    assert jwk["use"] == "sig"
    assert jwk["kid"] == "ogma-bridge-test"
    # x + y are base64url-without-padding, encode unsigned ints.
    assert "x" in jwk and "=" not in jwk["x"]
    assert "y" in jwk and "=" not in jwk["y"]


def test_public_jwk_matches_loaded_private_key() -> None:
    """The JWK coordinates round-trip back to the same public key."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    jwk = bridge_keys.public_jwk()
    numbers = bridge_keys._load_or_raise().public_key.public_numbers()

    assert _decode_b64url_uint(jwk["x"]) == numbers.x
    assert _decode_b64url_uint(jwk["y"]) == numbers.y


def test_jwks_wraps_jwk_in_keys_array() -> None:
    """RFC 7517 JWKS shape — single-key today, but the wrapper future-proofs rotation."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    jwks = bridge_keys.public_jwks()
    assert isinstance(jwks["keys"], list)
    assert len(jwks["keys"]) == 1
    assert jwks["keys"][0]["kty"] == "EC"


# ───────────────────────────────────────────────────────────────────────────
# Sign / verify round-trip
# ───────────────────────────────────────────────────────────────────────────


def test_sign_and_verify_round_trip() -> None:
    """Happy path: mint a token + verify with the same loaded keypair."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    token = bridge_keys.sign_bridge_token(
        tenant_id="tnt_us_round_trip",
        user_id="user-rt",
        user_email="rt@example.com",
        mcp_state="state-rt",
    )
    claims = bridge_keys.verify_bridge_token(token)
    assert claims["tenant_id"] == "tnt_us_round_trip"
    assert claims["user_id"] == "user-rt"
    assert claims["user_email"] == "rt@example.com"
    assert claims["mcp_state"] == "state-rt"
    assert claims["aud"] == "mcp-bridge"
    assert claims["iss"] == "ogma.vargate.ai"


def test_token_header_carries_kid_and_alg() -> None:
    """Verifying consumers select the public key by `kid` from the JWKS."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    token = bridge_keys.sign_bridge_token(
        tenant_id="t",
        user_id="u",
        user_email="e@e.com",
        mcp_state="s",
    )
    headers = pyjwt.get_unverified_header(token)
    assert headers["kid"] == "ogma-bridge-test"
    assert headers["alg"] == "ES256"


def test_tampered_payload_fails_verify() -> None:
    """Flip a bit in the middle of the encoded payload — signature must fail."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    token = bridge_keys.sign_bridge_token(
        tenant_id="tnt_us_tamper",
        user_id="u",
        user_email="e@e.com",
        mcp_state="s",
    )
    header_b64, payload_b64, sig_b64 = token.split(".")
    tampered_payload = payload_b64[:-1] + (
        "A" if payload_b64[-1] != "A" else "B"
    )
    tampered = ".".join([header_b64, tampered_payload, sig_b64])
    with pytest.raises(pyjwt.InvalidSignatureError):
        bridge_keys.verify_bridge_token(tampered)


def test_wrong_audience_fails_verify() -> None:
    """A JWT minted with a different `aud` claim is rejected."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    keypair = bridge_keys._load_or_raise()
    bogus = pyjwt.encode(
        {
            "iss": bridge_keys.BRIDGE_JWT_ISSUER,
            "aud": "not-mcp-bridge",
            "exp": int(time.time()) + 60,
            "iat": int(time.time()),
            "tenant_id": "x",
            "user_id": "y",
            "user_email": "z@z.com",
            "mcp_state": "s",
        },
        keypair.private_key,
        algorithm=bridge_keys.BRIDGE_JWT_ALGORITHM,
        headers={"kid": keypair.kid},
    )
    with pytest.raises(pyjwt.InvalidAudienceError):
        bridge_keys.verify_bridge_token(bogus)


def test_expired_token_fails_verify() -> None:
    """A JWT past its exp claim is rejected by pyjwt's leeway-free verify."""
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    token = bridge_keys.sign_bridge_token(
        tenant_id="t",
        user_id="u",
        user_email="e@e.com",
        mcp_state="s",
        ttl_seconds=-10,  # born already-expired
    )
    with pytest.raises(pyjwt.ExpiredSignatureError):
        bridge_keys.verify_bridge_token(token)
