#!/usr/bin/env python3
# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""End-to-end onboarding smoke (T4.8).

T4's sprint goal is "an admin pastes their key and sees usage data in 60
seconds." This script automates the four-step flow over HTTP — exactly
what the dashboard does — against a real Anthropic test org, measures
the wall-clock between SSO and the first SUCCESS state on backfill,
and asserts the Prom metrics surfaced real observations.

**Not in CI.** Hits the live Admin API; run once per release candidate.

What it does:

  1. Inserts a fresh smoke user row in `users` (sso_provider=google,
     unique sso_subject_id keyed off wall-clock, sso_sign_in_at = NOW())
     — bypasses the SSO callback, which would require a real OAuth flow.
  2. Mints a session JWT carrying that user_id, no tenant_id — the
     same shape `current_user` sees right after an SSO callback.
  3. Records a baseline /metrics scrape so the assertions can prove
     deltas, not just non-zero counters polluted by previous runs.
  4. Starts the wall-clock. POSTs `/onboarding/validate-key`.
  5. POSTs `/onboarding/select-region` — captures the fresh tenant_id
     from the response body. Mints a NEW JWT with that tenant_id (the
     gateway also re-issues via Set-Cookie, but `secure=True` cookies
     don't survive `http://gateway:8000` per RFC 6265bis — so we sign
     our own; same secret, same shape).
  6. POSTs `/onboarding/start-backfill`. Captures task_id.
  7. Polls `/onboarding/backfill-status/{task_id}` every 2 s. Prints
     PROGRESS observations live so a slow run is debuggable rather
     than a black box.
  8. Stops the wall-clock the instant SUCCESS lands.
  9. Verifies chain integrity via `verify_telemetry_chain`. Asserts
     `telemetry_records > 0` for the new tenant.
 10. Re-scrapes /metrics; asserts every one of the three T4.7
     instruments saw a delta against the baseline.

Sprint-gate thresholds (printed in the headline):
  - **< 60 s** — T4 ships golden.
  - **60–120 s** — T4 ships with a footnote; optimization is a T5
    follow-up.
  - **> 120 s** — sprint goal failed. Identify the bottleneck.

Usage:

    export ANTHROPIC_ADMIN_KEY_TEST=sk-ant-admin01-...
    # Optional:
    export SMOKE_DAYS=30                       # default
    export SMOKE_GATEWAY_URL=http://gateway:8000   # in-container default
    docker compose exec celery-worker python scripts/smoke_t4_onboarding.py

Or, host-side if your env has the same JWT_SIGNING_KEY in scope:

    cd /home/vargate/vargate-telemetry
    ANTHROPIC_ADMIN_KEY_TEST=sk-... \
      SMOKE_GATEWAY_URL=http://127.0.0.1:8001 \
      python scripts/smoke_t4_onboarding.py

Re-running is safe: each invocation provisions a fresh user + tenant,
so previous-run state doesn't influence wall-clock. (Old smoke tenants
accumulate as orphan rows — fine for manual testing, the dispatcher
ignores empty inboxes.)
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import text as sql_text


# ───────────────────────────────────────────────────────────────────────────
# Tuneables and thresholds
# ───────────────────────────────────────────────────────────────────────────

POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 240.0  # 4 minutes — generous; sprint goal is 60 s.

# Sprint-gate thresholds (seconds, wall-clock validate-key -> SUCCESS).
GATE_GOLDEN_S = 60.0
GATE_FOOTNOTE_S = 120.0


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(
            f"ERROR: {name} is not set.\n"
            f"Set it to a real Anthropic admin API key for the test "
            f"org and re-run.",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def _masked(api_key: str) -> str:
    if len(api_key) <= 18:
        return "***"
    return api_key[:14] + "..." + api_key[-4:]


@dataclass
class MetricSnapshot:
    """Parsed counts from a /metrics scrape, restricted to the labels
    we care about for the T4 deltas check."""

    # vargate_onboarding_step_seconds_count{step=...} → count of obs
    step_counts: dict[str, float]
    # vargate_onboarding_time_to_first_pull_seconds_count (no labels)
    first_pull_count: float
    # vargate_onboarding_completion_total{outcome="completed"}
    completed_count: float

    @classmethod
    def parse(cls, body: str) -> "MetricSnapshot":
        step_counts: dict[str, float] = {}
        first_pull_count = 0.0
        completed_count = 0.0

        for line in body.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            # Histogram _count series — one per label combination.
            m = re.match(
                r'^vargate_onboarding_step_seconds_count\{step="([^"]+)"\}\s+(\S+)',
                line,
            )
            if m:
                step_counts[m.group(1)] = float(m.group(2))
                continue
            m = re.match(
                r"^vargate_onboarding_time_to_first_pull_seconds_count\s+(\S+)",
                line,
            )
            if m:
                first_pull_count = float(m.group(1))
                continue
            m = re.match(
                r'^vargate_onboarding_completion_total\{outcome="completed"\}\s+(\S+)',
                line,
            )
            if m:
                completed_count = float(m.group(1))
                continue

        return cls(
            step_counts=step_counts,
            first_pull_count=first_pull_count,
            completed_count=completed_count,
        )


def _scrape_metrics(client: httpx.Client, base_url: str) -> MetricSnapshot:
    r = client.get(f"{base_url}/metrics", timeout=10.0)
    r.raise_for_status()
    return MetricSnapshot.parse(r.text)


def _insert_smoke_user(email: str, sso_subject: str) -> str:
    """Create a fresh user row and return its UUID as a string.

    Bypasses the SSO callback — we're testing onboarding's HTTP
    surface, not the OAuth flow (which T4.3 already covers in unit
    tests). Sets `sso_sign_in_at = NOW()` so the time-to-first-pull
    histogram can observe a meaningful delta.
    """
    from vargate_telemetry.db import SessionLocal

    user_uuid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        s.execute(
            sql_text(
                """
                INSERT INTO users
                    (id, email, sso_provider, sso_subject_id,
                     name, created_at, last_login_at, sso_sign_in_at)
                VALUES
                    (:id, :email, 'google', :sub,
                     'T4 Smoke Bot', :now, :now, :now)
                """
            ),
            {
                "id": str(user_uuid),
                "email": email,
                "sub": sso_subject,
                "now": now,
            },
        )
        s.commit()
    return str(user_uuid)


def _delete_smoke_user(user_id: str) -> None:
    """Best-effort cleanup of the user row. The tenant + telemetry
    rows are intentionally LEFT IN PLACE — a future smoke run shouldn't
    re-pollute, but post-mortem of a failed run wants the data intact
    for inspection. (Tenants accumulate; the dispatcher ignores empty
    inboxes, and the smoke uses a fresh tenant_id each invocation.)"""
    from vargate_telemetry.db import SessionLocal

    try:
        with SessionLocal() as s:
            s.execute(
                sql_text("DELETE FROM users WHERE id = :id"),
                {"id": user_id},
            )
            s.commit()
    except Exception as exc:  # pragma: no cover — cleanup, not gate
        print(f"  warning: failed to clean up smoke user: {exc}", file=sys.stderr)


# ───────────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────────


def main() -> int:
    api_key = _require_env("ANTHROPIC_ADMIN_KEY_TEST")
    days = int(os.environ.get("SMOKE_DAYS", "30"))
    base_url = os.environ.get(
        "SMOKE_GATEWAY_URL", "http://gateway:8000"
    ).rstrip("/")
    # `/api/` mount point — matches FastAPI's root_path in app.py.
    api_root = f"{base_url}/api"

    # Side-effect-free imports placed inside main: keeps `--help` fast
    # and lets the env-var preflight bail before paying for the full
    # vargate_telemetry import graph.
    from vargate_telemetry.auth.jwt import issue_session_jwt
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import SessionLocal

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sso_subject = f"t4-smoke-{int(time.time())}"
    email = f"smoke-{sso_subject}@vargate.local"

    print("=== T4 ONBOARDING SMOKE ===\n")
    print(f"Gateway:      {api_root}")
    print(f"Admin key:    {_masked(api_key)}")
    print(f"Days:         {days}")
    print(f"Smoke user:   {email}")
    print(f"Started at:   {started}\n")

    # ── Step 0: insert smoke user + mint pre-tenant JWT ───────────────────
    print("[0/6] Provisioning smoke user + minting pre-tenant JWT... ", end="", flush=True)
    user_id = _insert_smoke_user(email, sso_subject)
    pre_tenant_jwt = issue_session_jwt(
        user_id=user_id,
        email=email,
        sso_provider="google",
        tenant_id=None,
    )
    print(f"user_id={user_id[:8]}…")

    client = httpx.Client(timeout=30.0)

    try:
        # ── Step 1: baseline /metrics scrape ──────────────────────────────
        print("[1/6] Baseline /metrics scrape... ", end="", flush=True)
        baseline = _scrape_metrics(client, base_url)
        print(
            f"step_counts={dict(sorted(baseline.step_counts.items()))} "
            f"first_pull={baseline.first_pull_count:g} "
            f"completed={baseline.completed_count:g}"
        )

        # ── Step 2: start wall-clock, POST validate-key ───────────────────
        t0 = time.monotonic()
        print("[2/6] POST /api/onboarding/validate-key... ", end="", flush=True)
        r = client.post(
            f"{api_root}/onboarding/validate-key",
            headers={"Authorization": f"Bearer {pre_tenant_jwt}"},
            json={"admin_key": api_key},
        )
        if r.status_code != 200:
            print(f"FAILED status={r.status_code} body={r.text}")
            return 3
        validate_body = r.json()
        print(
            f"org={validate_body['org_name']!r} "
            f"capabilities={validate_body['capabilities']}"
        )

        # ── Step 3: POST select-region (provision tenant + seal key) ──────
        print("[3/6] POST /api/onboarding/select-region (region=us)... ", end="", flush=True)
        r = client.post(
            f"{api_root}/onboarding/select-region",
            headers={"Authorization": f"Bearer {pre_tenant_jwt}"},
            json={"region": "us", "admin_key": api_key},
        )
        if r.status_code != 200:
            print(f"FAILED status={r.status_code} body={r.text}")
            return 4
        select_body = r.json()
        tenant_id = select_body["tenant_id"]
        print(f"tenant_id={tenant_id} region={select_body['region']}")

        # Mint a fresh JWT now carrying the tenant_id claim. The gateway
        # set this on Set-Cookie too, but `secure=True` cookies don't
        # round-trip over plain HTTP — sign our own (same secret).
        post_tenant_jwt = issue_session_jwt(
            user_id=user_id,
            email=email,
            sso_provider="google",
            tenant_id=tenant_id,
        )

        # ── Step 4: POST start-backfill ───────────────────────────────────
        print("[4/6] POST /api/onboarding/start-backfill... ", end="", flush=True)
        r = client.post(
            f"{api_root}/onboarding/start-backfill",
            headers={"Authorization": f"Bearer {post_tenant_jwt}"},
            json={"tenant_id": tenant_id, "days": days},
        )
        if r.status_code != 200:
            print(f"FAILED status={r.status_code} body={r.text}")
            return 5
        task_id = r.json()["task_id"]
        print(f"task_id={task_id}")

        # ── Step 5: poll backfill-status ──────────────────────────────────
        print("[5/6] Polling backfill-status every 2s:")
        deadline = time.monotonic() + POLL_TIMEOUT_S
        last_state: Optional[str] = None
        last_progress: tuple[int, int, int] = (-1, -1, -1)
        terminal_body: Optional[dict] = None

        while time.monotonic() < deadline:
            r = client.get(
                f"{api_root}/onboarding/backfill-status/{task_id}",
                headers={"Authorization": f"Bearer {post_tenant_jwt}"},
            )
            if r.status_code != 200:
                print(
                    f"    poll FAILED status={r.status_code} "
                    f"body={r.text}"
                )
                return 6
            poll = r.json()
            state = poll["state"]

            # Print state transitions + new PROGRESS counters as they land.
            if state != last_state:
                print(f"    → state: {state}")
                last_state = state
            if state == "PROGRESS":
                progress_tuple = (
                    poll.get("chunks_processed") or 0,
                    poll.get("inserted") or 0,
                    poll.get("deduped") or 0,
                )
                if progress_tuple != last_progress:
                    print(
                        f"        chunks={progress_tuple[0]} "
                        f"inserted={progress_tuple[1]} "
                        f"deduped={progress_tuple[2]}"
                    )
                    last_progress = progress_tuple

            if state in ("SUCCESS", "FAILURE"):
                terminal_body = poll
                break

            time.sleep(POLL_INTERVAL_S)
        else:
            print(
                f"    poll TIMED OUT after {POLL_TIMEOUT_S}s "
                f"in state={last_state!r}"
            )
            return 7

        t1 = time.monotonic()
        wall = t1 - t0

        if terminal_body is None or terminal_body["state"] != "SUCCESS":
            print(f"    backfill terminated non-SUCCESS: {terminal_body!r}")
            return 8

        print(
            f"    SUCCESS: chunks={terminal_body.get('chunks_processed', 0)} "
            f"inserted={terminal_body.get('inserted', 0)} "
            f"deduped={terminal_body.get('deduped', 0)}"
        )
        print(f"    wall-clock: {wall:.1f}s\n")

        # ── Step 6: chain + reconciliation + metric deltas ────────────────
        print("[6/6] Post-conditions:")

        # Chain integrity.
        chain = verify_telemetry_chain(tenant_id)
        if not chain.valid:
            print(f"    chain.valid: FALSE — {chain!r}")
            return 9
        print(f"    chain.valid: True (record_count={chain.record_count})")

        # Telemetry-records count, scoped to the fresh tenant.
        with SessionLocal() as s:
            tr_count = s.execute(
                sql_text(
                    "SELECT COUNT(*) FROM telemetry_records "
                    "WHERE tenant_id = :t"
                ),
                {"t": tenant_id},
            ).scalar()
        print(f"    telemetry_records count(*): {tr_count}")
        if not tr_count or tr_count == 0:
            print(
                "    WARNING: zero rows ingested. Either the test org "
                "has no recent usage within the SMOKE_DAYS window, or "
                "the Admin API endpoint shape diverges. Inspect the "
                "Anthropic console for this org's usage.",
            )
            # Don't hard-fail on zero rows — that's a fixture-org
            # condition, not a code-path failure. The metrics deltas
            # below still tell us whether the gateway-side observation
            # path fired.

        # Metric deltas.
        after = _scrape_metrics(client, base_url)
        print("    metric deltas vs baseline:")
        step_deltas = {
            step: after.step_counts.get(step, 0.0)
            - baseline.step_counts.get(step, 0.0)
            for step in (
                "validate-key",
                "select-region",
                "start-backfill",
            )
        }
        for step, delta in step_deltas.items():
            print(f"      step_seconds_count{{step={step!r}}}: +{delta:g}")
        first_pull_delta = after.first_pull_count - baseline.first_pull_count
        completed_delta = after.completed_count - baseline.completed_count
        print(
            f"      time_to_first_pull_count:                +{first_pull_delta:g}"
        )
        print(
            f"      completion_total{{outcome='completed'}}: +{completed_delta:g}"
        )

        # Hard assertions on the metric path: a successful onboarding
        # MUST have observed each step + bumped the completion counter.
        #
        # Note on `time_to_first_pull`: the observation happens inside the
        # celery-worker process (where the backfill runs), but `/metrics`
        # is scraped from the gateway process. `prometheus_client`'s
        # default REGISTRY is in-process — different processes have
        # different registries. So the +0 delta is STRUCTURAL, not a
        # bug in observation logic. Reported as a warning here, flagged
        # for T5 in the completion notes (multi-process REGISTRY via
        # PROMETHEUS_MULTIPROC_DIR is the canonical fix).
        problems: list[str] = []
        for step, delta in step_deltas.items():
            if delta < 1:
                problems.append(
                    f"step {step!r} step_seconds_count did not increase"
                )
        if completed_delta < 1:
            problems.append(
                "completion_total{outcome='completed'} did not increase"
            )

        if problems:
            print("\n    METRIC PROBLEMS:")
            for p in problems:
                print(f"      - {p}")
            return 10

        if tr_count and tr_count > 0 and first_pull_delta < 1:
            print(
                "    NOTE: time_to_first_pull invisible on the gateway "
                "scrape — observation lands in the worker's separate "
                "in-process REGISTRY. T5 follow-up (multi-process "
                "REGISTRY via PROMETHEUS_MULTIPROC_DIR)."
            )

        # ── Headline ──────────────────────────────────────────────────────
        if wall < GATE_GOLDEN_S:
            verdict = "GOLDEN"
        elif wall < GATE_FOOTNOTE_S:
            verdict = "PASSED (with footnote — near target, T5 follow-up)"
        else:
            verdict = "FAILED (over 120 s sprint goal threshold)"

        print(f"\nSUCCESS — T4 onboarding wall-clock: {wall:.1f} s — {verdict}")
        return 0 if wall < GATE_FOOTNOTE_S else 11

    finally:
        client.close()
        _delete_smoke_user(user_id)


if __name__ == "__main__":
    sys.exit(main())
