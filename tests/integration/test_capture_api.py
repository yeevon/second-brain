from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from secondbrain.capture_api import INTERNAL_TOKEN_HEADER, create_capture_api
from secondbrain.capture_models import COMPLETE, DELIVERY_FAILED, FAILED, FILED, RECEIVED
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger

from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient, FakeDiscordMessage
from tests.fakes.downstream import FakeDownstreamClient
from tests.support import event_types, ledger_rows


TOKEN = "test-internal-token"
_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def api_context(tmp_path):
    settings = SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        ledger_path=tmp_path / "runtime" / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
        delivery_retry_max_attempts=5,
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
    test_client = httpx.AsyncClient(
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
            test_client=test_client,
            downstream=downstream,
        )
    finally:
        await test_client.aclose()
        await downstream.aclose()
        ledger.close()


@pytest.mark.asyncio
async def test_health_endpoint_reports_ok_without_token(api_context):
    response = await api_context.test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "capture-service"}


@pytest.mark.asyncio
async def test_get_capture_rejects_missing_or_incorrect_token(api_context):
    capture = await ingest_normal_capture(api_context)

    missing = await api_context.test_client.get(f"/internal/captures/{capture.capture_id}")
    wrong = await api_context.test_client.get(
        f"/internal/captures/{capture.capture_id}",
        headers={INTERNAL_TOKEN_HEADER: "wrong"},
    )
    correct = await api_context.downstream.get_capture(capture.capture_id)

    assert missing.status_code == 401
    assert missing.json() == {"detail": "unauthorized"}
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "unauthorized"}
    assert correct.status_code == 200


@pytest.mark.asyncio
async def test_unknown_capture_returns_404_without_internal_details(api_context):
    response = await api_context.downstream.get_capture("SB-20260609-9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "capture not found"}
    assert "not found:" not in response.text


@pytest.mark.asyncio
async def test_unhealthy_ledger_returns_503_without_sql_error_details(api_context, monkeypatch):
    def fail_health():
        raise RuntimeError("sqlite database is locked")

    monkeypatch.setattr(api_context.service, "assert_healthy", fail_health)

    response = await api_context.test_client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "capture-service unavailable"}
    assert "sqlite" not in response.text
    assert "locked" not in response.text


@pytest.mark.asyncio
async def test_invalid_request_payload_returns_422(api_context):
    capture = await ingest_normal_capture(api_context)
    claimed = api_context.ledger.claim_due_deliveries(
        now=_NOW,
        lease_until=_NOW + timedelta(seconds=60),
        batch_size=10,
    )
    attempt = claimed[0].delivery_attempts

    # acknowledge-filed requires note_path; omitting it gives 422
    response = await api_context.test_client.post(
        f"/internal/captures/{capture.capture_id}/delivery/acknowledge-filed",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={"delivery_attempt": attempt},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/internal/captures/SB-20260609-0001/delivery/acknowledge-forwarded", {"delivery_attempt": 1}),
        ("/internal/captures/SB-20260609-0001/delivery/acknowledge-classifying", {"delivery_attempt": 1}),
        ("/internal/captures/SB-20260609-0001/delivery/renew-lease", {"delivery_attempt": 1}),
        (
            "/internal/captures/SB-20260609-0001/delivery/acknowledge-filed",
            {"delivery_attempt": 1, "note_path": "x.md"},
        ),
        (
            "/internal/captures/SB-20260609-0001/delivery/acknowledge-inbox",
            {"delivery_attempt": 1, "note_path": "x.md"},
        ),
        (
            "/internal/captures/SB-20260609-0001/delivery/schedule-retry",
            {"delivery_attempt": 1, "error_type": "TimeoutError", "reason_type": "webhook_failure"},
        ),
        (
            "/internal/captures/SB-20260609-0001/delivery/acknowledge-failed",
            {"delivery_attempt": 1},
        ),
        ("/internal/receipts/SB-20260609-0001/edit", {"content": "updated receipt"}),
    ],
)
@pytest.mark.asyncio
async def test_state_changing_routes_require_internal_token(api_context, path, payload):
    response = await api_context.test_client.post(path, json=payload)

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_legacy_retry_route_returns_404(api_context):
    """The /retry endpoint was removed in SB-107; downstream must not use it."""
    capture = await ingest_normal_capture(api_context)
    response = await api_context.test_client.post(
        f"/internal/captures/{capture.capture_id}/retry",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_downstream_callback_end_to_end_flow(api_context):
    """
    Full attempt-aware callback flow:
    insert → claim → acknowledge-forwarded → acknowledge-classifying → acknowledge-filed
    """
    capture = await ingest_normal_capture(api_context)
    cid = capture.capture_id

    # Dispatcher claims the capture
    claimed = api_context.ledger.claim_due_deliveries(
        now=_NOW,
        lease_until=_NOW + timedelta(seconds=60),
        batch_size=10,
    )
    assert len(claimed) == 1
    attempt = claimed[0].delivery_attempts

    forwarded = await api_context.downstream.acknowledge_forwarded(cid, attempt)
    classifying = await api_context.downstream.acknowledge_classifying(cid, attempt)
    filed = await api_context.downstream.acknowledge_filed(
        cid, attempt, "20_projects/halo/file.md"
    )

    assert forwarded.status_code == 200
    assert forwarded.json()["changed"] is True
    assert classifying.status_code == 200
    assert classifying.json()["changed"] is True
    assert filed.status_code == 200
    assert filed.json()["changed"] is True
    assert filed.json()["outcome"] == "changed"
    assert filed.json()["delivery_status"] == COMPLETE

    final = await api_context.downstream.get_capture(cid)
    assert final.json()["status"] == FILED
    assert final.json()["delivery_status"] == COMPLETE
    assert final.json()["derived_note_path"] == "20_projects/halo/file.md"


@pytest.mark.asyncio
async def test_attempt_aware_filed_callback_is_idempotent(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)
    cid = capture.capture_id
    payload = {"note_path": "20_projects/halo/file.md", "git_commit_hash": "abc123"}

    first = await api_context.downstream.acknowledge_filed(cid, attempt, **payload)
    second = await api_context.downstream.acknowledge_filed(cid, attempt, **payload)

    assert first.status_code == 200
    assert first.json()["changed"] is True
    assert second.status_code == 200
    assert second.json()["changed"] is False
    assert second.json()["outcome"] == "idempotent_replay"
    assert event_types(api_context.ledger, cid).count("CAPTURE_FILED") == 1


@pytest.mark.asyncio
async def test_attempt_aware_filed_callback_with_different_path_returns_409(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)
    cid = capture.capture_id

    first = await api_context.downstream.acknowledge_filed(cid, attempt, "20_projects/halo/a.md")
    second = await api_context.downstream.acknowledge_filed(cid, attempt, "20_projects/halo/b.md")

    assert first.status_code == 200
    assert second.status_code == 409
    assert api_context.service.get_capture(cid).derived_note_path == "20_projects/halo/a.md"


@pytest.mark.asyncio
async def test_attempt_aware_filed_callback_with_different_git_hash_returns_409(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)
    cid = capture.capture_id

    first = await api_context.downstream.acknowledge_filed(
        cid, attempt, "20_projects/halo/file.md", git_commit_hash="abc"
    )
    second = await api_context.downstream.acknowledge_filed(
        cid, attempt, "20_projects/halo/file.md", git_commit_hash="xyz"
    )

    assert first.status_code == 200
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_attempt_aware_inbox_callback_with_different_reason_returns_409(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)
    cid = capture.capture_id

    first = await api_context.downstream.acknowledge_inbox(
        cid, attempt, "00_inbox/file.md", reason_type="low_confidence"
    )
    second = await api_context.downstream.acknowledge_inbox(
        cid, attempt, "00_inbox/file.md", reason_type="needs_clarification"
    )

    assert first.status_code == 200
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_acknowledge_filed_before_forwarded_returns_409(api_context):
    """acknowledge-filed from PENDING_FORWARD state (not CLASSIFYING) is rejected."""
    capture = await ingest_normal_capture(api_context)

    # Claim to get a delivery_attempt number
    claimed = api_context.ledger.claim_due_deliveries(
        now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10
    )
    attempt = claimed[0].delivery_attempts
    # Do NOT acknowledge-forwarded or acknowledge-classifying — try to file directly
    response = await api_context.downstream.acknowledge_filed(
        capture.capture_id, attempt, "20_projects/halo/file.md"
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_stale_attempt_callback_returns_200_with_stale_outcome(api_context):
    """A stale (wrong attempt number) callback returns 200 with outcome=stale_attempt."""
    capture, attempt = await _capture_in_classifying_state_attempt_2(api_context)

    stale = await api_context.downstream.acknowledge_filed(
        capture.capture_id, attempt - 1, "20_projects/halo/file.md"
    )

    assert stale.status_code == 200
    assert stale.json()["changed"] is False
    assert stale.json()["outcome"] == "stale_attempt"


@pytest.mark.asyncio
async def test_schedule_retry_returns_delivery_disposition(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)

    response = await api_context.downstream.schedule_retry(
        capture.capture_id, attempt, "TimeoutError", "webhook_failure"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "retry_scheduled"
    assert body["delivery_status"] == "RETRY_WAIT"


@pytest.mark.asyncio
async def test_acknowledge_failed_marks_terminal_failure(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)

    response = await api_context.downstream.acknowledge_failed(
        capture.capture_id, attempt, reason_type="writer_failure"
    )

    assert response.status_code == 200
    assert response.json()["changed"] is True
    assert api_context.service.get_capture(capture.capture_id).delivery_status == DELIVERY_FAILED


@pytest.mark.asyncio
async def test_retry_api_rejects_free_form_error_message(api_context):
    capture = await ingest_normal_capture(api_context)
    claimed = api_context.ledger.claim_due_deliveries(
        now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10
    )
    attempt = claimed[0].delivery_attempts

    response = await api_context.downstream.schedule_retry(
        capture.capture_id,
        attempt,
        error_type="TimeoutError: POST https://webhook.example/?token=secret123",
        reason_type="webhook_failure",
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_terminal_failure_api_rejects_secret_like_reason(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)

    response = await api_context.downstream.acknowledge_failed(
        capture.capture_id,
        attempt,
        reason_type="token=secret123&url=http://example.com",
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_sensitive_rejection_fetch_returns_redacted_text_only(api_context, capsys):
    secret = "TEST_ONLY_DO_NOT_USE_123456"
    await api_context.service.handle_gateway_message(
        FakeDiscordMessage(channel=api_context.channel, message_id=1002, content=f"api_key={secret}")
    )
    capture_id = ledger_rows(api_context.ledger)[0]["capture_id"]

    response = await api_context.downstream.get_capture(capture_id)
    captured = capsys.readouterr()
    body = response.json()

    assert response.status_code == 200
    assert body["raw_text"] is None
    assert body["redacted_text"] == "api_key=[REDACTED]"
    assert secret not in response.text
    assert secret not in captured.out
    assert secret not in captured.err


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/delivery/acknowledge-forwarded", {"delivery_attempt": 1}),
        ("/delivery/acknowledge-classifying", {"delivery_attempt": 1}),
        ("/delivery/renew-lease", {"delivery_attempt": 1}),
        ("/delivery/acknowledge-filed", {"delivery_attempt": 1, "note_path": "x.md"}),
        ("/delivery/acknowledge-inbox", {"delivery_attempt": 1, "note_path": "x.md"}),
        ("/delivery/schedule-retry", {"delivery_attempt": 1, "error_type": "T", "reason_type": "r"}),
        ("/delivery/acknowledge-failed", {"delivery_attempt": 1}),
    ],
)
@pytest.mark.asyncio
async def test_unknown_capture_callback_returns_404(api_context, path, payload):
    response = await api_context.test_client.post(
        f"/internal/captures/SB-UNKNOWN-9999{path}",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json=payload,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "capture not found"


@pytest.mark.asyncio
async def test_duplicate_acknowledge_forwarded_is_idempotent(api_context):
    capture = await ingest_normal_capture(api_context)
    cid = capture.capture_id
    claimed = api_context.ledger.claim_due_deliveries(
        now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10
    )
    attempt = claimed[0].delivery_attempts

    resp1 = await api_context.downstream.acknowledge_forwarded(cid, attempt)
    resp2 = await api_context.downstream.acknowledge_forwarded(cid, attempt)

    assert resp1.status_code == 200
    assert resp1.json()["changed"] is True
    assert resp1.json()["outcome"] == "changed"
    assert resp2.status_code == 200
    assert resp2.json()["changed"] is False
    assert resp2.json()["outcome"] == "idempotent_replay"


@pytest.mark.asyncio
async def test_wrong_attempt_acknowledge_forwarded_returns_stale_outcome(api_context):
    capture = await ingest_normal_capture(api_context)
    cid = capture.capture_id
    api_context.ledger.claim_due_deliveries(
        now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10
    )
    stale = await api_context.downstream.acknowledge_forwarded(cid, 99)

    assert stale.status_code == 200
    assert stale.json()["changed"] is False
    assert stale.json()["outcome"] == "stale_attempt"
    assert stale.json()["ignored_reason"] == "stale_attempt"


@pytest.mark.asyncio
async def test_acknowledge_classifying_before_forwarded_returns_409(api_context):
    """Classifying before forwarded ack is an invalid state transition."""
    capture = await ingest_normal_capture(api_context)
    cid = capture.capture_id
    claimed = api_context.ledger.claim_due_deliveries(
        now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10
    )
    attempt = claimed[0].delivery_attempts
    # State is FORWARDING; classifying callback is premature

    response = await api_context.downstream.acknowledge_classifying(cid, attempt)

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_renew_lease_after_completion_returns_409(api_context):
    """Renewing a lease after the capture is terminal must return 409."""
    capture, attempt = await _capture_in_classifying_state(api_context)
    await api_context.downstream.acknowledge_filed(
        capture.capture_id, attempt, "20_projects/halo/file.md"
    )

    response = await api_context.downstream.renew_lease(capture.capture_id, attempt)

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_wrong_attempt_renew_lease_returns_stale_outcome(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)

    stale = await api_context.downstream.renew_lease(capture.capture_id, attempt + 1)

    assert stale.status_code == 200
    assert stale.json()["changed"] is False
    assert stale.json()["outcome"] == "stale_attempt"


@pytest.mark.asyncio
async def test_acknowledge_failed_on_already_filed_capture_returns_200(api_context):
    """Failing a capture that is already FILED returns 200 (ignored_already_terminal, not an error)."""
    capture, attempt = await _capture_in_classifying_state(api_context)
    await api_context.downstream.acknowledge_filed(
        capture.capture_id, attempt, "20_projects/halo/file.md"
    )

    response = await api_context.downstream.acknowledge_failed(capture.capture_id, attempt)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_idempotent_filed_replay_repairs_failed_receipt_delivery(api_context):
    """An idempotent FILED replay must attempt to edit the Discord receipt."""
    capture, attempt = await _capture_in_classifying_state(api_context)
    cid = capture.capture_id
    note_path = "20_projects/halo/file.md"

    # First call succeeds
    await api_context.downstream.acknowledge_filed(cid, attempt, note_path)
    edits_after_first = api_context.channel.edit_attempts

    # Second call is an idempotent replay — receipt edit must still be attempted
    await api_context.downstream.acknowledge_filed(cid, attempt, note_path)

    assert api_context.channel.edit_attempts > edits_after_first


@pytest.mark.asyncio
async def test_idempotent_inbox_replay_repairs_failed_receipt_delivery(api_context):
    """An idempotent INBOX replay must attempt to edit the Discord receipt."""
    capture, attempt = await _capture_in_classifying_state(api_context)
    cid = capture.capture_id
    note_path = "00_inbox/file.md"

    await api_context.downstream.acknowledge_inbox(cid, attempt, note_path)
    edits_after_first = api_context.channel.edit_attempts

    await api_context.downstream.acknowledge_inbox(cid, attempt, note_path)

    assert api_context.channel.edit_attempts > edits_after_first


@pytest.mark.asyncio
async def test_receipt_edit_endpoint_sends_one_replacement_when_edit_fails(api_context):
    capture = await ingest_normal_capture(api_context)
    api_context.channel.fail_receipt_edit = True

    response = await api_context.downstream.edit_receipt(capture.capture_id, "replacement receipt")
    updated = api_context.service.get_capture(capture.capture_id)

    assert response.status_code == 200
    assert api_context.channel.edit_attempts == 1
    assert len(api_context.channel.replacement_receipts) == 1
    assert updated.receipt_message_id == str(api_context.channel.replacement_receipts[0][0])
    assert event_types(api_context.ledger, capture.capture_id).count("RECEIPT_REPLACED") == 1


@pytest.mark.asyncio
async def test_receipt_edit_endpoint_returns_503_when_edit_and_replacement_fail(api_context):
    capture = await ingest_normal_capture(api_context)
    api_context.channel.fail_receipt_edit = True
    api_context.channel.fail_initial_send = True

    response = await api_context.downstream.edit_receipt(capture.capture_id, "replacement receipt")

    assert response.status_code == 503
    assert response.json() == {"detail": "receipt delivery failed"}
    assert api_context.service.get_capture(capture.capture_id).raw_text == "Review reconnect handling."
    assert "Review reconnect handling." not in response.text


@pytest.mark.asyncio
async def test_capture_only_mode_rejects_legacy_mark_forwarded_route(api_context):
    """Legacy /mark-forwarded route no longer exists."""
    capture = await ingest_normal_capture(api_context)
    response = await api_context.test_client.post(
        f"/internal/captures/{capture.capture_id}/mark-forwarded",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_capture_only_mode_rejects_legacy_mark_filed_route(api_context):
    """Legacy /mark-filed route no longer exists."""
    from tests.fakes.classifier import VALID_CLASSIFICATION
    capture = await ingest_normal_capture(api_context)
    response = await api_context.test_client.post(
        f"/internal/captures/{capture.capture_id}/mark-filed",
        headers={INTERNAL_TOKEN_HEADER: TOKEN},
        json={"note_path": "x.md", "classification": VALID_CLASSIFICATION},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# retry_attempts field presence in API responses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_get_includes_retry_attempts(api_context):
    capture = await ingest_normal_capture(api_context)

    response = await api_context.downstream.get_capture(capture.capture_id)

    body = response.json()
    assert "retry_attempts" in body
    assert body["retry_attempts"] == 0


@pytest.mark.asyncio
async def test_schedule_retry_response_includes_retry_attempts(api_context):
    capture, attempt = await _capture_in_classifying_state(api_context)

    response = await api_context.downstream.schedule_retry(
        capture.capture_id, attempt, "TimeoutError", "webhook_failure"
    )

    body = response.json()
    assert "retry_attempts" in body
    assert body["retry_attempts"] == 1


@pytest.mark.asyncio
async def test_stale_callback_response_includes_current_retry_attempts(api_context):
    """A stale-attempt callback response includes the capture's current retry_attempts."""
    capture, attempt = await _capture_in_classifying_state_attempt_2(api_context)

    stale = await api_context.downstream.acknowledge_filed(
        capture.capture_id, attempt - 1, "20_projects/halo/file.md"
    )

    body = stale.json()
    assert stale.status_code == 200
    assert body["outcome"] == "stale_attempt"
    assert "retry_attempts" in body
    # After one schedule_retry call in _capture_in_classifying_state_attempt_2, retry_attempts == 1
    assert body["retry_attempts"] == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def ingest_normal_capture(api_context):
    await api_context.service.handle_gateway_message(
        FakeDiscordMessage(
            channel=api_context.channel,
            message_id=1001,
            content="Review reconnect handling.",
        )
    )
    return api_context.service.captures_by_status(RECEIVED)[0]


async def _capture_in_classifying_state(api_context):
    """Insert, claim, forward, classifying — ready for terminal callback."""
    capture = await ingest_normal_capture(api_context)
    cid = capture.capture_id

    claimed = api_context.ledger.claim_due_deliveries(
        now=_NOW, lease_until=_NOW + timedelta(seconds=60), batch_size=10
    )
    attempt = claimed[0].delivery_attempts

    await api_context.downstream.acknowledge_forwarded(cid, attempt)
    await api_context.downstream.acknowledge_classifying(cid, attempt)

    return capture, attempt


async def _capture_in_classifying_state_attempt_2(api_context):
    """Advance to attempt 2 in CLASSIFYING state (stale-callback tests need attempt >= 2)."""
    from datetime import UTC, datetime, timedelta as td

    capture, attempt1 = await _capture_in_classifying_state(api_context)
    cid = capture.capture_id

    await api_context.downstream.schedule_retry(cid, attempt1, "connection_timeout")

    # Claim retry: use real clock + margin since schedule_retry stores real-clock next_attempt_at
    after_retry = datetime.now(UTC) + td(seconds=15)
    claimed = api_context.ledger.claim_due_deliveries(
        now=after_retry, lease_until=after_retry + td(seconds=60), batch_size=10
    )
    attempt2 = claimed[0].delivery_attempts
    assert attempt2 == 2

    await api_context.downstream.acknowledge_forwarded(cid, attempt2)
    await api_context.downstream.acknowledge_classifying(cid, attempt2)

    return capture, attempt2
