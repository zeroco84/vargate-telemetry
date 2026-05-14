# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""Celery task modules for the MCP server.

Side-effect: importing this package registers all task modules so the
worker (which includes `mcp_server.tasks` via celery_app's include=)
discovers them.
"""
from mcp_server.tasks import persist_event  # noqa: F401
from mcp_server.tasks import refresh_bridge_jwk  # noqa: F401
