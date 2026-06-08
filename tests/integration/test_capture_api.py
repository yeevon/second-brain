from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from secondbrain.capture_api import INTERNAL_TOKEN_HEADER, create_capture_api
from secondbrain.capture_models import CLASSIFYING, FAILED, FILED, FORWARDED, RECEIVED
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger

from tests.fakes.classifier import VALID_CLASSIFICATION
from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient, FakeDiscordMessage, FakeReceiptMessage
from tests.fakes.downstream import FakeDownstreamClient
from tests.support import event_types, ledger_rows


TOKEN = "test-internal-token"


@pytest_asyncio.fixture
async def api_context(tmp_path):
    settings = SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        ledger_path=tmp_path / "runtime" / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
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


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/internal/captures/SB-20260608-0001/mark-forwarded", None),
        ("/internal/captures/SB-20260608-0001/mark-classifying", None),
        ("/internal/captures/SB-20260608-0001/mark-filed", {"note_path": "x.md", "classification": VALID_CLASSIFICATION}),
        ("/internal/captures/SB-20260608-0001/mark-inbox", {"note_path": "x.md", "classification": {**VALID_CLASSIFICATION, "folder": "inbox", "project": None}}),
        ("/internal/captures/SB-20260608-0001/mark-failed", {"reason": "failed safely"}),
        ("/internal/captures/SB-20260608-0001/retry", None),
        ("/internal/receipts/SB-20260608-0001/edit", {"content": "updated receipt"}),
    ],
)
@pytest.mark.asyncio
async def test_state_changing_routes_require_internal_token(api_context, path, payload):
    response = await api_context.test_client.post(path, json=payload)

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_downstream_can_fetch_transition_and_edit_receipt_without_sqlite_access(api_context):
    capture = await ingest_normal_capture(api_context)

    fetched = await api_context.downstream.get_capture(capture.capture_id)
    forwarded = await api_context.downstream.mark_forwarded(capture.capture_id)
    classifying = await api_context.downstream.mark_classifying(capture.capture_id)
    filed = await api_context.downstream.mark_filed(
        capture.capture_id,
        {"note_path": "20_projects/halo/file.md", "classification": VALID_CLASSIFICATION},
    )
    edited = await api_context.downstream.edit_receipt(capture.capture_id, "filed through api")
    final = await api_context.downstream.get_capture(capture.capture_id)

    assert fetched.status_code == 200
    assert fetched.json()["raw_text"] == "Review reconnect handling."
    assert forwarded.json()["status"] == FORWARDED
    assert forwarded.json()["changed"] is True
    assert classifying.json()["status"] == CLASSIFYING
    assert filed.json()["status"] == FILED
    assert edited.status_code == 200
    assert api_context.channel.edited_receipts == [(9001, "filed through api")]
    assert final.json()["status"] == FILED
    assert final.json()["derived_note_path"] == "20_projects/halo/file.md"


@pytest.mark.asyncio
async def test_repeated_transition_callback_does_not_duplicate_event(api_context):
    capture = await ingest_normal_capture(api_context)

    first = await api_context.downstream.mark_forwarded(capture.capture_id)
    second = await api_context.downstream.mark_forwarded(capture.capture_id)

    assert first.json()["changed"] is True
    assert second.json()["changed"] is False
    assert event_types(api_context.ledger, capture.capture_id).count("CAPTURE_FORWARDED") == 1

    await api_context.downstream.mark_classifying(capture.capture_id)
    filed_payload = {"note_path": "20_projects/halo/file.md", "classification": VALID_CLASSIFICATION}
    filed_first = await api_context.downstream.mark_filed(capture.capture_id, filed_payload)
    filed_second = await api_context.downstream.mark_filed(capture.capture_id, filed_payload)

    assert filed_first.json()["changed"] is True
    assert filed_second.json()["changed"] is False
    assert event_types(api_context.ledger, capture.capture_id).count("CAPTURE_FILED") == 1


@pytest.mark.asyncio
async def test_conflicting_filed_replay_returns_conflict(api_context):
    capture = await ingest_normal_capture(api_context)
    await api_context.downstream.mark_classifying(capture.capture_id)
    first = await api_context.downstream.mark_filed(
        capture.capture_id,
        {"note_path": "20_projects/halo/a.md", "classification": VALID_CLASSIFICATION},
    )
    second = await api_context.downstream.mark_filed(
        capture.capture_id,
        {"note_path": "20_projects/halo/b.md", "classification": VALID_CLASSIFICATION},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert api_context.service.get_capture(capture.capture_id).derived_note_path == "20_projects/halo/a.md"


@pytest.mark.asyncio
async def test_invalid_state_transition_returns_conflict(api_context):
    capture = await ingest_normal_capture(api_context)

    response = await api_context.downstream.mark_filed(
        capture.capture_id,
        {"note_path": "20_projects/halo/file.md", "classification": VALID_CLASSIFICATION},
    )

    assert response.status_code == 409
    assert api_context.service.get_capture(capture.capture_id).status == RECEIVED
    assert "CAPTURE_FILED" not in event_types(api_context.ledger, capture.capture_id)


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


@pytest.mark.asyncio
async def test_failed_capture_retry_returns_to_received(api_context):
    capture = await ingest_normal_capture(api_context)

    failed = await api_context.downstream.mark_failed(capture.capture_id, "temporary failure")
    retried = await api_context.downstream.retry(capture.capture_id)

    assert failed.json()["status"] == FAILED
    assert retried.json()["status"] == RECEIVED
    assert event_types(api_context.ledger, capture.capture_id).count("CAPTURE_RETRIED") == 1


@pytest.mark.asyncio
async def test_retry_rejects_non_failed_capture(api_context):
    capture = await ingest_normal_capture(api_context)
    await api_context.downstream.mark_classifying(capture.capture_id)
    await api_context.downstream.mark_filed(
        capture.capture_id,
        {"note_path": "20_projects/halo/file.md", "classification": VALID_CLASSIFICATION},
    )

    response = await api_context.downstream.retry(capture.capture_id)

    assert response.status_code == 409


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


async def ingest_normal_capture(api_context):
    await api_context.service.handle_gateway_message(
        FakeDiscordMessage(
            channel=api_context.channel,
            message_id=1001,
            content="Review reconnect handling.",
        )
    )
    return api_context.service.captures_by_status(RECEIVED)[0]
