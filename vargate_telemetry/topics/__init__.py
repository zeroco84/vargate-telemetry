# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Activity categorization (TM4 Track D).

Classifies MCP interaction summaries into a fixed, versioned topic
taxonomy, powering the "Top topics" view on the user-detail page.
Classifications are derived analytics stored in ``interaction_topics``
(NOT on the immutable chain record). See ``taxonomy.py`` for the
category set and ``tasks/classify_topics.py`` for the classifier.
"""

from vargate_telemetry.topics.taxonomy import (
    CATEGORIES,
    CATEGORY_SET,
    OTHER,
    TAXONOMY_VERSION,
    is_valid,
    normalize,
)

__all__ = [
    "CATEGORIES",
    "CATEGORY_SET",
    "OTHER",
    "TAXONOMY_VERSION",
    "is_valid",
    "normalize",
]
