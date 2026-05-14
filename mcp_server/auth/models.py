# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM1 — SQLAlchemy ORM models for MCP OAuth state.

Two tables (migration 0016_mcp_oauth_tables):

- :class:`McpOauthClient` — Dynamic Client Registration record.
  One row per Claude installation that has self-registered.
- :class:`McpAccessToken` — issued bearer + refresh token pair.
  Hashed (SHA-256 hex) on insert; raw tokens are never persisted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from vargate_telemetry.models.base import Base


class McpOauthClient(Base):
    """Per the RFC 7591 Dynamic Client Registration protocol.

    Created when Claude POSTs to ``/register`` with its metadata.
    The ``client_secret_hash`` column stores a bcrypt-style hash;
    the raw secret is returned ONCE in the DCR response and never
    persisted in plaintext.

    Soft-delete via :attr:`deleted_at`. Per the project rule
    ``never delete files / records``: revocation is by writing the
    timestamp, not by removing the row.
    """

    __tablename__ = "mcp_oauth_clients"

    client_id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    client_secret_hash: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    client_name: Mapped[str] = mapped_column(String(256), nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(
        ARRAY(String(512)), nullable=False
    )
    grant_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False
    )
    response_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False
    )
    token_endpoint_auth_method: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class McpAccessToken(Base):
    """Issued bearer token, bound to one Ogma tenant + user.

    The PK is ``token_hash`` (SHA-256 hex of the raw bearer
    string) so the validator's hot path is a single index hit:

        SELECT * FROM mcp_access_tokens
        WHERE token_hash = :h
          AND revoked_at IS NULL
          AND expires_at > now()

    ``resource`` is the RFC 8707 audience value — the validator
    rejects bearers whose ``resource`` doesn't match the MCP
    server's :func:`mcp_server.config.resource_indicator`. This
    keeps main-Ogma JWTs from being replayed at the MCP surface.
    """

    __tablename__ = "mcp_access_tokens"

    token_hash: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    client_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("mcp_oauth_clients.client_id", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_email: Mapped[str] = mapped_column(String(256), nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False)
    scopes: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String(64)), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    refresh_token_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
