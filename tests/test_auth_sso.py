# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the SSO callback + JWT session machinery (T4.2)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text


# Force a deterministic signing key for the JWT module. Overrides
# whatever's in the container env — compose passes JWT_SIGNING_KEY=""
# by default, and `setdefault` would treat empty-string-but-set as
# "already set" and NOT replace it. Tests must never share signing
# keys with prod anyway, so the override is always the right call.
os.environ["JWT_SIGNING_KEY"] = (
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b"
)
os.environ.setdefault("OGMA_OAUTH_REDIRECT_BASE", "http://localhost:5173")


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + stub exchanger
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_auth_state() -> None:
    """Reset users/sessions tables + nonce-replay Redis namespace + stub state."""
    from vargate_telemetry.auth.sso import (
        _reset_nonce_tracking_for_test,
        set_exchanger_for_test,
    )
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE sessions, users RESTART IDENTITY CASCADE")
        )
    _reset_nonce_tracking_for_test()
    set_exchanger_for_test("google", None)
    set_exchanger_for_test("microsoft", None)

    yield

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE sessions, users RESTART IDENTITY CASCADE")
        )
    _reset_nonce_tracking_for_test()
    set_exchanger_for_test("google", None)
    set_exchanger_for_test("microsoft", None)


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


class StubExchanger:
    """Deterministic ID-token claims for tests."""

    def __init__(
        self,
        *,
        sub: str,
        email: str,
        nonce: str,
        name: Optional[str] = None,
    ) -> None:
        from vargate_telemetry.auth.sso import IdTokenClaims

        self._claims = IdTokenClaims(
            sub=sub, email=email, name=name, nonce=nonce
        )
        self.calls: list[tuple[str, str]] = []

    def fetch_id_token_claims(
        self, code: str, redirect_uri: str
    ):  # noqa: D401 — Protocol implementation
        self.calls.append((code, redirect_uri))
        return self._claims


def _set_oauth_cookies(client: TestClient, state: str, nonce: str) -> None:
    """Mimic what the frontend sets before redirecting to the provider."""
    client.cookies.set("ogma_oauth_state", state)
    client.cookies.set("ogma_oauth_nonce", nonce)


def _post_google_callback(
    client: TestClient, code: str, state: str
) -> "object":
    return client.post(
        "/auth/sso/google/callback",
        json={"code": code, "state": state},
    )


# ───────────────────────────────────────────────────────────────────────────
# 1. New-user creation on first sign-in
# ───────────────────────────────────────────────────────────────────────────


def test_sso_callback_creates_new_user_on_first_login(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """First Google callback inserts a fresh row into `users`."""
    from vargate_telemetry.auth.sso import set_exchanger_for_test
    from vargate_telemetry.db import engine

    stub = StubExchanger(
        sub="google-12345",
        email="alice@example.com",
        nonce="nonce-aaa",
        name="Alice Smith",
    )
    set_exchanger_for_test("google", stub)

    _set_oauth_cookies(client, state="state-aaa", nonce="nonce-aaa")
    response = _post_google_callback(client, code="auth-code-1", state="state-aaa")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["name"] == "Alice Smith"
    assert body["user_id"]

    # Row landed in users.
    with engine.connect() as conn:
        rows = list(
            conn.execute(
                sql_text(
                    "SELECT sso_provider, sso_subject_id, email, name "
                    "FROM users WHERE sso_subject_id = :s"
                ),
                {"s": "google-12345"},
            )
        )
    assert len(rows) == 1
    assert rows[0].sso_provider == "google"
    assert rows[0].email == "alice@example.com"
    assert rows[0].name == "Alice Smith"

    # Session cookie set.
    assert "ogma_session" in response.cookies


# ───────────────────────────────────────────────────────────────────────────
# 2. Repeat-login finds the existing user (no duplicate row)
# ───────────────────────────────────────────────────────────────────────────


def test_sso_callback_finds_existing_user_on_repeat_login(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """Second sign-in for the same (provider, sub) bumps last_login_at,
    doesn't create a second row.
    """
    from vargate_telemetry.auth.sso import set_exchanger_for_test
    from vargate_telemetry.db import engine

    # First login.
    set_exchanger_for_test(
        "google",
        StubExchanger(
            sub="google-12345", email="alice@example.com", nonce="n-1"
        ),
    )
    _set_oauth_cookies(client, state="s-1", nonce="n-1")
    r1 = _post_google_callback(client, code="c-1", state="s-1")
    assert r1.status_code == 200
    user_id_first = r1.json()["user_id"]

    # Second login with the same (provider, sub) but a different
    # nonce/state (as it would be in real life).
    set_exchanger_for_test(
        "google",
        StubExchanger(
            sub="google-12345", email="alice@example.com", nonce="n-2"
        ),
    )
    # Fresh cookies for the new sign-in.
    client.cookies.clear()
    _set_oauth_cookies(client, state="s-2", nonce="n-2")
    r2 = _post_google_callback(client, code="c-2", state="s-2")
    assert r2.status_code == 200, r2.text
    user_id_second = r2.json()["user_id"]

    # Same user — same row.
    assert user_id_first == user_id_second

    with engine.connect() as conn:
        count = conn.execute(
            sql_text(
                "SELECT COUNT(*) FROM users WHERE sso_subject_id = :s"
            ),
            {"s": "google-12345"},
        ).scalar()
        last_login = conn.execute(
            sql_text(
                "SELECT last_login_at FROM users WHERE sso_subject_id = :s"
            ),
            {"s": "google-12345"},
        ).scalar()

    assert count == 1
    assert last_login is not None


# ───────────────────────────────────────────────────────────────────────────
# 3. JWT round-trip: issue → verify → expired-token rejection
# ───────────────────────────────────────────────────────────────────────────


def test_jwt_round_trip(clean_auth_state: None) -> None:
    """issue_session_jwt → decode_session_jwt round-trips, and an
    expired token raises InvalidJwtError.
    """
    from vargate_telemetry.auth.jwt import (
        InvalidJwtError,
        decode_session_jwt,
        issue_session_jwt,
    )

    # Happy path — current time.
    token = issue_session_jwt(
        user_id="user-1",
        email="bob@example.com",
        sso_provider="google",
        tenant_id=None,
    )
    payload = decode_session_jwt(token)
    assert payload.sub == "user-1"
    assert payload.email == "bob@example.com"
    assert payload.sso == "google"
    assert payload.tenant_id is None

    # Expired path — issue with `now` in the past so `exp` is also
    # in the past, then decode should raise.
    long_past = datetime.now(timezone.utc) - timedelta(hours=2)
    expired_token = issue_session_jwt(
        user_id="user-1",
        email="bob@example.com",
        sso_provider="google",
        now=long_past,
    )
    with pytest.raises(InvalidJwtError) as excinfo:
        decode_session_jwt(expired_token)
    assert "expired" in str(excinfo.value).lower()


# ───────────────────────────────────────────────────────────────────────────
# 4. CSRF: state-cookie mismatch rejects the callback
# ───────────────────────────────────────────────────────────────────────────


def test_invalid_state_param_rejects_callback(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """If the state in the POST body doesn't match the state cookie,
    the callback returns 401 with `code: invalid_state`.
    """
    from vargate_telemetry.auth.sso import set_exchanger_for_test

    set_exchanger_for_test(
        "google",
        StubExchanger(sub="google-x", email="x@example.com", nonce="n"),
    )

    _set_oauth_cookies(client, state="real-state", nonce="n")
    response = _post_google_callback(
        client,
        code="any",
        state="WRONG-STATE-TAMPERED",
    )
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_state"


# ───────────────────────────────────────────────────────────────────────────
# 5. Replay protection via nonce — same nonce twice is refused
# ───────────────────────────────────────────────────────────────────────────


def test_replay_protection_via_nonce(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """First callback consumes the nonce. A second callback with the
    same nonce (even if state + code re-validate) returns 401
    `code: nonce_replay`.
    """
    from vargate_telemetry.auth.sso import set_exchanger_for_test

    # First call — succeeds.
    set_exchanger_for_test(
        "google",
        StubExchanger(
            sub="google-replay", email="r@example.com", nonce="nonce-once"
        ),
    )
    _set_oauth_cookies(client, state="state-replay", nonce="nonce-once")
    r1 = _post_google_callback(client, code="c", state="state-replay")
    assert r1.status_code == 200, r1.text

    # Second call — same nonce, same state. The state-cookie check
    # would pass again (we re-set the cookies), but the nonce
    # replay-tracker rejects.
    client.cookies.clear()
    _set_oauth_cookies(client, state="state-replay", nonce="nonce-once")
    r2 = _post_google_callback(client, code="c", state="state-replay")
    assert r2.status_code == 401
    detail = r2.json()["detail"]
    assert detail["code"] == "nonce_replay"


# ───────────────────────────────────────────────────────────────────────────
# Bonus: /me returns 401 without a token and 200 with one
# ───────────────────────────────────────────────────────────────────────────


def test_me_requires_authentication_and_returns_user_payload(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """/me is the canonical session check — verifies the middleware."""
    from vargate_telemetry.auth.jwt import issue_session_jwt

    # No cookie → 401.
    r_no_auth = client.get("/me")
    assert r_no_auth.status_code == 401
    assert r_no_auth.json()["detail"]["code"] == "unauthenticated"

    # With a valid JWT → 200.
    token = issue_session_jwt(
        user_id="user-me",
        email="me@example.com",
        sso_provider="google",
    )
    r_auth = client.get(
        "/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_auth.status_code == 200, r_auth.text
    body = r_auth.json()
    assert body["user_id"] == "user-me"
    assert body["email"] == "me@example.com"
    assert body["sso_provider"] == "google"
    assert body["tenants"] == []


# ───────────────────────────────────────────────────────────────────────────
# Logout (T5.6) — clears the session cookie
# ───────────────────────────────────────────────────────────────────────────


def test_logout_returns_204_and_clears_session_cookie(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """POST /auth/logout returns 204 + a Set-Cookie that overwrites
    the session cookie with an empty value and Max-Age=0. Browser
    drops the cookie on receipt.

    Cookie attributes (Path, HttpOnly, Secure, SameSite) must match
    the set-cookie shape from the SSO callback — browsers only
    overwrite cookies whose attributes match. A mismatch leaves the
    original cookie in place.
    """
    r = client.post("/auth/logout")
    assert r.status_code == 204, r.text
    # Body is empty.
    assert r.content == b""

    set_cookie = r.headers.get("set-cookie", "")
    assert "ogma_session=" in set_cookie
    lower = set_cookie.lower()
    # Both Max-Age=0 and the immediate Expires= are present; either
    # triggers eviction across the browser matrix we care about.
    assert "max-age=0" in lower
    # Attributes must mirror the set path (Path + HttpOnly + Secure +
    # SameSite).
    assert "httponly" in lower
    assert "secure" in lower
    assert "samesite=lax" in lower
    assert "path=/" in lower


def test_logout_is_idempotent_without_a_session(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """Logout intentionally isn't gated on `current_user` — calling
    it without a session (cookie expired, never had one) still
    returns 204 + clears whatever cookie might still exist client-
    side. Idempotent."""
    r = client.post("/auth/logout")
    assert r.status_code == 204

    # Second call also succeeds.
    r2 = client.post("/auth/logout")
    assert r2.status_code == 204


def test_logout_clears_cookie_even_with_active_session(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """If the caller has a valid session, logout still clears it.
    The 204 carries the eviction Set-Cookie regardless of whether
    the inbound cookie was valid."""
    from vargate_telemetry.auth.jwt import issue_session_jwt

    token = issue_session_jwt(
        user_id="user-logout",
        email="logout@example.com",
        sso_provider="google",
    )
    # Sanity: /me succeeds first.
    r_me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r_me.status_code == 200

    # Logout returns 204 with the eviction Set-Cookie.
    r_logout = client.post(
        "/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r_logout.status_code == 204
    assert "ogma_session=" in r_logout.headers.get("set-cookie", "")
