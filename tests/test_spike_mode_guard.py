# Copyright (C) Twinlite Services Limited
# Licensed under the Apache License, Version 2.0
# See LICENSE for the full license text.
"""TM2 Phase A3 — spike-mode startup guard tests.

The contract:

  - Spike mode unset → assertion is a no-op.
  - Spike mode set + test bypass set → assertion is a no-op (the
    happy-path for the existing TM1 spike-flow tests).
  - Spike mode set + test bypass UNSET → RuntimeError that
    explicitly calls out the misconfiguration.

We exercise these by calling :func:`assert_spike_mode_safe`
directly. Re-importing the mcp_server.main module to test the
"raised at import" path would be more end-to-end but pulls in the
whole FastAPI / FastMCP construction; not worth the noise.
"""

from __future__ import annotations

import pytest


def test_guard_noop_when_spike_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production posture: spike mode unset → guard is silent."""
    from mcp_server import config

    monkeypatch.delenv("MCP_SPIKE_MODE", raising=False)
    config.assert_spike_mode_safe()  # must not raise


def test_guard_noop_when_test_bypass_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test posture: spike + bypass both set → guard allows."""
    from mcp_server import config

    monkeypatch.setenv("MCP_SPIKE_MODE", "true")
    monkeypatch.setenv("MCP_ALLOW_SPIKE_MODE_FOR_TESTING", "1")
    config.assert_spike_mode_safe()  # must not raise


def test_guard_raises_when_spike_set_without_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production misconfiguration: spike set, bypass unset → boom."""
    from mcp_server import config

    monkeypatch.setenv("MCP_SPIKE_MODE", "true")
    monkeypatch.delenv("MCP_ALLOW_SPIKE_MODE_FOR_TESTING", raising=False)
    with pytest.raises(RuntimeError, match="MCP_SPIKE_MODE"):
        config.assert_spike_mode_safe()


def test_guard_error_message_names_the_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error needs to be self-explanatory for ops triage."""
    from mcp_server import config

    monkeypatch.setenv("MCP_SPIKE_MODE", "yes")
    monkeypatch.delenv("MCP_ALLOW_SPIKE_MODE_FOR_TESTING", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        config.assert_spike_mode_safe()

    msg = str(excinfo.value)
    assert "MCP_ALLOW_SPIKE_MODE_FOR_TESTING" in msg
    assert "TM2" in msg
    assert ".env" in msg


def test_guard_accepts_various_truthy_spike_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TM1 spike_mode_enabled() reads 1/true/yes/on; guard matches."""
    from mcp_server import config

    monkeypatch.delenv("MCP_ALLOW_SPIKE_MODE_FOR_TESTING", raising=False)
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("MCP_SPIKE_MODE", truthy)
        with pytest.raises(RuntimeError):
            config.assert_spike_mode_safe()
