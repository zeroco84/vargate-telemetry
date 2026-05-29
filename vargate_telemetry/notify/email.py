# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""AWS SES wrapper for outbound transactional email (TM3 Phase B4).

One public function: :func:`send_email`. Used today by the budget-
alert path; future channels (digest digests, expiring-credential
warnings) will reuse the same entry point.

Configuration
=============

Read from environment:

- ``AWS_SES_REGION`` — default ``eu-west-1`` (matches the EU tenant
  primary region; SES is regional and the sending identity lives in
  the same region as the verified domain).
- ``OGMA_ALERT_FROM_ADDRESS`` — the verified sender, e.g.
  ``alerts@vargate.ai``. SES will reject ``SendEmail`` with
  ``MessageRejected`` if this identity is not verified in the
  configured region.
- ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` — boto3 picks
  these up automatically via its default credential chain; we do
  NOT read them here so an EC2 / IAM-role flow works without a
  code change.

In tests
========

``send_email`` is the seam. Tests monkey-patch ``_build_ses_client``
(or the cached client via ``_reset_client_for_test``) to a Mock
whose ``send_email`` returns a synthetic ``{"MessageId": "stub"}``.
This avoids any network round-trip + any SES sandbox interaction.

If ``OGMA_ALERT_FROM_ADDRESS`` is not set, :func:`send_email`
raises :class:`SesNotConfigured` — the alert evaluator catches and
logs, so a misconfigured prod box surfaces in the worker log but
doesn't crash the beat loop. Customers without alert recipients
configured on a budget never enter this path in the first place.
"""

from __future__ import annotations

import logging
import os
import threading
from email.utils import formataddr
from typing import Any, Optional


_log = logging.getLogger(__name__)

# Display name on the From header. Recipients see "Vargate.ai" rather
# than the bare sending address (which carries the `mail.` MAIL-FROM
# subdomain for SPF alignment — see docs/ops/integrations/aws-ses.md).
# formataddr() RFC-quotes the dot in the name automatically.
_FROM_DISPLAY_NAME = "Vargate.ai"


class EmailDeliveryError(RuntimeError):
    """SES returned an error response or boto3 raised mid-send."""


class SesNotConfigured(RuntimeError):
    """``OGMA_ALERT_FROM_ADDRESS`` (or another required env) is unset."""


_client: Optional[Any] = None
_client_lock = threading.Lock()


def _build_ses_client() -> Any:
    """Build the boto3 SES client.

    Lazy — first call constructs; subsequent calls reuse the cached
    instance. The MinIO storage module's pattern (see
    ``storage/object_store.py``).
    """
    import boto3
    from botocore.config import Config as BotoConfig

    region = os.environ.get("AWS_SES_REGION", "eu-west-1")
    # Tighten timeouts so a hung SES doesn't block the celery beat
    # loop indefinitely. Five-second connect, fifteen-second read,
    # one retry — same posture as the S3 client.
    config = BotoConfig(
        connect_timeout=5,
        read_timeout=15,
        retries={"max_attempts": 1},
    )
    return boto3.client("ses", region_name=region, config=config)


def _client_singleton() -> Any:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_ses_client()
    return _client


def _reset_client_for_test() -> None:
    """Drop the cached client so the next call rebuilds it.

    Tests that mutate the env or swap in a mocked boto3 module
    need this; the global cache otherwise pins the first-seen
    config for the entire test session.
    """
    global _client
    with _client_lock:
        _client = None


def send_email(
    *,
    to: list[str],
    subject: str,
    html_body: str,
    text_body: str,
) -> str:
    """Send a single email via SES.

    Parameters
    ----------
    to:
        Recipient email addresses. Non-empty.
    subject:
        Email subject line. Plaintext, no HTML.
    html_body, text_body:
        Body in both shapes. SES requires HTML and Text alongside
        each other when sending multipart; we always supply both so
        text-only mail clients (some compliance inboxes) render
        cleanly.

    Returns
    -------
    The SES ``MessageId`` string. Useful for tracing in CloudWatch.

    Raises
    ------
    SesNotConfigured if the sender identity env is unset.
    EmailDeliveryError if SES rejects or boto3 raises.
    """
    if not to:
        raise ValueError("send_email: 'to' must be a non-empty list")

    sender = os.environ.get("OGMA_ALERT_FROM_ADDRESS")
    if not sender:
        raise SesNotConfigured(
            "OGMA_ALERT_FROM_ADDRESS is not set; cannot send email. "
            "Verify the sender identity in AWS SES and set the env var "
            "(see docs/ops/integrations/aws-ses.md)."
        )

    # Build the From with a display name so recipients see
    # "Vargate.ai", not the raw mail.ogma.vargate.ai address.
    # formataddr handles RFC 5322 quoting of the dotted name.
    source = formataddr((_FROM_DISPLAY_NAME, sender))

    client = _client_singleton()
    try:
        response = client.send_email(
            Source=source,
            Destination={"ToAddresses": to},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 — boto3's exception tree is wide
        raise EmailDeliveryError(
            f"SES send_email raised: {exc!s}"
        ) from exc

    msg_id = response.get("MessageId")
    if not msg_id:
        raise EmailDeliveryError(
            f"SES response missing MessageId: {response!r}"
        )

    _log.info(
        "send_email: SES accepted MessageId=%s, to=%s, subject=%s",
        msg_id,
        to,
        subject,
    )
    return str(msg_id)
