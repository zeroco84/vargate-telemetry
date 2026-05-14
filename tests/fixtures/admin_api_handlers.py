# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Shared httpx MockTransport helpers for Admin API tests (T5.5.6).

Background: T5.5.6 added an automatic ``/v1/organizations/workspaces``
call inside ``_pull_admin_for_tenant`` and
``_backfill_admin_for_tenant`` (the workspace-name sync for the Usage
view). Every existing pull-task test stub returned its usage-shaped
payload on EVERY request — so the new workspace call hit those handlers
and either burned the call-count counter or fed garbage to
``Workspace.model_validate``.

The fix at T5.5.6 launch was inline: each test's handler grew a
``if "/workspaces" in request.url.path`` short-circuit. That's
copy-pasteable but easy to miss in a new test. This module wraps the
pattern in one helper.

USAGE
=====
Wrap any existing single-purpose usage handler:

    from fixtures.admin_api_handlers import skip_workspaces

    @skip_workspaces
    def my_handler(request: httpx.Request) -> httpx.Response:
        ...  # only sees usage / messages requests

Or use the standalone routing helper inline:

    def my_handler(request):
        if is_workspaces_request(request):
            return empty_workspaces_response()
        ...  # usage logic

The empty response is the documented 200 shape for a tenant with zero
workspaces, which is the right semantic for tests that don't care
about workspace names. Tests that DO want to exercise workspace
resolution can supply their own workspaces fixture without this
helper.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

import httpx


_WORKSPACES_PATH_SUBSTRING = "/workspaces"
# TM3 Phase A4: `_sync_api_keys` adds a second admin-side fetch.
# Same problem shape as T5.5.6's workspaces — handlers that don't
# stub api_keys will either burn a call-counter or feed garbage to
# ApiKey.model_validate. Sibling helpers below.
_API_KEYS_PATH_SUBSTRING = "/api_keys"


def is_workspaces_request(request: httpx.Request) -> bool:
    """Return True if the request targets the workspaces endpoint."""
    return _WORKSPACES_PATH_SUBSTRING in request.url.path


def empty_workspaces_response() -> httpx.Response:
    """The well-formed empty envelope from Anthropic's workspaces endpoint."""
    return httpx.Response(200, json={"data": [], "has_more": False})


def is_api_keys_request(request: httpx.Request) -> bool:
    """Return True if the request targets the api_keys endpoint."""
    return _API_KEYS_PATH_SUBSTRING in request.url.path


def empty_api_keys_response() -> httpx.Response:
    """Well-formed empty envelope from Anthropic's api_keys endpoint."""
    return httpx.Response(200, json={"data": [], "has_more": False})


def skip_workspaces(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """Decorator: short-circuit workspaces + api_keys to empty envelopes.

    Despite the legacy name, this decorator now also handles api_keys
    requests (TM3 Phase A4 added a second admin-side sync). Test
    handlers wrapped with this see only usage / messages traffic;
    their call-count counters, window-tracking lists, and parameter
    assertions remain stable across both syncs.
    """

    @wraps(handler)
    def wrapped(request: httpx.Request) -> httpx.Response:
        if is_workspaces_request(request):
            return empty_workspaces_response()
        if is_api_keys_request(request):
            return empty_api_keys_response()
        return handler(request)

    return wrapped


__all__ = [
    "empty_workspaces_response",
    "empty_api_keys_response",
    "is_workspaces_request",
    "is_api_keys_request",
    "skip_workspaces",
]
