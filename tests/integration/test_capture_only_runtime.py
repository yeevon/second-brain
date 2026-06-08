import asyncio
from types import SimpleNamespace

import httpx
import pytest

from secondbrain.app import run_capture_only_runtime
from secondbrain.capture_api import create_capture_api
from secondbrain.capture_models import RECEIVED, REJECTED_SENSITIVE
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger

from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient, FakeDiscordMessage


def make_settings(tmp_path):
    return SimpleNamespace(
        capture_processing_mode="capture-only",
        discord_bot_token="discord-token",
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        periodic_reconcile_limit=100,
        periodic_reconcile_interval_seconds=60,
        ledger_path=tmp_path / "runtime" / "ledger.sqlite3",
        capture_service_internal_token="x" * 32,
        capture_api_host="127.0.0.1",
        capture_api_port=8000,
    )


def make_capture_only_context(tmp_path, *, history_messages=None):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    channel = FakeDiscordChannel(history_messages or [])
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=None, receipt_client=discord)
    return SimpleNamespace(
        settings=settings,
        ledger=ledger,
        channel=channel,
        discord=discord,
        service=service,
    )


@pytest.mark.asyncio
async def test_capture_only_mode_persists_normal_message_as_received(tmp_path):
    context = make_capture_only_context(tmp_path)

    await context.service.handle_gateway_message(
        FakeDiscordMessage(channel=context.channel, content="Capture this from EC2.")
    )

    captures = context.service.captures_by_status(RECEIVED)
    assert len(captures) == 1
    assert captures[0].raw_text == "Capture this from EC2."


@pytest.mark.asyncio
async def test_capture_only_mode_sends_durable_capture_receipt(tmp_path):
    context = make_capture_only_context(tmp_path)

    await context.service.handle_gateway_message(FakeDiscordMessage(channel=context.channel))

    capture = context.service.captures_by_status(RECEIVED)[0]
    assert context.channel.sent_receipts == [
        (
            9001,
            f"⏳ {capture.capture_id} received.\n"
            "Your note is safely captured.\n"
            "Downstream filing is not enabled yet.",
        )
    ]


@pytest.mark.asyncio
async def test_capture_only_mode_does_not_start_classifier_worker(tmp_path, monkeypatch):
    monkeypatch.setattr("secondbrain.app.run_capture_worker", _fail_if_called)
    await _run_fake_capture_only_runtime(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_capture_only_mode_does_not_construct_vault_writer(tmp_path, monkeypatch):
    monkeypatch.setattr("secondbrain.app.VaultWriter", _fail_if_called)
    await _run_fake_capture_only_runtime(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_capture_only_mode_rejects_sensitive_message_before_plaintext_persistence(tmp_path):
    context = make_capture_only_context(tmp_path)
    secret = "TEST_ONLY_SB104_DO_NOT_USE_123456"

    await context.service.handle_gateway_message(
        FakeDiscordMessage(channel=context.channel, content=f"api_key={secret}")
    )

    rejected = context.service.captures_by_status(REJECTED_SENSITIVE)[0]
    dump = context.ledger._runtime.read(lambda conn: "\n".join(conn.iterdump()))
    assert rejected.raw_text is None
    assert rejected.redacted_text == "api_key=[REDACTED]"
    assert secret not in dump


@pytest.mark.asyncio
async def test_capture_only_mode_startup_reconciliation_recovers_missed_message_once(tmp_path):
    missed = FakeDiscordMessage(message_id=1001, content="Missed during reboot.")
    context = make_capture_only_context(tmp_path, history_messages=[missed])

    first = await context.service.startup_reconcile(context.discord)
    second = await context.service.startup_reconcile(context.discord)

    captures = context.service.captures_by_status(RECEIVED)
    assert first.handled == 1
    assert second.seen == 0
    assert len(captures) == 1
    assert captures[0].raw_text == "Missed during reboot."


@pytest.mark.asyncio
async def test_capture_only_mode_duplicate_gateway_event_does_not_duplicate_row_or_receipt(tmp_path):
    context = make_capture_only_context(tmp_path)
    message = FakeDiscordMessage(message_id=1001, channel=context.channel)

    await context.service.handle_gateway_message(message)
    await context.service.handle_gateway_message(message)

    assert context.service.total_captures() == 1
    assert len(context.channel.sent_receipts) == 1


@pytest.mark.asyncio
async def test_health_endpoint_reports_ok_in_capture_only_mode(tmp_path):
    context = make_capture_only_context(tmp_path)
    app = create_capture_api(
        capture_service=context.service,
        internal_token=context.settings.capture_service_internal_token,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "capture-service"}


@pytest.mark.asyncio
async def test_sigterm_style_shutdown_closes_service_cleanly(tmp_path, monkeypatch):
    closed = await _run_fake_capture_only_runtime(tmp_path, monkeypatch)

    assert closed == ["close"]


async def _run_fake_capture_only_runtime(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    closed = []

    class RecordingCaptureService(CaptureService):
        def close(self):
            closed.append("close")
            super().close()

    class FakeApiServer:
        def __init__(self, app, *, host, port):
            self.app = app
            self.host = host
            self.port = port

        async def serve(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        async def stop(self):
            pass

    class FakeClient(FakeDiscordClient):
        async def start(self, token):
            await self.on_ready_callback()

        async def close(self):
            pass

    def fake_create_discord_client(handle_gateway_message, on_ready_callback=None):
        client = FakeClient(FakeDiscordChannel())
        client.handle_gateway_message = handle_gateway_message
        client.on_ready_callback = on_ready_callback
        return client

    def open_service(cls, settings, *, notify_capture=None, receipt_client=None):
        assert notify_capture is None
        return RecordingCaptureService(
            settings=settings,
            ledger=Ledger(settings.ledger_path),
            notify_capture=notify_capture,
            receipt_client=receipt_client,
        )

    monkeypatch.setattr("secondbrain.app.InternalApiServer", FakeApiServer)
    monkeypatch.setattr("secondbrain.app.create_discord_client", fake_create_discord_client)
    monkeypatch.setattr("secondbrain.capture_service.CaptureService.open", classmethod(open_service))

    await run_capture_only_runtime(settings)

    return closed


def _fail_if_called(*args, **kwargs):
    raise AssertionError("should not be called in capture-only mode")
