# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the Haiku topic classifier (TM4 Track D / D2).

These mock the ``_build_client`` seam, so they exercise the
prompt-building, structured-output parsing, and never-fake-a-label
behavior WITHOUT the anthropic SDK installed and without any network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from vargate_telemetry.topics import classifier
from vargate_telemetry.topics.classifier import (
    BATCH_SIZE,
    ClassificationError,
    ClassifierNotConfigured,
    classify_summaries,
)


def _fake_response(classifications: list[dict]) -> SimpleNamespace:
    """A minimal stand-in for an anthropic Message: .content[*].type/.text."""
    text = json.dumps({"classifications": classifications})
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class _FakeClient:
    def __init__(
        self,
        response: Any = None,
        raise_exc: Optional[Exception] = None,
        capture: Optional[dict] = None,
    ) -> None:
        self._response = response
        self._raise = raise_exc
        self._capture = capture
        self.messages = self  # client.messages.create -> self.create

    def create(self, **kwargs: Any) -> Any:
        if self._capture is not None:
            self._capture.update(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._response


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch):
    def _install(
        response: Any = None,
        raise_exc: Optional[Exception] = None,
        capture: Optional[dict] = None,
    ) -> _FakeClient:
        client = _FakeClient(
            response=response, raise_exc=raise_exc, capture=capture
        )
        monkeypatch.setattr(classifier, "_build_client", lambda: client)
        return client

    return _install


def test_maps_each_summary_to_its_category(patch_client) -> None:
    patch_client(
        _fake_response(
            [
                {"index": 0, "category": "Coding"},
                {"index": 1, "category": "Writing & content"},
            ]
        )
    )
    out = classify_summaries(["fixed a bug", "wrote a blog post"])
    assert out == ["Coding", "Writing & content"]


def test_missing_index_left_unclassified_not_guessed(patch_client) -> None:
    # Model returns only index 0 → index 1 stays None, never fabricated.
    patch_client(_fake_response([{"index": 0, "category": "Coding"}]))
    assert classify_summaries(["a", "b"]) == ["Coding", None]


def test_stray_category_normalizes_to_other(patch_client) -> None:
    patch_client(_fake_response([{"index": 0, "category": "Gardening"}]))
    assert classify_summaries(["plant tomatoes"]) == ["Other"]


def test_out_of_range_index_ignored(patch_client) -> None:
    patch_client(
        _fake_response(
            [
                {"index": 0, "category": "Coding"},
                {"index": 9, "category": "Research"},  # no such input
            ]
        )
    )
    assert classify_summaries(["x"]) == ["Coding"]


def test_api_error_raises_classification_error(patch_client) -> None:
    patch_client(raise_exc=RuntimeError("boom"))
    with pytest.raises(ClassificationError):
        classify_summaries(["x"])


def test_unparseable_response_raises(patch_client) -> None:
    patch_client(
        SimpleNamespace(content=[SimpleNamespace(type="text", text="nope")])
    )
    with pytest.raises(ClassificationError):
        classify_summaries(["x"])


def test_not_configured_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No _build_client patch → the real one runs → no key → raises
    # BEFORE importing the SDK (so this passes without anthropic present).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ClassifierNotConfigured):
        classify_summaries(["x"])


def test_empty_batch_short_circuits(patch_client) -> None:
    assert classify_summaries([]) == []


def test_batch_over_limit_rejected(patch_client) -> None:
    with pytest.raises(ValueError):
        classify_summaries(["x"] * (BATCH_SIZE + 1))


def test_request_carries_taxonomy_enum_and_numbered_summaries(
    patch_client,
) -> None:
    cap: dict = {}
    patch_client(
        _fake_response([{"index": 0, "category": "Coding"}]), capture=cap
    )
    classify_summaries(["fixed a bug"])

    assert cap["model"] == "claude-haiku-4-5"
    # System prompt carries the taxonomy + a (defensive) cache breakpoint.
    sys_block = cap["system"][0]
    assert sys_block["cache_control"] == {"type": "ephemeral"}
    assert "Coding" in sys_block["text"]
    # Structured output constrains category to the taxonomy enum.
    schema = cap["output_config"]["format"]["schema"]
    enum = schema["properties"]["classifications"]["items"]["properties"][
        "category"
    ]["enum"]
    assert "Coding" in enum and "Other" in enum
    # Summaries are numbered so the model can address each by index.
    assert "[0] fixed a bug" in cap["messages"][0]["content"]
