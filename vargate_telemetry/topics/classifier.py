# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""LLM topic classifier (TM4 Track D / D2).

Classifies a batch of MCP interaction summaries into the fixed taxonomy
(``topics/taxonomy.py``) with Claude Haiku via the Anthropic SDK. Off
the hot path — invoked by the ``classify_topics`` Celery task.

Design
======
- **Model:** ``claude-haiku-4-5``. Ample for one-sentence classification;
  Haiku was the founder's choice for cost (Track D scoping).
- **Structured output:** ``messages.create`` with
  ``output_config.format`` = a JSON schema whose ``category`` field is an
  ``enum`` built FROM the taxonomy — so every label the model returns is
  a valid category by construction. No free-text parsing.
- **Batching:** up to ``BATCH_SIZE`` summaries per request amortizes the
  per-call overhead. This is the real cost lever.
- **Prompt caching:** ``cache_control`` is set on the system block, but
  Haiku's minimum cacheable prefix is 4096 tokens and the taxonomy
  prompt is far smaller — so caching does NOT engage today; it's
  defensive/future-proofing only. Cost is dominated by the (tiny)
  per-call token count, which batching already keeps negligible.
- **Never fake a label:** a returned category is always a taxonomy value
  (incl. ``Other``, the honest catch-all). If the API call or parse
  FAILS, or the model omits a summary's index, that summary is returned
  as ``None`` (unclassified) — the caller writes no row and it's
  reprocessed on the next run. We never write a guessed label.
- The Anthropic SDK is imported lazily inside ``_build_client`` so this
  module imports cleanly without the package present (tests mock the
  seam) — same posture as ``notify/email.py`` with boto3.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from vargate_telemetry.topics.taxonomy import (
    CATEGORIES,
    TAXONOMY_VERSION,
    normalize,
)

_log = logging.getLogger(__name__)

# Haiku — see module docstring. Bare ID string per the anthropic SDK.
CLASSIFIER_MODEL = "claude-haiku-4-5"

# Summaries per request. ~20 keeps the response well under max_tokens and
# amortizes the (uncacheable, but tiny) system prompt across the batch.
BATCH_SIZE = 20

# Canonical category names, sourced from the taxonomy (single source of
# truth) and frozen into the response schema's enum.
_CATEGORY_NAMES: list[str] = list(CATEGORIES)


class ClassifierNotConfigured(RuntimeError):
    """``ANTHROPIC_API_KEY`` is unset — classification can't run."""


class ClassificationError(RuntimeError):
    """The API call or response parse failed for a batch.

    The caller leaves the batch's records unclassified (no rows) and lets
    the next run retry — never writes a guessed label.
    """


def _build_system_prompt() -> str:
    """Build the classifier system prompt from the taxonomy.

    Deterministic (sorted-stable insertion order of CATEGORIES) so the
    prefix is byte-stable across calls — the precondition caching would
    need, even though the prompt is below Haiku's cache threshold today.
    """
    lines = [
        "You classify short summaries of Claude interactions into exactly "
        "one topic category. A summary describes what happened in one "
        "Claude turn; classify it by its primary activity.",
        "",
        "Categories:",
    ]
    for name, guidance in CATEGORIES.items():
        lines.append(f"- {name}: {guidance}")
    lines += [
        "",
        "Rules:",
        "- Choose the single best-fitting category for each summary.",
        '- Use "Other" only when no specific category clearly applies.',
        "- Return one classification per input summary, addressed by its "
        "index.",
    ]
    return "\n".join(lines)


SYSTEM_PROMPT = _build_system_prompt()

# Response schema: a list of {index, category} with category constrained
# to the taxonomy enum. additionalProperties:false is required by the
# structured-output contract.
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "category": {"type": "string", "enum": _CATEGORY_NAMES},
                },
                "required": ["index", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}


def _build_client() -> Any:
    """Construct the Anthropic client. Lazy-imports the SDK.

    Raises :class:`ClassifierNotConfigured` BEFORE importing the SDK when
    the key is unset, so a misconfigured worker fails cleanly (and so
    tests that exercise the not-configured path don't need the package).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ClassifierNotConfigured(
            "ANTHROPIC_API_KEY is not set; cannot classify. Set the "
            "Vargate-owned Anthropic key (see docs/ops)."
        )
    import anthropic  # lazy — see module docstring

    return anthropic.Anthropic()


def _build_user_content(summaries: list[str]) -> str:
    """Number the summaries so the model can address each by index."""
    lines = [
        f"Classify these {len(summaries)} interaction summaries. Return "
        "exactly one classification per summary, addressed by index.",
        "",
    ]
    for i, summary in enumerate(summaries):
        # Summaries are already <=500 chars by the MCP contract; guard
        # anyway so one pathological row can't blow the request size.
        lines.append(f"[{i}] {summary[:500]}")
    return "\n".join(lines)


def classify_summaries(summaries: list[str]) -> list[Optional[str]]:
    """Classify a batch of summaries into taxonomy categories.

    Returns a list the SAME length as ``summaries``; element ``i`` is the
    category for ``summaries[i]``, or ``None`` if the model didn't return
    a classification for that index (left unclassified, never guessed).

    Raises
    ------
    ClassifierNotConfigured
        ``ANTHROPIC_API_KEY`` unset.
    ClassificationError
        The API call failed or the response couldn't be parsed — the
        caller leaves the whole batch unclassified and retries later.
    """
    if not summaries:
        return []
    if len(summaries) > BATCH_SIZE:
        raise ValueError(
            f"batch of {len(summaries)} exceeds BATCH_SIZE={BATCH_SIZE}"
        )

    client = _build_client()
    try:
        response = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {"role": "user", "content": _build_user_content(summaries)}
            ],
            output_config={
                "format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}
            },
        )
    except ClassifierNotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001 — SDK exception tree is wide
        # Transport/SDK error (after the SDK's own retries). Surface so
        # the caller leaves the batch unclassified and retries next run.
        raise ClassificationError(
            f"Anthropic classify call failed: {exc!s}"
        ) from exc

    # output_config.format guarantees the first text block is valid JSON
    # matching the schema.
    try:
        text = next(b.text for b in response.content if b.type == "text")
        parsed = json.loads(text)
        items = parsed["classifications"]
    except (StopIteration, KeyError, ValueError, TypeError) as exc:
        raise ClassificationError(
            f"could not parse classifier response: {exc!s}"
        ) from exc

    # Map index -> category. normalize() is a defense-in-depth guard: the
    # enum schema already constrains category, but a stray value becomes
    # Other rather than poisoning aggregation. Out-of-range or missing
    # indices are ignored (those summaries stay unclassified).
    by_index: dict[int, str] = {}
    for item in items:
        try:
            idx = int(item["index"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= idx < len(summaries):
            by_index[idx] = normalize(item.get("category"))

    return [by_index.get(i) for i in range(len(summaries))]
