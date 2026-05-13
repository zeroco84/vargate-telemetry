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


def test_sso_callback_preserves_existing_tenant_binding_on_repeat_login(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """T5.6 hotfix: a returning user with `users.tenant_id` already set
    must get a JWT carrying that tenant_id, not `None`.

    The original T4.2 code hardcoded ``tenant_id=None`` in the SSO
    callback's ``issue_session_jwt`` call on the assumption that
    T4.5's select-region would always be the next step. That's true
    for first-time onboarding, but a returning sign-in for an
    already-onboarded user would re-issue a tenant_id-less JWT,
    making ``/me`` report ``tenants=[]``, which the frontend reads
    as "not onboarded" and starts the flow over (re-paste key, etc.).

    This test pins the fix: sign in → manually bind the user to a
    tenant via direct SQL (simulating T4.5's commit) → sign in
    again → decode the re-issued JWT and assert it carries the
    tenant_id.

    Implementation note: TestClient runs over HTTP, which means the
    `Secure` session cookie is *received* (stored in the cookie jar)
    but not *resent* on subsequent http:// requests. We pull the
    JWT value directly out of the jar and decode it instead of
    chasing it via /me.
    """
    from vargate_telemetry.auth.jwt import decode_session_jwt
    from vargate_telemetry.auth.sso import set_exchanger_for_test
    from vargate_telemetry.db import engine

    # First sign-in. User row created, no tenant binding yet.
    set_exchanger_for_test(
        "google",
        StubExchanger(
            sub="google-returning", email="returning@example.com", nonce="n-1"
        ),
    )
    _set_oauth_cookies(client, state="s-1", nonce="n-1")
    r1 = _post_google_callback(client, code="c-1", state="s-1")
    assert r1.status_code == 200
    user_id = r1.json()["user_id"]

    # First-sign-in JWT carries tenant_id=None (no binding yet).
    first_jwt = client.cookies.get("ogma_session")
    assert first_jwt is not None
    first_payload = decode_session_jwt(first_jwt)
    assert first_payload.tenant_id is None

    # Simulate T4.5 select-region: create a tenant + bind the user.
    # (We don't go through the real route to keep the test surface
    # narrow — the binding is what matters for this assertion.)
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES "
                "(:t, 'eu', TRUE, 'trial')"
            ),
            {"t": "tnt_eu_returning_test"},
        )
        conn.execute(
            sql_text("UPDATE users SET tenant_id = :t WHERE id = :uid"),
            {"t": "tnt_eu_returning_test", "uid": user_id},
        )

    # Sign out (clear cookies), then sign in again with the same
    # (provider, sub).
    client.cookies.clear()
    set_exchanger_for_test(
        "google",
        StubExchanger(
            sub="google-returning", email="returning@example.com", nonce="n-2"
        ),
    )
    _set_oauth_cookies(client, state="s-2", nonce="n-2")
    r2 = _post_google_callback(client, code="c-2", state="s-2")
    assert r2.status_code == 200, r2.text
    assert r2.json()["user_id"] == user_id  # same user row

    # The re-issued JWT MUST carry the tenant_id binding — the
    # whole point of the hotfix.
    second_jwt = client.cookies.get("ogma_session")
    assert second_jwt is not None
    second_payload = decode_session_jwt(second_jwt)
    assert second_payload.tenant_id == "tnt_eu_returning_test", (
        f"expected tenant binding preserved in re-issued JWT; "
        f"got tenant_id={second_payload.tenant_id!r}"
    )

    # And the JWT works as a Bearer token: /me reports the tenant.
    # (Bearer side-steps the TestClient HTTP+Secure cookie limitation.)
    me_after = client.get(
        "/me",
        headers={"Authorization": f"Bearer {second_jwt}"},
    )
    assert me_after.status_code == 200, me_after.text
    body = me_after.json()
    assert len(body["tenants"]) == 1, (
        f"expected one tenant in /me response; got {body}"
    )
    assert body["tenants"][0]["tenant_id"] == "tnt_eu_returning_test"


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

    # Expired path — issue with `now` far enough in the past that the
    # token's exp (now + TTL) is BEFORE current time. T5.5.8 bumped
    # SESSION_TOKEN_TTL_SECONDS from 15 min → 8 h; a 2-hour-old
    # token is still valid for 6 more hours and wouldn't trip the
    # expired branch. Use 12 hours back to comfortably clear any
    # reasonable TTL.
    long_past = datetime.now(timezone.utc) - timedelta(hours=12)
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


def test_me_returns_real_tenant_region_from_db(
    clean_auth_state: None,
    client: TestClient,
) -> None:
    """T5.5.8 regression pin: /me must look the region up from the
    `tenants` table, not hardcode "us".

    Pre-T5.5.8 the /me handler had a T4.2 placeholder
    ``TenantSummary(tenant_id=..., region="us")`` that never got
    updated. EU customers saw a "US-EAST · PROD" environment chip
    and a "US" region in the tenant chip — the dashboard looked
    misrouted even though the data was correctly EU-sealed.
    """
    from vargate_telemetry.auth.jwt import issue_session_jwt
    from vargate_telemetry.db import engine

    # Seed an EU tenant in the DB and a JWT bound to it.
    eu_tenant = "tnt_eu_region_test"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'eu', TRUE, 'trial')"
            ),
            {"t": eu_tenant},
        )

    token = issue_session_jwt(
        user_id="user-eu",
        email="eu-user@example.com",
        sso_provider="google",
        tenant_id=eu_tenant,
    )
    r = client.get(
        "/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenants"] == [
        {"tenant_id": eu_tenant, "region": "eu"}
    ], (
        f"/me must return the EU tenant's real region; got "
        f"{body['tenants']!r}"
    )


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
