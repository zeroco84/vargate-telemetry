# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Fixed topic taxonomy for activity categorization (TM4 Track D).

A small, curated, **versioned** set of categories. Fixed (not
emergent) so "Top topics" aggregates cleanly and stays
defensible / queryable for a compliance product. ``Other`` is the
explicit catch-all — the classifier never invents a label outside
this set, and :func:`normalize` maps anything unexpected to
``Other`` rather than fabricating a category.

Versioned: bump :data:`TAXONOMY_VERSION` when the set changes, so
old labels keep their meaning and the classify task can re-run only
what it needs. The version is stored alongside each label in
``interaction_topics``.

The descriptions guide Claude at classification time — they go
verbatim into the prompt. Keep them short and as disjoint as
possible so a one-sentence summary lands in exactly one bucket.
"""

from __future__ import annotations

TAXONOMY_VERSION = "v1"

OTHER = "Other"

# Ordered mapping: category -> one-line guidance for the classifier
# prompt. The order here is also the canonical display order for any
# UI that wants the full taxonomy (e.g. a legend).
CATEGORIES: dict[str, str] = {
    "Coding": "Writing, debugging, refactoring, or explaining code.",
    "Data & analysis": (
        "Querying, transforming, analyzing, or visualizing data."
    ),
    "Writing & content": (
        "Drafting or editing prose — docs, marketing, posts, copy."
    ),
    "Research": (
        "Gathering, comparing, or summarizing information on a topic."
    ),
    "Ops & infra": (
        "Deployment, CI/CD, cloud, infrastructure, configuration, "
        "monitoring."
    ),
    "Planning & PM": (
        "Planning, scoping, roadmaps, task breakdowns, project "
        "coordination."
    ),
    "Communication": (
        "Drafting emails, messages, or other person-to-person "
        "communication."
    ),
    "Learning & explanation": (
        "Explaining concepts or answering how/why questions to learn."
    ),
    "Review & QA": (
        "Reviewing, critiquing, or testing existing work — code "
        "review, editing, QA."
    ),
    OTHER: "Anything that does not clearly fit another category.",
}

# Frozen set for O(1) validation.
CATEGORY_SET = frozenset(CATEGORIES)

# Precomputed case-insensitive lookup for defensive normalization.
_LOWER_TO_CANONICAL = {name.lower(): name for name in CATEGORY_SET}


def is_valid(category: str) -> bool:
    """True iff ``category`` is exactly one of the taxonomy labels."""
    return category in CATEGORY_SET


def normalize(category: str | None) -> str:
    """Map a model-returned label to a known category; else ``Other``.

    The classifier is constrained to the taxonomy via structured
    output, but this guards aggregation defensively: a stray or
    case-shifted label becomes ``Other`` (never a fabricated
    category, and never an error). Empty / None → ``Other``.
    """
    if not category:
        return OTHER
    stripped = category.strip()
    if stripped in CATEGORY_SET:
        return stripped
    return _LOWER_TO_CANONICAL.get(stripped.lower(), OTHER)
