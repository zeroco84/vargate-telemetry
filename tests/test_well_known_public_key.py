# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 Phase B1 — /.well-known/ogma-public-key.json endpoint tests.

The endpoint must:
  - Serve the JWK shape produced by bridge_keys.public_jwk().
  - Be reachable without authentication (the key it serves is the
    public half of an asymmetric pair).
  - Carry a 24-hour Cache-Control header.
  - Match the private key actually used for signing — round-trip
    a signed bridge JWT through the JWK's coordinates as a
    sanity check.
"""

from __future__ import annotations

import base64

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


def _decode_b64url_uint(s: str) -> int:
    pad = "=" * (-len(s) % 4)
    return int.from_bytes(
        base64.urlsafe_b64decode((s + pad).encode("ascii")), "big"
    )


def test_well_known_returns_jwk_shape(client: TestClient) -> None:
    """Happy-path — JWK fields per RFC 7517 + 7518 §6.2.1."""
    response = client.get("/.well-known/ogma-public-key.json")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["kty"] == "EC"
    assert body["crv"] == "P-256"
    assert body["alg"] == "ES256"
    assert body["use"] == "sig"
    assert body["kid"] == "ogma-bridge-test"
    # x + y are base64url-without-padding strings (no '=' chars).
    assert "x" in body and "=" not in body["x"]
    assert "y" in body and "=" not in body["y"]


def test_well_known_is_unauthenticated(client: TestClient) -> None:
    """No bearer / cookie required — public endpoint by design."""
    response = client.get("/.well-known/ogma-public-key.json")
    assert response.status_code == 200


def test_well_known_sets_24h_cache_header(client: TestClient) -> None:
    """24h max-age so CF + clients can cache without manual busting."""
    response = client.get("/.well-known/ogma-public-key.json")
    cache = response.headers.get("cache-control", "")
    assert "public" in cache
    assert "max-age=86400" in cache  # 24h * 60m * 60s


def test_jwk_coordinates_match_loaded_private_key(client: TestClient) -> None:
    """The served public key matches the private key in the loader cache.

    Protects against a future refactor that accidentally serves
    one keypair while signing with another — the kind of bug that
    silently 401s every bridge token in production.
    """
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    response = client.get("/.well-known/ogma-public-key.json")
    body = response.json()

    private_numbers = (
        bridge_keys._load_or_raise().public_key.public_numbers()
    )
    assert _decode_b64url_uint(body["x"]) == private_numbers.x
    assert _decode_b64url_uint(body["y"]) == private_numbers.y


def test_jwt_signed_by_loader_verifies_with_well_known_jwk(
    client: TestClient,
) -> None:
    """End-to-end: a JWT minted by the gateway's loader verifies
    using the public key SERVED by the well-known endpoint.

    This is the contract the MCP server depends on — it never
    holds the private key, only the well-known JWK. If signing
    and serving ever drift, this test catches it before deploy.
    """
    from vargate_telemetry.auth import bridge_keys

    bridge_keys.reset_cache_for_test()
    token = bridge_keys.sign_bridge_token(
        tenant_id="tnt_us_jwk_roundtrip",
        user_id="u",
        user_email="e@e.com",
        mcp_state="s",
    )

    response = client.get("/.well-known/ogma-public-key.json")
    jwk_body = response.json()

    # Reconstruct an EC public key from the JWK coordinates.
    x = _decode_b64url_uint(jwk_body["x"])
    y = _decode_b64url_uint(jwk_body["y"])
    pub_numbers = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1())
    pub_key = pub_numbers.public_key()

    claims = pyjwt.decode(
        token,
        pub_key,
        algorithms=["ES256"],
        audience="mcp-bridge",
        issuer="ogma.vargate.ai",
    )
    assert claims["tenant_id"] == "tnt_us_jwk_roundtrip"
