# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the fixed topic taxonomy (TM4 Track D / D1)."""

from __future__ import annotations

import pytest

from vargate_telemetry.topics import (
    CATEGORIES,
    CATEGORY_SET,
    OTHER,
    TAXONOMY_VERSION,
    is_valid,
    normalize,
)


def test_version_is_pinned() -> None:
    assert TAXONOMY_VERSION == "v1"


def test_other_is_a_category() -> None:
    assert OTHER == "Other"
    assert OTHER in CATEGORY_SET


def test_every_category_has_nonempty_guidance() -> None:
    """The descriptions feed the classifier prompt — none may be blank."""
    for name, guidance in CATEGORIES.items():
        assert guidance and guidance.strip(), name


def test_all_known_categories_are_valid() -> None:
    for name in CATEGORIES:
        assert is_valid(name)


def test_normalize_passes_through_known() -> None:
    assert normalize("Coding") == "Coding"
    assert normalize("Review & QA") == "Review & QA"


def test_normalize_is_case_and_whitespace_insensitive() -> None:
    assert normalize("coding") == "Coding"
    assert normalize("  CODING  ") == "Coding"
    assert normalize("data & ANALYSIS") == "Data & analysis"


@pytest.mark.parametrize("bad", ["Gardening", "", "   ", None])
def test_normalize_unknown_falls_back_to_other(bad: object) -> None:
    # Unknown / empty / None must become Other — never a fabricated
    # label and never an exception.
    assert normalize(bad) == OTHER  # type: ignore[arg-type]


def test_is_valid_rejects_unknown() -> None:
    assert not is_valid("Gardening")
    assert not is_valid("coding")  # case-sensitive exact check
