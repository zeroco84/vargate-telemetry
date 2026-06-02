# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""eDiscovery content export (TM6 T6.2).

Builds a downloadable ZIP bundle of a tenant's captured content for legal
/ compliance discovery. The bundle's differentiator is a **chain-
verification proof**: alongside the decrypted chats it ships, per record,
the hash-chain position (chain_seq / self_hash / prev_hash) and the
``content_hash`` (SHA-256 of the plaintext), plus the GENESIS-to-tip
``verify_telemetry_chain`` result. An auditor can then independently:

  1. confirm the whole per-tenant chain is intact (verification.valid);
  2. SHA-256 any exported message's text and match it to that record's
     ``content_hash`` — proving the exported content is exactly what was
     recorded, not altered after the fact.

Purged messages (deleted via T6.1) are INCLUDED in the proof — their
chain record + content_hash remain ("this existed"), with the content
itself absent and flagged purged ("and was deleted"). That's the AGCS
posture eDiscovery needs.

Scoped by tenant (always, via RLS) and optionally by data subject +
date range. Read-only; mutates nothing. Synchronous for now (a very
large export is a future async/Celery follow-up).
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.chain import verify_telemetry_chain
from vargate_telemetry.db import session_scope
from vargate_telemetry.storage.content import retrieve_content

_log = logging.getLogger(__name__)

SOURCE_API_CONTENT = "compliance_content"
SCHEMA_VERSION = 1

Retriever = Callable[[str, str], bytes]


def _utc_stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _fetch_records(
    tenant_id: str,
    *,
    subject_user_id: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
) -> list[Any]:
    clauses = [
        "m.tenant_id = current_setting('app.tenant_id')",
        "m.source_api = :src",
        "m.record_type = 'chat_message'",
        "m.metadata->>'chat_id' IS NOT NULL",
    ]
    params: dict[str, Any] = {"src": SOURCE_API_CONTENT}
    if subject_user_id:
        clauses.append("m.subject_user_id::text = :uid")
        params["uid"] = subject_user_id
    if start is not None:
        clauses.append("m.occurred_at >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("m.occurred_at < :end")
        params["end"] = end

    sql = (
        "SELECT id::text AS record_id, external_id, "
        "metadata->>'chat_id' AS chat_id, metadata AS metadata, "
        "occurred_at, content_ref, content_size_bytes, content_hash, "
        "chain_seq, chain_self_hash, chain_prev_hash, "
        "subject_user_id::text AS subject_user_id "
        "FROM telemetry_records m WHERE " + " AND ".join(clauses) + " "
        "ORDER BY chain_seq"
    )
    with session_scope(tenant_id) as s:
        return s.execute(sql_text(sql), params).all()


def _fetch_purge_state(tenant_id: str) -> tuple[bool, set[str]]:
    sql = """
        SELECT
            bool_or(metadata->>'scope' = 'tenant') AS tenant_shred,
            array_remove(
                array_agg(metadata->>'deleted_external_id'), NULL
            ) AS purged_eids
        FROM telemetry_records
        WHERE tenant_id = current_setting('app.tenant_id')
          AND source_api = :src
          AND record_type = 'content_deletion'
    """
    with session_scope(tenant_id) as s:
        row = s.execute(sql_text(sql), {"src": SOURCE_API_CONTENT}).one()
    return bool(row.tenant_shred), set(row.purged_eids or [])


_README = """\
Ogma by Vargate — eDiscovery content export
===========================================

This bundle contains captured chat content for one tenant, plus a
tamper-evidence proof.

Files
-----
- manifest.json    Export metadata, scope, counts, chain-verification summary.
- chats.json       The captured chats and their messages (decrypted text).
- chain_proof.json Per-record hash-chain proof.

How to verify an exported message was not altered
--------------------------------------------------
1. Confirm chain_proof.json -> verification.valid is true. That means the
   tenant's entire append-only hash chain (from GENESIS to the latest
   record) is internally consistent — no record was inserted, removed, or
   modified.
2. For any message in chats.json, take its text, encode it as UTF-8, and
   compute the SHA-256 digest. Find the matching record in
   chain_proof.json (same external_id) and compare your digest to its
   "content_hash" (hex). A match proves the exported text is exactly the
   content that was recorded in the chain.

Purged messages
---------------
A message marked "purged": true had its content deleted (data-subject
request / retention / offboarding). Its chain record and content_hash
remain in the proof — proving it existed and was deleted — but the text
itself is absent (null). This is by design.
"""


def build_export_bundle(
    tenant_id: str,
    *,
    generated_at: datetime,
    subject_user_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    retriever: Retriever = retrieve_content,
) -> tuple[str, bytes]:
    """Build the eDiscovery ZIP. Returns ``(filename, zip_bytes)``."""
    if not tenant_id:
        raise ValueError("tenant_id required")

    records = _fetch_records(
        tenant_id,
        subject_user_id=subject_user_id,
        start=start,
        end=end,
    )
    tenant_shred, purged_eids = _fetch_purge_state(tenant_id)
    verification = verify_telemetry_chain(tenant_id)

    chats: dict[str, dict[str, Any]] = {}
    proof_records: list[dict[str, Any]] = []
    purged_count = 0

    for r in records:
        md = r.metadata or {}
        purged = tenant_shred or r.external_id in purged_eids
        content: Optional[str] = None
        if r.content_ref and not purged:
            try:
                content = retriever(tenant_id, r.content_ref).decode(
                    "utf-8", errors="replace"
                )
            except Exception:  # noqa: BLE001 — one bad blob can't fail the export
                _log.exception(
                    "content_export: decrypt failed %s/%s",
                    tenant_id,
                    r.content_ref,
                )
                content = None
        if purged:
            purged_count += 1

        chat = chats.setdefault(
            r.chat_id,
            {
                "chat_id": r.chat_id,
                "chat_name": md.get("chat_name"),
                "model": md.get("model"),
                "user_email": md.get("user_email"),
                "messages": [],
            },
        )
        chat["messages"].append(
            {
                "message_id": r.external_id,
                "role": md.get("role") or "unknown",
                "occurred_at": r.occurred_at.isoformat(),
                "content": content,
                "content_size_bytes": r.content_size_bytes,
                "purged": purged,
            }
        )
        proof_records.append(
            {
                "external_id": r.external_id,
                "chat_id": r.chat_id,
                "chain_seq": int(r.chain_seq),
                "chain_self_hash": bytes(r.chain_self_hash).hex(),
                "chain_prev_hash": bytes(r.chain_prev_hash).hex(),
                "content_hash": bytes(r.content_hash).hex(),
                "occurred_at": r.occurred_at.isoformat(),
                "purged": purged,
            }
        )

    manifest = {
        "product": "Ogma by Vargate",
        "export_type": "eDiscovery content export",
        "schema_version": SCHEMA_VERSION,
        "agcs_controls": ["AG-2.3 (chain integrity)", "AG-2.8 (replayability)"],
        "tenant_id": tenant_id,
        "generated_at": generated_at.isoformat(),
        "scope": {
            "subject_user_id": subject_user_id,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        "counts": {
            "chats": len(chats),
            "messages": len(proof_records),
            "purged_messages": purged_count,
        },
        "chain_verification": {
            "valid": verification.valid,
            "record_count": verification.record_count,
            "failure_reason": verification.failure_reason,
            "failed_at_index": verification.failed_at_index,
        },
        "files": ["manifest.json", "chats.json", "chain_proof.json", "README.txt"],
    }
    chats_doc = {"chats": list(chats.values())}
    proof_doc = {
        "tenant_id": tenant_id,
        "verification": manifest["chain_verification"],
        "content_hash_algorithm": "sha256(plaintext utf-8)",
        "records": proof_records,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("chats.json", json.dumps(chats_doc, indent=2))
        zf.writestr("chain_proof.json", json.dumps(proof_doc, indent=2))
        zf.writestr("README.txt", _README)

    filename = f"vargate-export-{tenant_id}-{_utc_stamp(generated_at)}.zip"
    _log.info(
        "content_export: tenant %s — %d chats, %d messages (%d purged), "
        "chain valid=%s",
        tenant_id,
        len(chats),
        len(proof_records),
        purged_count,
        verification.valid,
    )
    return filename, buf.getvalue()
