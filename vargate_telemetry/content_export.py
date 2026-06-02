# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""eDiscovery content export (TM6 T6.2 + PDF export).

Builds a downloadable export of a tenant's captured content for legal /
compliance discovery, in two formats:

  - **zip** (default): the machine-readable bundle — ``manifest.json`` +
    ``chats.json`` (decrypted) + ``chain_proof.json`` + ``README.txt``.
  - **pdf**: a human / courtroom-readable production — cover +
    integrity attestation, the chats as a transcript (Bates-numbered),
    and the chain-proof as an appendix.
  - **both**: one zip containing the JSON bundle AND the PDF.

The differentiator is the **chain-verification proof**: per record we
ship the hash-chain position (chain_seq / self_hash / prev_hash) and the
``content_hash`` (SHA-256 of plaintext), plus the GENESIS-to-tip
``verify_telemetry_chain`` result. An auditor can independently confirm
the export wasn't altered. The PDF is a *rendering* of this — the chain
remains the authoritative integrity.

Design: one ``gather_export_model`` does all the query / decrypt / redact
/ proof / chain-verify work; per-format ``render_*`` functions consume
the model. ``build_export_bundle`` is the back-compat shim
(gather → render_zip). Purged messages (T6.1) appear in the proof with
content absent. PII is masked by default (T6.3); full content is the
explicit, audit-logged ``reveal`` option. Read-only. Synchronous.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.chain import verify_telemetry_chain
from vargate_telemetry.db import session_scope
from vargate_telemetry.pii_detector import detect_and_redact
from vargate_telemetry.storage.content import retrieve_content

_log = logging.getLogger(__name__)

SOURCE_API_CONTENT = "compliance_content"
SCHEMA_VERSION = 1
_BATES_PREFIX = "VARGATE-"

Retriever = Callable[[str, str], bytes]


def _utc_stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


@dataclass
class ExportModel:
    """All export data, gathered once, rendered per-format."""

    tenant_id: str
    generated_at: datetime
    scope: dict[str, Any]
    redacted: bool
    chats: list[dict[str, Any]]
    proof_records: list[dict[str, Any]]
    verification: dict[str, Any]
    counts: dict[str, int]
    digest: str = field(default="")


# ───────────────────────────────────────────────────────────────────────────
# Gather (single source of the data for every format)
# ───────────────────────────────────────────────────────────────────────────


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


def gather_export_model(
    tenant_id: str,
    *,
    generated_at: datetime,
    subject_user_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    redact: bool = True,
    retriever: Retriever = retrieve_content,
) -> ExportModel:
    """Fetch + decrypt + (optionally) redact the in-scope content, verify
    the chain, and assemble the chats + chain-proof. Format-agnostic."""
    if not tenant_id:
        raise ValueError("tenant_id required")

    records = _fetch_records(
        tenant_id, subject_user_id=subject_user_id, start=start, end=end
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
        msg_redacted = False
        if r.content_ref and not purged:
            try:
                plaintext = retriever(tenant_id, r.content_ref).decode(
                    "utf-8", errors="replace"
                )
                if redact:
                    content, findings = detect_and_redact(plaintext)
                    msg_redacted = bool(findings)
                else:
                    content = plaintext
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
                "redacted": msg_redacted,
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

    verification_dict = {
        "valid": verification.valid,
        "record_count": verification.record_count,
        "failure_reason": verification.failure_reason,
        "failed_at_index": verification.failed_at_index,
    }
    counts = {
        "chats": len(chats),
        "messages": len(proof_records),
        "purged_messages": purged_count,
    }
    model = ExportModel(
        tenant_id=tenant_id,
        generated_at=generated_at,
        scope={
            "subject_user_id": subject_user_id,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        redacted=redact,
        chats=list(chats.values()),
        proof_records=proof_records,
        verification=verification_dict,
        counts=counts,
    )
    # A digest of the verifiable data, surfaced on the PDF cover so the
    # rendered document references the exact chain proof it was built from.
    model.digest = hashlib.sha256(
        json.dumps(
            {"verification": verification_dict, "records": proof_records},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return model


# ───────────────────────────────────────────────────────────────────────────
# Render: zip (machine-readable) — output preserved from T6.2
# ───────────────────────────────────────────────────────────────────────────

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
- export.pdf       (present in a combined export) the human-readable PDF.

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

   NOTE: this per-message check only applies to a FULL export
   (manifest.json -> "redacted": false). In a redacted export the text is
   masked, so it will not match the plaintext content_hash — the chain
   verification (step 1) and record existence still hold.

Purged messages
---------------
A message marked "purged": true had its content deleted (data-subject
request / retention / offboarding). Its chain record and content_hash
remain in the proof — proving it existed and was deleted — but the text
itself is absent (null). This is by design.
"""


def _manifest(model: ExportModel, *, files: list[str]) -> dict[str, Any]:
    return {
        "product": "Ogma by Vargate",
        "export_type": "eDiscovery content export",
        "schema_version": SCHEMA_VERSION,
        "agcs_controls": ["AG-2.3 (chain integrity)", "AG-2.8 (replayability)"],
        "tenant_id": model.tenant_id,
        "generated_at": model.generated_at.isoformat(),
        "scope": model.scope,
        "redacted": model.redacted,
        "counts": model.counts,
        "chain_verification": model.verification,
        "digest": model.digest,
        "files": files,
    }


def _json_members(model: ExportModel) -> dict[str, str]:
    """The 4 JSON/text members common to the zip + combined formats."""
    manifest = _manifest(
        model,
        files=["manifest.json", "chats.json", "chain_proof.json", "README.txt"],
    )
    proof_doc = {
        "tenant_id": model.tenant_id,
        "verification": model.verification,
        "content_hash_algorithm": "sha256(plaintext utf-8)",
        "records": model.proof_records,
    }
    return {
        "manifest.json": json.dumps(manifest, indent=2),
        "chats.json": json.dumps({"chats": model.chats}, indent=2),
        "chain_proof.json": json.dumps(proof_doc, indent=2),
        "README.txt": _README,
    }


def render_zip(model: ExportModel) -> tuple[str, bytes]:
    """The machine-readable bundle (4 files). Output identical to T6.2."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in _json_members(model).items():
            zf.writestr(name, body)
    filename = (
        f"vargate-export-{model.tenant_id}-{_utc_stamp(model.generated_at)}.zip"
    )
    return filename, buf.getvalue()


def render_combined_zip(model: ExportModel) -> tuple[str, bytes]:
    """One zip containing the JSON bundle AND the PDF production."""
    pdf_name, pdf_bytes = render_pdf(model)
    members = _json_members(model)
    # Re-stamp the manifest's files list to advertise the PDF.
    manifest = _manifest(
        model,
        files=[
            "manifest.json",
            "chats.json",
            "chain_proof.json",
            "README.txt",
            "export.pdf",
        ],
    )
    members["manifest.json"] = json.dumps(manifest, indent=2)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in members.items():
            zf.writestr(name, body)
        zf.writestr("export.pdf", pdf_bytes)
    filename = (
        f"vargate-export-{model.tenant_id}-{_utc_stamp(model.generated_at)}.zip"
    )
    return filename, buf.getvalue()


# ───────────────────────────────────────────────────────────────────────────
# Render: PDF (human / courtroom-readable production)
# ───────────────────────────────────────────────────────────────────────────


def _bates(n: int) -> str:
    return f"{_BATES_PREFIX}{n:06d}"


def render_pdf(model: ExportModel) -> tuple[str, bytes]:
    """Render the legal-facing PDF: cover + attestation, Bates-numbered
    transcript, and the chain-proof appendix. Imports ReportLab lazily so
    the rest of the module loads even if the optional dep is absent."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        PageBreak,
        PageTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.pdfgen import canvas as _canvas
    from xml.sax.saxutils import escape

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body", parent=styles["Normal"], fontSize=9, leading=12
    )
    small = ParagraphStyle(
        "Small", parent=styles["Normal"], fontSize=7, leading=9,
        textColor=colors.grey,
    )
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12)
    mono = ParagraphStyle(
        "Mono", parent=styles["Normal"], fontName="Courier", fontSize=7,
        leading=9,
    )

    tenant = escape(model.tenant_id)
    digest_short = model.digest[:16]

    class _NumberedCanvas(_canvas.Canvas):
        """Two-pass canvas for 'Page X of Y' + header/footer on every page."""

        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            self._saved: list[dict] = []

        def showPage(self) -> None:  # noqa: N802 (reportlab API)
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self._draw_furniture(total)
                super().showPage()
            super().save()

        def _draw_furniture(self, total: int) -> None:
            self.setFont("Helvetica", 7)
            self.setFillColor(colors.grey)
            self.drawString(
                0.75 * inch, 10.5 * inch,
                f"Ogma eDiscovery Export — CONFIDENTIAL — tenant {tenant}",
            )
            self.drawString(
                0.75 * inch, 0.5 * inch,
                f"Page {self._pageNumber} of {total}  ·  "
                f"generated {model.generated_at.isoformat()}  ·  "
                f"doc {digest_short}",
            )

    buf = io.BytesIO()
    frame = Frame(
        0.75 * inch, 0.75 * inch, 7.0 * inch, 9.5 * inch, id="body"
    )
    doc = BaseDocTemplate(
        buf, pagesize=letter,
        title=f"Ogma eDiscovery Export — {model.tenant_id}",
        author="Ogma by Vargate",
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])

    story: list[Any] = []

    # ── Cover ──
    story.append(Paragraph("Ogma by Vargate", h2))
    story.append(Paragraph("eDiscovery Content Export", h1))
    story.append(Paragraph("CONFIDENTIAL — produced for legal / compliance review", small))
    story.append(Spacer(1, 16))

    v = model.verification
    sc = model.scope
    cover_rows = [
        ["Tenant", tenant],
        ["Generated", escape(model.generated_at.isoformat())],
        ["Scope — subject", escape(str(sc.get("subject_user_id") or "all users"))],
        ["Scope — from", escape(str(sc.get("start") or "—"))],
        ["Scope — to", escape(str(sc.get("end") or "—"))],
        ["Redacted (PII masked)", "yes" if model.redacted else "NO — full content"],
        ["Chats / messages / purged",
         f"{model.counts['chats']} / {model.counts['messages']} / {model.counts['purged_messages']}"],
        ["Chain verification",
         f"valid={v['valid']}  (records={v['record_count']})"],
        ["Document digest (SHA-256)", digest_short + "…"],
    ]
    t = Table(cover_rows, colWidths=[2.2 * inch, 4.6 * inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 16))
    story.append(Paragraph(
        "<b>Integrity attestation.</b> Each captured record is bound into "
        "a per-tenant, append-only hash chain. A verification result of "
        "<b>valid=true</b> means the chain from GENESIS to the latest "
        "record is internally consistent — no record was inserted, "
        "removed, or modified. The per-message content hashes in the "
        "appendix let a reviewer independently confirm (on a full, "
        "unredacted export) that the text reproduced here is exactly what "
        "was recorded. This PDF is a rendering of the verifiable export "
        f"data identified by document digest {digest_short}…; the "
        "authoritative integrity record is the audit chain itself.",
        body,
    ))

    # ── Transcript (Bates-numbered) ──
    bates_of: dict[str, str] = {}
    counter = 0
    for chat in model.chats:
        story.append(PageBreak())
        title = escape(chat.get("chat_name") or chat["chat_id"])
        story.append(Paragraph(f"Chat: {title}", h2))
        meta_bits = [
            chat.get("model"), chat.get("user_email"),
            f"{len(chat['messages'])} messages",
        ]
        story.append(Paragraph(
            escape(" · ".join(b for b in meta_bits if b)), small
        ))
        if any(m["purged"] for m in chat["messages"]) and all(
            m["purged"] for m in chat["messages"]
        ):
            story.append(Paragraph(
                "<b>This chat's content was purged (deleted).</b> Records "
                "remain in the proof; content is absent.", body
            ))
        story.append(Spacer(1, 8))

        for m in chat["messages"]:
            counter += 1
            bid = _bates(counter)
            bates_of[m["message_id"]] = bid
            role = escape(str(m["role"]).upper())
            ts = escape(str(m["occurred_at"]))
            story.append(Paragraph(f"{bid}  ·  {role}  ·  {ts}", small))
            if m["purged"]:
                text = "<i>[content deleted]</i>"
            elif m["content"] is None:
                text = "<i>[content unavailable — could not be decrypted]</i>"
            else:
                text = escape(m["content"]).replace("\n", "<br/>")
            story.append(Paragraph(text, body))
            story.append(Spacer(1, 8))

    # ── Chain-proof appendix ──
    story.append(PageBreak())
    story.append(Paragraph("Appendix A — Chain-verification proof", h2))
    story.append(Paragraph(
        f"verify_telemetry_chain: valid={v['valid']}, "
        f"records={v['record_count']}"
        + (f", failure={escape(str(v['failure_reason']))}" if v["failure_reason"] else ""),
        body,
    ))
    story.append(Spacer(1, 8))

    head = ["Bates", "chain_seq", "external_id", "content_hash (SHA-256)"]
    rows = [head]
    for r in model.proof_records:
        rows.append([
            bates_of.get(r["external_id"], "—"),
            str(r["chain_seq"]),
            Paragraph(escape(r["external_id"]), mono),
            Paragraph(r["content_hash"], mono),
        ])
    proof_table = Table(
        rows, colWidths=[0.9 * inch, 0.7 * inch, 2.1 * inch, 3.3 * inch],
        repeatRows=1,
    )
    proof_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(proof_table)
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "<b>How to verify.</b> (1) Confirm valid=true above — the whole "
        "GENESIS-to-tip chain is consistent. (2) For any message, SHA-256 "
        "its UTF-8 text and compare to the content_hash for its row "
        "(matched by external_id / Bates). A match proves the text is "
        "exactly what was recorded. This per-message check applies to a "
        "full (unredacted) export; in a redacted export the masked text "
        "won't match — the chain verification + record existence still "
        "hold.",
        body,
    ))

    doc.build(story, canvasmaker=_NumberedCanvas)
    filename = (
        f"vargate-export-{model.tenant_id}-{_utc_stamp(model.generated_at)}.pdf"
    )
    return filename, buf.getvalue()


# ───────────────────────────────────────────────────────────────────────────
# Back-compat shim + format dispatch
# ───────────────────────────────────────────────────────────────────────────


def build_export_bundle(
    tenant_id: str,
    *,
    generated_at: datetime,
    subject_user_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    redact: bool = True,
    retriever: Retriever = retrieve_content,
) -> tuple[str, bytes]:
    """Back-compat: the T6.2 JSON/zip bundle (gather → render_zip)."""
    model = gather_export_model(
        tenant_id,
        generated_at=generated_at,
        subject_user_id=subject_user_id,
        start=start,
        end=end,
        redact=redact,
        retriever=retriever,
    )
    return render_zip(model)


_RENDERERS = {
    "zip": render_zip,
    "pdf": render_pdf,
    "both": render_combined_zip,
}
_MEDIA_TYPES = {
    "zip": "application/zip",
    "pdf": "application/pdf",
    "both": "application/zip",
}


def build_export(
    tenant_id: str,
    *,
    fmt: str,
    generated_at: datetime,
    subject_user_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    redact: bool = True,
    retriever: Retriever = retrieve_content,
) -> tuple[str, bytes, str]:
    """Gather once + render the requested format. Returns
    ``(filename, bytes, media_type)``. Raises ValueError on bad fmt."""
    if fmt not in _RENDERERS:
        raise ValueError(f"unsupported export format {fmt!r}")
    model = gather_export_model(
        tenant_id,
        generated_at=generated_at,
        subject_user_id=subject_user_id,
        start=start,
        end=end,
        redact=redact,
        retriever=retriever,
    )
    filename, payload = _RENDERERS[fmt](model)
    return filename, payload, _MEDIA_TYPES[fmt]
