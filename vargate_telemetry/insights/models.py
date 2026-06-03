# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Insights wire shapes + severity ladder (TM7).

A :class:`Card` is one analysis on the Insights page. The frontend
contract is deliberately small:

- ``severity`` is an ascending-urgency ladder
  (``idle`` < ``advisory`` < ``warning`` < ``action``); the card's
  color tier and sort weight come from it.
- ``findings_count`` drives the empty-vs-populated branch. **When it
  is ``0`` the UI shows ``empty_state`` and ignores ``headline`` /
  ``items``**; otherwise it shows the headline + items.
- ``cta`` (optional) renders as a single "go do something" link.

Each card module exposes module-level ``CARD_ID`` and ``CARD_TITLE``
plus ``build_card(tenant_id, window) -> Card``. The two
module-level constants let the aggregator build a graceful
:func:`idle_card` for a card whose ``build_card`` raised, without
having to instantiate the card first.

``escalate`` and ``idle_card`` are the only two pieces of shared
behaviour the cards lean on, so they live here next to the shapes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Ascending urgency. A card never renders below "idle"; "action" is
# the loudest tier (something is over budget / anomalous and the
# operator should act now).
Severity = Literal["idle", "advisory", "warning", "action"]


# Numeric rank for the severity ladder. Used by ``escalate`` to pick
# the louder of two severities, and available to callers that want to
# sort cards by urgency.
SEVERITY_RANK: dict = {
    "idle": 0,
    "advisory": 1,
    "warning": 2,
    "action": 3,
}


class InsightItem(BaseModel):
    """One line within a card's body.

    ``label`` is always shown; ``value`` is the right-aligned figure
    (e.g. a dollar amount or percentage rendered as a string by the
    card so the wire stays display-ready); ``detail`` is optional
    secondary text.
    """

    label: str
    detail: Optional[str] = None
    value: Optional[str] = None


class InsightCta(BaseModel):
    """A single call-to-action link rendered at the foot of a card."""

    label: str
    href: str


class Card(BaseModel):
    """One Insights card.

    ``empty_state`` is shown (in place of ``headline`` + ``items``)
    whenever ``findings_count == 0``. ``cta``, when present, renders
    as a link regardless of the finding count.
    """

    id: str
    title: str
    severity: Severity
    findings_count: int
    headline: str
    items: list[InsightItem] = Field(default_factory=list)
    empty_state: Optional[str] = None
    cta: Optional[InsightCta] = None


class InsightsResponse(BaseModel):
    """The full Insights page payload: an ordered list of cards.

    ``refreshed_at`` is timezone-aware (UTC) so the UI can render a
    "computed N minutes ago" stamp without guessing the zone.
    """

    window: str
    refreshed_at: datetime
    cards: list[Card]


def escalate(a: Severity, b: Severity) -> Severity:
    """Return the higher-rank (louder) of two severities.

    Ties return ``a``. Used by cards that accumulate several findings
    and want the card-level severity to reflect the worst one.
    """
    return a if SEVERITY_RANK[a] >= SEVERITY_RANK[b] else b


def idle_card(
    id: str,
    title: str,
    *,
    empty_state: str,
    headline: str = "",
) -> Card:
    """Build an idle, finding-free card.

    Severity ``idle``, ``findings_count`` 0, empty ``items``, no
    ``cta`` — the UI renders ``empty_state``. Used both for cards
    that genuinely found nothing and (by the aggregator) for a card
    whose ``build_card`` raised, so one failure never blanks the page.
    """
    return Card(
        id=id,
        title=title,
        severity="idle",
        findings_count=0,
        headline=headline,
        items=[],
        empty_state=empty_state,
        cta=None,
    )
