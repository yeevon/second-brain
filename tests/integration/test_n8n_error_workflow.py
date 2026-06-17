"""Integration tests for the SB-113 report-workflow-error endpoint."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from secondbrain.capture_api import INTERNAL_TOKEN_HEADER, create_capture_api
from secondbrain.capture_models import (
    COMPLETE,
    DELIVERY_CLASSIFYING,
    DELIVERY_FAILED,
    DELIVERY_FORWARDED,
    RECEIVED,
    RETRY_WAIT,
)
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger

from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient, FakeDiscordMessage
from tests.fakes.downstream import FakeDownstreamClient


TOKEN = "test-internal-token"
_MAX_ATTEMPTS = 3
# Far-future timestamp used to force RETRY_WAIT captures to be due for re-claim
_FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def ctx(tmp_path):
    settings = SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        ledger_path=tmp_path / "runtime" / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
        delivery_retry_max_attempts=_MAX_ATTEMPTS,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
        delivery_forward_lease_seconds=60,
        delivery_processing_lease_seconds=300,
        delivery_dispatch_interval_seconds=2,
        delivery_dispatch_batch_size=25,
        downstream_delivery_enabled=True,
        capture_processing_mode="capture-only",
        writer_service_url=None,
        writer_service_token=None,
    )
    ledger = Ledger(settings.ledger_path)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)
    app = create_capture_api(capture_service=service, internal_token=TOKEN)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    downstream = FakeDownstreamClient(app, token=TOKEN)
    try:
        yield SimpleNamespace(
            settings=settings,
            ledger=ledger,
            channel=channel,
            discord=discord,
            service=service,
            app=app,
            client=client,
            downstream=downstream,
        )
    finally:
        await client.aclose()
        await downstream.aclose()
        ledger.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _ingest(ctx, *, msg_id: int = 1001, text: str = "test note"):
    await ctx.service.handle_gateway_message(
        FakeDiscordMessage(message_id=msg_id, content=text)
    )
    return ctx.service.captures_by_status(RECEIVED)[0]


def _claim(ctx) -> list:
    """Claim due deliveries with far-future now so RETRY_WAIT captures are always due."""
    return ctx.ledger.claim_due_deliveries(
        now=_FAR_FUTURE,
        lease_until=_FAR_FUTURE + timedelta(seconds=300),
        batch_size=10,
    )


async def _advance_to_forwarding(ctx, capture_id: str) -> int:
    claimed = _claim(ctx)
    match = next(c for c in claimed if c.capture_id == capture_id)
    return match.delivery_attempts


async def _advance_to_classifying(ctx, capture_id: str) -> int:
    attempt = await _advance_to_forwarding(ctx, capture_id)
    await ctx.downstream.acknowledge_forwarded(capture_id, attempt)
    await ctx.downstream.acknowledge_classifying(capture_id, attempt)
    return attempt


# ── Authentication ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_missing_token(ctx):
    capture = await _ingest(ctx)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        json={
            "delivery_attempt": attempt,
            "disposition": "retryable",
            "error_type": "gemini_timeout",
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_wrong_token(ctx):
    capture = await _ingest(ctx)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: "wrong-token"},
        json={
            "delivery_attempt": attempt,
            "disposition": "retryable",
            "error_type": "gemini_timeout",
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_report_workflow_error_accepts_correct_token(ctx):
    capture = await _ingest(ctx)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert response.status_code == 200


# ── Validation ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_unknown_capture(ctx):
    response = await ctx.downstream.report_workflow_error(
        "SB-20260611-9999",
        delivery_attempt=1,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_invalid_disposition(ctx):
    capture = await _ingest(ctx)
    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={
            "delivery_attempt": 1,
            "disposition": "unknown_disposition",
            "error_type": "gemini_timeout",
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_terminal_error_with_retryable_disposition(ctx):
    capture = await _ingest(ctx)
    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={
            "delivery_attempt": 1,
            "disposition": "retryable",
            "error_type": "contract_violation",  # terminal error, not retryable
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_retryable_error_with_terminal_disposition(ctx):
    capture = await _ingest(ctx)
    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={
            "delivery_attempt": 1,
            "disposition": "terminal",
            "error_type": "gemini_timeout",  # retryable error
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_unknown_error_type(ctx):
    capture = await _ingest(ctx)
    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={
            "delivery_attempt": 1,
            "disposition": "retryable",
            "error_type": "some_unknown_error_not_in_taxonomy",
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_unsafe_slug_in_reason_type(ctx):
    capture = await _ingest(ctx)
    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={
            "delivery_attempt": 1,
            "disposition": "retryable",
            "error_type": "gemini_timeout",
            "reason_type": "has spaces and !@# chars",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_unknown_stage(ctx):
    capture = await _ingest(ctx)
    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={
            "delivery_attempt": 1,
            "disposition": "retryable",
            "error_type": "gemini_timeout",
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "unknown_custom_stage",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_extra_fields(ctx):
    capture = await _ingest(ctx)
    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={
            "delivery_attempt": 1,
            "disposition": "retryable",
            "error_type": "gemini_timeout",
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
            "raw_text": "should be rejected",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_workflow_error_rejects_stack_field(ctx):
    capture = await _ingest(ctx)
    response = await ctx.client.post(
        f"/internal/captures/{capture.capture_id}/delivery/report-workflow-error",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={
            "delivery_attempt": 1,
            "disposition": "retryable",
            "error_type": "gemini_timeout",
            "reason_type": "workflow_error",
            "workflow_id": "wf1",
            "workflow_name": "second_brain_intake",
            "stage": "gemini",
            "stack": "Error: something\n  at Node ...",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_workflow_error_accepts_null_execution_id(ctx):
    capture = await _ingest(ctx)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        execution_id=None,
        stage="gemini",
    )
    assert response.status_code == 200


# ── Retryable error reporting ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retryable_error_from_forwarded_state_schedules_retry(ctx):
    capture = await _ingest(ctx, msg_id=1002, text="SB-113 retryable test")
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)
    await ctx.downstream.acknowledge_forwarded(capture.capture_id, attempt)

    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["outcome"] == "retry_scheduled"
    assert data["delivery_status"] == RETRY_WAIT
    assert data["retry_attempts"] == 1
    assert data["capture_id"] == capture.capture_id


@pytest.mark.asyncio
async def test_retryable_error_from_classifying_state_schedules_retry(ctx):
    capture = await _ingest(ctx, msg_id=1003, text="SB-113 classifying retry")
    attempt = await _advance_to_classifying(ctx, capture.capture_id)

    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="classification_validation_failure",
        stage="classification_validation",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["outcome"] == "retry_scheduled"
    assert data["delivery_status"] == RETRY_WAIT


@pytest.mark.asyncio
async def test_retryable_error_from_forwarding_state_schedules_retry(ctx):
    capture = await _ingest(ctx, msg_id=1004, text="SB-113 forwarding retry")
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "retry_scheduled"
    assert response.json()["delivery_status"] == RETRY_WAIT


@pytest.mark.asyncio
async def test_retryable_error_increments_retry_count_to_one(ctx):
    capture = await _ingest(ctx, msg_id=1005)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert response.json()["retry_attempts"] == 1


@pytest.mark.asyncio
async def test_retryable_error_next_attempt_at_is_populated(ctx):
    capture = await _ingest(ctx, msg_id=1006)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    updated = ctx.ledger.get_capture(capture.capture_id)
    assert updated.next_attempt_at is not None


@pytest.mark.asyncio
async def test_retryable_error_clears_processing_lease(ctx):
    capture = await _ingest(ctx, msg_id=1007)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    updated = ctx.ledger.get_capture(capture.capture_id)
    assert updated.processing_lease_until is None


@pytest.mark.asyncio
async def test_retryable_error_raw_capture_preserved(ctx):
    capture = await _ingest(ctx, msg_id=1008, text="SB-113 preserve raw capture test")
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    updated = ctx.ledger.get_capture(capture.capture_id)
    assert updated.raw_text == "SB-113 preserve raw capture test"
    assert updated.discord_message_id is not None


@pytest.mark.asyncio
async def test_retryable_error_appends_workflow_error_event(ctx):
    capture = await _ingest(ctx, msg_id=1009)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
        workflow_name="second_brain_intake",
    )
    events = ctx.ledger.capture_events(capture.capture_id)
    event_names = [e["event_type"] for e in events]
    assert "N8N_WORKFLOW_ERROR_REPORTED" in event_names

    wf_event = next(e for e in events if e["event_type"] == "N8N_WORKFLOW_ERROR_REPORTED")
    payload = json.loads(wf_event["event_payload_json"])
    assert payload["disposition"] == "retryable"
    assert payload["error_type"] == "gemini_timeout"
    assert payload["workflow_name"] == "second_brain_intake"
    assert "raw_text" not in payload
    assert "stack" not in payload


# ── Terminal error reporting ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_error_from_forwarded_state_marks_failed(ctx):
    capture = await _ingest(ctx, msg_id=1010, text="SB-113 terminal test")
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)
    await ctx.downstream.acknowledge_forwarded(capture.capture_id, attempt)

    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["outcome"] == "terminal_failure"
    assert data["delivery_status"] == DELIVERY_FAILED


@pytest.mark.asyncio
async def test_terminal_error_from_classifying_state_marks_failed(ctx):
    capture = await _ingest(ctx, msg_id=1011, text="SB-113 terminal classifying test")
    attempt = await _advance_to_classifying(ctx, capture.capture_id)

    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="defense_in_depth_secret_detected",
        stage="workflow_unknown",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["outcome"] == "terminal_failure"
    assert data["delivery_status"] == DELIVERY_FAILED


@pytest.mark.asyncio
async def test_terminal_error_clears_processing_lease(ctx):
    capture = await _ingest(ctx, msg_id=1012)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    updated = ctx.ledger.get_capture(capture.capture_id)
    assert updated.processing_lease_until is None


@pytest.mark.asyncio
async def test_terminal_error_raw_capture_preserved(ctx):
    capture = await _ingest(ctx, msg_id=1013, text="SB-113 terminal raw preserve")
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    updated = ctx.ledger.get_capture(capture.capture_id)
    assert updated.raw_text == "SB-113 terminal raw preserve"
    assert updated.discord_message_id is not None


@pytest.mark.asyncio
async def test_terminal_error_appends_workflow_error_event(ctx):
    capture = await _ingest(ctx, msg_id=1014)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    events = ctx.ledger.capture_events(capture.capture_id)
    event_names = [e["event_type"] for e in events]
    assert "N8N_WORKFLOW_ERROR_REPORTED" in event_names


# ── Retry exhaustion ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_exhaustion_marks_terminal_failure(ctx):
    """Exhausting max_attempts via retryable errors must transition to DELIVERY_FAILED."""
    capture = await _ingest(ctx, msg_id=1015, text="SB-113 exhaustion test")
    _claim(ctx)  # attempt 1

    for _ in range(_MAX_ATTEMPTS):
        cur = ctx.ledger.get_capture(capture.capture_id)
        if cur.delivery_status == DELIVERY_FAILED:
            break
        r = await ctx.downstream.report_workflow_error(
            capture.capture_id,
            delivery_attempt=cur.delivery_attempts,
            disposition="retryable",
            error_type="gemini_timeout",
            stage="gemini",
        )
        if r.json().get("delivery_status") == RETRY_WAIT:
            _claim(ctx)  # re-claim for next attempt

    final = ctx.ledger.get_capture(capture.capture_id)
    assert final.delivery_status == DELIVERY_FAILED
    assert final.raw_text is not None


@pytest.mark.asyncio
async def test_retry_exhaustion_raw_capture_preserved(ctx):
    capture = await _ingest(ctx, msg_id=1016, text="SB-113 raw text must survive exhaustion")
    _claim(ctx)

    for _ in range(_MAX_ATTEMPTS):
        cur = ctx.ledger.get_capture(capture.capture_id)
        if cur.delivery_status == DELIVERY_FAILED:
            break
        r = await ctx.downstream.report_workflow_error(
            capture.capture_id,
            delivery_attempt=cur.delivery_attempts,
            disposition="retryable",
            error_type="gemini_timeout",
            stage="gemini",
        )
        if r.json().get("delivery_status") == RETRY_WAIT:
            _claim(ctx)

    final = ctx.ledger.get_capture(capture.capture_id)
    assert final.raw_text == "SB-113 raw text must survive exhaustion"


# ── Idempotency / replay safety ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_report_does_not_increment_retry_twice(ctx):
    capture = await _ingest(ctx, msg_id=1017)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    r1 = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert r1.json()["outcome"] == "retry_scheduled"
    retry_count_after_first = r1.json()["retry_attempts"]

    r2 = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert r2.status_code == 200
    assert r2.json()["outcome"] in ("ignored_retry_already_scheduled", "ignored_already_terminal")

    final = ctx.ledger.get_capture(capture.capture_id)
    assert final.retry_attempts == retry_count_after_first, (
        "retry_attempts must not increment on duplicate report"
    )


@pytest.mark.asyncio
async def test_duplicate_report_appends_single_event(ctx):
    capture = await _ingest(ctx, msg_id=1018)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    events = ctx.ledger.capture_events(capture.capture_id)
    wf_events = [e for e in events if e["event_type"] == "N8N_WORKFLOW_ERROR_REPORTED"]
    assert len(wf_events) == 1, f"Expected 1 N8N_WORKFLOW_ERROR_REPORTED, got {len(wf_events)}"


@pytest.mark.asyncio
async def test_stale_attempt_is_ignored(ctx):
    capture = await _ingest(ctx, msg_id=1019)
    _claim(ctx)

    # Retryable error on attempt 1 → RETRY_WAIT
    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=1,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    # Re-claim → attempt 2
    _claim(ctx)
    cur = ctx.ledger.get_capture(capture.capture_id)
    assert cur.delivery_attempts == 2

    # Stale report for attempt 1
    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=1,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "ignored_stale_attempt"


@pytest.mark.asyncio
async def test_stale_attempt_does_not_mutate_delivery_status(ctx):
    capture = await _ingest(ctx, msg_id=1020)
    _claim(ctx)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=1,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    _claim(ctx)
    state_after_second_claim = ctx.ledger.get_capture(capture.capture_id)
    assert state_after_second_claim.delivery_attempts == 2

    # Stale terminal callback must not mark the capture DELIVERY_FAILED
    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=1,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    final = ctx.ledger.get_capture(capture.capture_id)
    assert final.delivery_attempts == 2
    assert final.delivery_status != DELIVERY_FAILED


@pytest.mark.asyncio
async def test_already_complete_capture_is_ignored(ctx):
    capture = await _ingest(ctx, msg_id=1021)
    attempt = await _advance_to_classifying(ctx, capture.capture_id)

    await ctx.downstream.acknowledge_filed(
        capture.capture_id, attempt, note_path="vault/notes/test.md"
    )
    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "ignored_already_terminal"


@pytest.mark.asyncio
async def test_already_failed_capture_is_ignored(ctx):
    capture = await _ingest(ctx, msg_id=1022)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "ignored_already_terminal"


@pytest.mark.asyncio
async def test_conflicting_replay_returns_ignored(ctx):
    capture = await _ingest(ctx, msg_id=1023)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    response = await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "ignored_conflicting_replay"


@pytest.mark.asyncio
async def test_conflicting_replay_does_not_mutate_state(ctx):
    capture = await _ingest(ctx, msg_id=1024)
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="retryable",
        error_type="gemini_timeout",
        stage="gemini",
    )
    state_after_first = ctx.ledger.get_capture(capture.capture_id)
    assert state_after_first.delivery_status == RETRY_WAIT

    await ctx.downstream.report_workflow_error(
        capture.capture_id,
        delivery_attempt=attempt,
        disposition="terminal",
        error_type="contract_violation",
        stage="workflow_unknown",
    )
    state_after_conflict = ctx.ledger.get_capture(capture.capture_id)
    assert state_after_conflict.delivery_status == RETRY_WAIT
    assert state_after_conflict.retry_attempts == state_after_first.retry_attempts


# ── Concurrent error reports ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_error_reports_only_increment_retry_once(ctx):
    """Two concurrent identical reports must result in exactly one retry increment."""
    capture = await _ingest(ctx, msg_id=1025, text="SB-113 concurrent test")
    attempt = await _advance_to_forwarding(ctx, capture.capture_id)

    async def _report():
        return await ctx.downstream.report_workflow_error(
            capture.capture_id,
            delivery_attempt=attempt,
            disposition="retryable",
            error_type="gemini_timeout",
            stage="gemini",
        )

    r1, r2 = await asyncio.gather(_report(), _report())

    assert r1.status_code == 200
    assert r2.status_code == 200

    outcomes = {r1.json()["outcome"], r2.json()["outcome"]}
    assert "retry_scheduled" in outcomes, f"Expected one retry_scheduled, got {outcomes}"

    final = ctx.ledger.get_capture(capture.capture_id)
    assert final.retry_attempts == 1, f"Expected retry_attempts=1, got {final.retry_attempts}"

    events = ctx.ledger.capture_events(capture.capture_id)
    wf_events = [e for e in events if e["event_type"] == "N8N_WORKFLOW_ERROR_REPORTED"]
    assert len(wf_events) == 1, f"Expected 1 workflow error event, got {len(wf_events)}"
