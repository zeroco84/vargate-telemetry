# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Unit tests for the T6.S session_id v2 wrap. Pure functions over a
fixed wrap key — no DB / DEK / HSM."""

from __future__ import annotations

import base64
from datetime import date

import pytest
from fastapi import HTTPException

from vargate_telemetry.api.sessions import (
    _SESSION_ID_V2_PREFIX,
    _decode_session_id,
    _encode_session_id,
)

_KEY = b"k" * 32
_KEY_OTHER = b"j" * 32


def test_v2_roundtrip() -> None:
    sid = _encode_session_id(
        date(2026, 5, 20), "user", "alice@example.com", wrap_key=_KEY
    )
    assert sid.startswith(_SESSION_ID_V2_PREFIX)
    d, at, ak = _decode_session_id(sid, wrap_key=_KEY)
    assert (d.isoformat(), at, ak) == ("2026-05-20", "user", "alice@example.com")


def test_v2_does_not_leak_the_actor() -> None:
    """The whole point: the actor handle must NOT be recoverable from the
    id without the key (it was base64-readable in v1)."""
    sid = _encode_session_id(
        date(2026, 5, 20), "user", "alice@example.com", wrap_key=_KEY
    )
    token = sid[len(_SESSION_ID_V2_PREFIX):]
    raw = base64.urlsafe_b64decode((token + "=" * (-len(token) % 4)).encode())
    assert b"alice@example.com" not in raw
    assert b"alice" not in raw


def test_v2_is_deterministic() -> None:
    """Same session → same id (stable URLs + equality), via the SIV nonce."""
    a = _encode_session_id(
        date(2026, 5, 20), "user", "alice@example.com", wrap_key=_KEY
    )
    b = _encode_session_id(
        date(2026, 5, 20), "user", "alice@example.com", wrap_key=_KEY
    )
    assert a == b


def test_v2_differs_per_tenant_key() -> None:
    a = _encode_session_id(date(2026, 5, 20), "user", "x@y.com", wrap_key=_KEY)
    b = _encode_session_id(
        date(2026, 5, 20), "user", "x@y.com", wrap_key=_KEY_OTHER
    )
    assert a != b


def test_v2_cross_tenant_decode_fails() -> None:
    sid = _encode_session_id(date(2026, 5, 20), "user", "x@y.com", wrap_key=_KEY)
    with pytest.raises(HTTPException) as ei:
        _decode_session_id(sid, wrap_key=_KEY_OTHER)
    assert ei.value.status_code == 400


def test_v1_legacy_id_still_decodes() -> None:
    """Pre-T6.S bare-base64url ids keep working (wrap_key ignored)."""
    raw = "2026-05-20|user|bob@example.com"
    v1 = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")
    d, at, ak = _decode_session_id(v1, wrap_key=_KEY)
    assert (d.isoformat(), at, ak) == ("2026-05-20", "user", "bob@example.com")


def test_pipe_in_actor_is_sanitised_round_trip() -> None:
    sid = _encode_session_id(
        date(2026, 5, 20), "user", "a|b@x.com", wrap_key=_KEY
    )
    d, at, ak = _decode_session_id(sid, wrap_key=_KEY)
    assert ak == "a_b@x.com"  # pipe replaced to keep the decode split stable


def test_malformed_v2_is_400() -> None:
    with pytest.raises(HTTPException) as ei:
        _decode_session_id("v2:!!!notbase64!!!", wrap_key=_KEY)
    assert ei.value.status_code == 400


def test_malformed_v1_is_400() -> None:
    with pytest.raises(HTTPException) as ei:
        _decode_session_id("###", wrap_key=_KEY)
    assert ei.value.status_code == 400
