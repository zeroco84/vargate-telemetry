# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM2 Phase C3 — bridge JWK fetch + lifespan wiring tests.

The MCP server's FastAPI lifespan calls
``bridge_verifier.fetch_and_cache_jwk()`` once at startup. If the
HTTP fetch fails after all retries, the lifespan re-raises so
uvicorn refuses to start — a silent-degraded boot would 503 every
/authorize/callback.

Tests:

  - Happy path: a 200 response with valid JWK primes the cache.
  - Custom URL override is honored.
  - Retry-then-success: first attempt 503, second attempt 200 →
    cache populated, fetch returns the JWK.
  - All-retries-failed: a persistent error raises after the last
    attempt; cache remains empty.
  - Invalid-shape response: a 200 with non-JWK JSON raises
    ValueError from set_jwk's validation; cache remains empty.

The fetcher is exercised by monkeypatching ``httpx.Client`` to
return canned responses. The lifespan integration is implicit
(when the test app boots via `with TestClient(app) as ...:` —
not normally needed for the OAuth tests since they bypass the
lifespan via the primed_verifier fixture).
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def reset_cache():
    """Wipe the verifier cache between every case so order doesn't matter."""
    from mcp_server.auth import bridge_verifier

    bridge_verifier.reset_for_test()
    yield
    bridge_verifier.reset_for_test()


def _mock_httpx_response(status_code: int, json_body: dict | list | str):
    """Build a stand-in for an httpx Response object."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body
    if status_code >= 400:
        # httpx.HTTPStatusError carries the response in .response —
        # the fetcher uses .raise_for_status() which builds this.
        import httpx

        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"server returned {status_code}",
            request=MagicMock(),
            response=response,
        )
    else:
        response.raise_for_status.return_value = None
    return response


def _mock_httpx_client(*responses):
    """A context-manager-aware MagicMock that returns the canned responses in sequence."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(side_effect=responses)
    return MagicMock(return_value=client)


VALID_JWK = {
    "kty": "EC",
    "crv": "P-256",
    "x": "qvR2hUiUdV6KMETXQrAfwV8Yi0bSF-ycIxc0zLV5zDQ",
    "y": "N0tKSvkgT772rpGnjAqOZyGWwPztehbfJOGLw4CmvP4",
    "kid": "ogma-bridge-test-fetch",
    "alg": "ES256",
    "use": "sig",
}


def test_happy_path_primes_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful 200 response writes the JWK into the verifier cache."""
    from mcp_server.auth import bridge_verifier

    response = _mock_httpx_response(200, VALID_JWK)
    monkeypatch.setattr(
        "httpx.Client", _mock_httpx_client(response)
    )

    returned = bridge_verifier.fetch_and_cache_jwk()
    assert returned == VALID_JWK
    cached = bridge_verifier.cached_jwk()
    assert cached["kid"] == "ogma-bridge-test-fetch"
    assert cached["alg"] == "ES256"


def test_custom_url_override_is_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_and_cache_jwk(url=...) overrides config.ogma_public_key_url()."""
    from mcp_server.auth import bridge_verifier

    response = _mock_httpx_response(200, VALID_JWK)
    client_mock = _mock_httpx_client(response)
    monkeypatch.setattr("httpx.Client", client_mock)

    bridge_verifier.fetch_and_cache_jwk(
        url="https://staging.ogma.example.com/.well-known/key.json"
    )
    # The mocked client's .get() was called with the override URL.
    args, _ = client_mock.return_value.get.call_args
    assert args[0] == "https://staging.ogma.example.com/.well-known/key.json"


def test_retry_succeeds_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 followed by a 200 should still land cached."""
    from mcp_server.auth import bridge_verifier

    first = _mock_httpx_response(503, {})
    second = _mock_httpx_response(200, VALID_JWK)
    monkeypatch.setattr(
        "httpx.Client", _mock_httpx_client(first, second)
    )
    # Squash the linear backoff so the test runs fast.
    monkeypatch.setattr(bridge_verifier, "_FETCH_BACKOFF_BASE_SECONDS", 0)

    returned = bridge_verifier.fetch_and_cache_jwk()
    assert returned["kid"] == "ogma-bridge-test-fetch"


def test_all_retries_fail_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistent failure exhausts retries + raises; cache stays empty."""
    from mcp_server.auth import bridge_verifier

    failing = _mock_httpx_response(503, {})
    monkeypatch.setattr(
        "httpx.Client",
        _mock_httpx_client(failing, failing, failing),
    )
    monkeypatch.setattr(bridge_verifier, "_FETCH_BACKOFF_BASE_SECONDS", 0)

    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        bridge_verifier.fetch_and_cache_jwk()
    assert bridge_verifier.cached_jwk() is None


def test_invalid_jwk_shape_raises_and_keeps_cache_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response with non-EC-P-256 JSON is rejected by set_jwk().

    Each retry hits the same canned response (the gateway returns
    the same bogus shape — we mock three responses to exhaust the
    retry loop deterministically).
    """
    from mcp_server.auth import bridge_verifier

    bogus_jwk = {"kty": "RSA", "n": "...", "e": "AQAB"}
    r1 = _mock_httpx_response(200, bogus_jwk)
    r2 = _mock_httpx_response(200, bogus_jwk)
    r3 = _mock_httpx_response(200, bogus_jwk)
    monkeypatch.setattr(
        "httpx.Client", _mock_httpx_client(r1, r2, r3)
    )
    monkeypatch.setattr(bridge_verifier, "_FETCH_BACKOFF_BASE_SECONDS", 0)

    # The validation error in set_jwk() bubbles up through retries —
    # each attempt fails the same way, so all three exhaust.
    with pytest.raises(ValueError, match="EC / P-256"):
        bridge_verifier.fetch_and_cache_jwk()
    assert bridge_verifier.cached_jwk() is None


def test_lifespan_calls_fetch_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the MCP server's lifespan enters, it must call the fetcher.

    Uses `with TestClient(app)` to actually enter the lifespan
    (the default TestClient(app) skips it).
    """
    from fastapi.testclient import TestClient
    from mcp_server.auth import bridge_verifier

    fetch_called = MagicMock()

    def fake_fetch(*args, **kwargs):
        fetch_called(*args, **kwargs)
        bridge_verifier.set_jwk(VALID_JWK)
        return VALID_JWK

    monkeypatch.setattr(
        bridge_verifier, "fetch_and_cache_jwk", fake_fetch
    )

    # Import lazy to ensure the monkeypatch is in place before the
    # lifespan reads bridge_verifier.fetch_and_cache_jwk.
    from mcp_server.main import app

    with TestClient(app) as _client:
        # Inside the `with`, the lifespan ran. The fetcher should
        # have been invoked once.
        assert fetch_called.call_count == 1
        assert bridge_verifier.cached_jwk()["kid"] == VALID_JWK["kid"]
