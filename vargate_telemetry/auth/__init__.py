# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Auth layer for Ogma — SSO callbacks, JWT sessions, FastAPI deps."""

from vargate_telemetry.auth.jwt import (
    SESSION_COOKIE_NAME,
    SESSION_TOKEN_TTL_SECONDS,
    JwtPayload,
    decode_session_jwt,
    issue_session_jwt,
)
from vargate_telemetry.auth.middleware import (
    AuthenticatedUser,
    current_user,
)
from vargate_telemetry.auth.sso import (
    SUPPORTED_PROVIDERS,
    SsoCallbackResult,
    TokenExchanger,
    handle_sso_callback,
    set_exchanger_for_test,
)

__all__ = [
    "AuthenticatedUser",
    "JwtPayload",
    "SESSION_COOKIE_NAME",
    "SESSION_TOKEN_TTL_SECONDS",
    "SUPPORTED_PROVIDERS",
    "SsoCallbackResult",
    "TokenExchanger",
    "current_user",
    "decode_session_jwt",
    "handle_sso_callback",
    "issue_session_jwt",
    "set_exchanger_for_test",
]
