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
        stale_lease_reaper_interval_seconds=30,
        stale_lease_reaper_batch_size=100,
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
        capture_service_heartbeat_interval_seconds=15,
        downstream_delivery_enabled=False,
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


# ---------------------------------------------------------------------------
# Status query integration — read_operational_status against real ledger state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_reports_retry_backlog_without_manual_sqlite_query(tmp_path):
    """
    End-to-end path from SB-108 reaper to SB-109 diagnostics:
    claim → mark forwarded → lease expires → reaper runs → RETRY_WAIT →
    status reader reports captures_waiting_for_retry = 1, stale_leases = 0.
    """
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from secondbrain.reaper import run_stale_lease_reaper_once
    from secondbrain.status import StatusSettings, read_operational_status

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)

    now = datetime.now(UTC)
    result = ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="test capture",
        received_at=now,
    )

    lease_until = now + timedelta(seconds=30)
    claims = ledger.claim_due_deliveries(
        now=now,
        lease_until=lease_until,
        batch_size=10,
    )
    assert len(claims) == 1

    ledger.mark_forwarded(
        capture_id=result.capture.capture_id,
        delivery_attempt=claims[0].delivery_attempts,
        lease_until=lease_until,
    )

    reaper_settings = SimpleNamespace(
        stale_lease_reaper_batch_size=10,
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
    )
    await run_stale_lease_reaper_once(
        settings=reaper_settings,
        ledger=ledger,
        _now=lease_until + timedelta(seconds=1),
    )

    ledger.close()

    snapshot = read_operational_status(
        settings=StatusSettings(
            ledger_path=settings.ledger_path,
            vault_path=None,
            status_timezone="UTC",
            capture_service_health_stale_after_seconds=60,
        ),
        now=lease_until + timedelta(seconds=1),
    )
    assert snapshot.captures_waiting_for_retry == 1
    assert snapshot.stale_leases == 0


def test_status_reports_stale_lease_before_watchdog_recovery(tmp_path):
    from datetime import UTC, datetime, timedelta
    from secondbrain.status import StatusSettings, read_operational_status

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)

    now = datetime.now(UTC)
    past = now - timedelta(minutes=10)
    ledger.insert_accepted_capture(
        discord_message_id="2001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="stale lease capture",
    )
    # Claim with a lease that is already expired at query time
    ledger.claim_due_deliveries(
        now=past,
        lease_until=past + timedelta(minutes=3),  # expired 7 minutes ago
        batch_size=10,
    )
    ledger.close()

    status_settings = StatusSettings(
        ledger_path=settings.ledger_path,
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=status_settings, now=now)
    assert snapshot.stale_leases == 1


def test_status_reports_stale_capture_service_after_heartbeat_stops(tmp_path):
    from datetime import UTC, datetime, timedelta
    from secondbrain.status import StatusSettings, read_operational_status

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)

    now = datetime.now(UTC)
    old_heartbeat = now - timedelta(minutes=5)  # > 60s stale threshold

    ledger.record_capture_service_start(
        instance_id="test-instance",
        now=old_heartbeat - timedelta(seconds=30),
    )
    ledger.record_capture_service_ready(
        instance_id="test-instance",
        now=old_heartbeat,
    )
    ledger.record_capture_service_heartbeat(
        instance_id="test-instance",
        now=old_heartbeat,
    )
    ledger.close()

    status_settings = StatusSettings(
        ledger_path=settings.ledger_path,
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=status_settings, now=now)
    assert snapshot.capture_service_health == "STALE"


def test_status_reports_healthy_after_local_full_runtime_ready(tmp_path):
    """
    Real-ledger regression: after the local-full startup sequence (STARTING →
    heartbeat → RUNNING) the status reader must return HEALTHY, not STALE or
    UNKNOWN.  This closes the gap between the stub-based unit tests and the
    actual ledger write path.
    """
    from datetime import UTC, datetime, timedelta
    from secondbrain.status import StatusSettings, read_operational_status

    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)

    now = datetime.now(UTC)
    instance_id = "local-full-test-instance"

    ledger.record_capture_service_start(instance_id=instance_id, now=now - timedelta(seconds=30))
    ledger.record_capture_service_ready(instance_id=instance_id, now=now - timedelta(seconds=25))
    ledger.record_capture_service_heartbeat(instance_id=instance_id, now=now - timedelta(seconds=5))
    ledger.close()

    snapshot = read_operational_status(
        settings=StatusSettings(
            ledger_path=settings.ledger_path,
            vault_path=None,
            status_timezone="UTC",
            capture_service_health_stale_after_seconds=60,
        ),
        now=now,
    )
    assert snapshot.capture_service_health == "HEALTHY"
    assert snapshot.capture_service_state == "RUNNING"
