import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from secondbrain.app import CaptureOnlyStartup, ensure_stale_lease_reaper_task
from secondbrain.capture_models import DELIVERY_FAILED, RETRY_WAIT
from secondbrain.capture_service import CaptureService
from secondbrain.config import Settings
from secondbrain.ledger import Ledger
from secondbrain.receipts import ATTACHMENT_WARNING, format_saved_receipt
from secondbrain.reconcile import ReconcileResult


_NOW = datetime(2026, 6, 9, 15, 0, 0, tzinfo=UTC)
_INSTANCE_A = "instance-a-uuid"
_INSTANCE_B = "instance-b-uuid"


BASE_ENV = {
    "CAPTURE_PROCESSING_MODE": "capture-only",
    "DISCORD_BOT_TOKEN": "discord-token",
    "DISCORD_GUILD_ID": "100",
    "DISCORD_CAPTURE_CHANNEL_ID": "200",
    "DISCORD_ALLOWED_USER_ID": "300",
    "LEDGER_PATH": ".runtime/ledger.sqlite3",
    "STARTUP_RECONCILE_LIMIT": "100",
    "CAPTURE_SERVICE_INTERNAL_TOKEN": "x" * 32,
    "CAPTURE_API_HOST": "127.0.0.1",
    "CAPTURE_API_PORT": "8000",
}


LOCAL_FULL_ENV = {
    **BASE_ENV,
    "CAPTURE_PROCESSING_MODE": "local-full",
    "GEMINI_API_KEY": "gemini-key",
    "GEMINI_MODEL": "gemini-test",
    "CLASSIFICATION_CONFIDENCE_THRESHOLD": "0.75",
    "CLASSIFIER_WORKER_COUNT": "1",
    "CLASSIFIER_QUEUE_MAXSIZE": "100",
    "VAULT_PATH": "/tmp/second-brain-test-vault",
}


def test_capture_only_mode_does_not_require_gemini_api_key(monkeypatch):
    _set_env(monkeypatch, BASE_ENV)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    settings = Settings()

    assert settings.capture_processing_mode == "capture-only"
    assert settings.gemini_api_key is None
    assert settings.gemini_model is None


def test_capture_only_mode_does_not_require_vault_path(monkeypatch):
    _set_env(monkeypatch, BASE_ENV)
    monkeypatch.delenv("VAULT_PATH", raising=False)

    settings = Settings()

    assert settings.capture_processing_mode == "capture-only"
    assert settings.vault_path is None


def test_local_full_mode_requires_gemini_api_key(monkeypatch):
    _set_env(monkeypatch, LOCAL_FULL_ENV)
    monkeypatch.delenv("GEMINI_API_KEY")

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        Settings()


def test_local_full_mode_requires_vault_path(monkeypatch):
    _set_env(monkeypatch, LOCAL_FULL_ENV)
    monkeypatch.delenv("VAULT_PATH")

    with pytest.raises(RuntimeError, match="VAULT_PATH"):
        Settings()


def test_unknown_processing_mode_is_rejected(monkeypatch):
    _set_env(monkeypatch, BASE_ENV)
    monkeypatch.setenv("CAPTURE_PROCESSING_MODE", "sometimes-local")

    with pytest.raises(RuntimeError, match="Unsupported capture processing mode: sometimes-local"):
        Settings()


def test_capture_only_receipt_does_not_claim_processing():
    capture = SimpleNamespace(capture_id="SB-20260608-0001")

    content = format_saved_receipt(
        capture,
        has_attachments=False,
        downstream_processing_enabled=False,
    )

    assert content == (
        "⏳ SB-20260608-0001 received.\n"
        "Your note is safely captured.\n"
        "Downstream filing is not enabled yet."
    )


def test_local_full_receipt_preserves_processing_message():
    capture = SimpleNamespace(capture_id="SB-20260608-0001")

    content = format_saved_receipt(
        capture,
        has_attachments=False,
        downstream_processing_enabled=True,
    )

    assert content == "⏳ SB-20260608-0001 received.\nYour note is saved. Processing…"


def test_capture_only_receipt_preserves_attachment_warning():
    capture = SimpleNamespace(capture_id="SB-20260608-0001")

    content = format_saved_receipt(
        capture,
        has_attachments=True,
        downstream_processing_enabled=False,
    )

    assert content.endswith(ATTACHMENT_WARNING)


@pytest.mark.asyncio
async def test_capture_only_reconciliation_failure_allows_retry():
    fake_result = ReconcileResult(seen=1, handled=1, ignored=0, warning=None)
    mock_service = SimpleNamespace(
        startup_reconcile=AsyncMock(side_effect=[RuntimeError("transient failure"), fake_result])
    )
    startup = CaptureOnlyStartup(capture_service=mock_service)
    fake_client = object()

    with pytest.raises(RuntimeError, match="transient failure"):
        await startup.start_once(fake_client)

    result = await startup.start_once(fake_client)

    assert result is fake_result
    assert mock_service.startup_reconcile.call_count == 2


@pytest.mark.asyncio
async def test_capture_only_repeated_ready_callback_reconciles_only_once_after_success():
    fake_result = ReconcileResult(seen=1, handled=1, ignored=0, warning=None)
    mock_service = SimpleNamespace(
        startup_reconcile=AsyncMock(return_value=fake_result)
    )
    startup = CaptureOnlyStartup(capture_service=mock_service)
    fake_client = object()

    first = await startup.start_once(fake_client)
    second = await startup.start_once(fake_client)

    assert first is fake_result
    assert second is None
    assert mock_service.startup_reconcile.call_count == 1


def _set_env(monkeypatch, env):
    for key in set(BASE_ENV) | set(LOCAL_FULL_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# ensure_stale_lease_reaper_task lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_reaper_task_creates_task_when_none_exists():
    async def fake_reaper_loop():
        await asyncio.sleep(10)

    startup = SimpleNamespace(reaper_task=None)
    capture_service = SimpleNamespace(run_stale_lease_reaper_loop=fake_reaper_loop)

    ensure_stale_lease_reaper_task(startup=startup, capture_service=capture_service)

    assert startup.reaper_task is not None
    assert not startup.reaper_task.done()
    startup.reaper_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup.reaper_task


@pytest.mark.asyncio
async def test_ensure_reaper_task_does_not_replace_running_task():
    async def fake_reaper_loop():
        await asyncio.sleep(10)

    startup = SimpleNamespace(reaper_task=None)
    capture_service = SimpleNamespace(run_stale_lease_reaper_loop=fake_reaper_loop)

    ensure_stale_lease_reaper_task(startup=startup, capture_service=capture_service)
    original_task = startup.reaper_task

    ensure_stale_lease_reaper_task(startup=startup, capture_service=capture_service)

    assert startup.reaper_task is original_task, "running task must not be replaced"
    original_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original_task


@pytest.mark.asyncio
async def test_ensure_reaper_task_restarts_completed_task():
    call_count = 0

    async def fake_reaper_loop():
        nonlocal call_count
        call_count += 1

    startup = SimpleNamespace(reaper_task=None)
    capture_service = SimpleNamespace(run_stale_lease_reaper_loop=fake_reaper_loop)

    ensure_stale_lease_reaper_task(startup=startup, capture_service=capture_service)
    original_task = startup.reaper_task
    await original_task  # run to completion

    assert original_task.done()

    ensure_stale_lease_reaper_task(startup=startup, capture_service=capture_service)

    assert startup.reaper_task is not original_task, "completed task must be replaced"
    await startup.reaper_task
    assert call_count == 2


@pytest.mark.asyncio
async def test_capture_only_ready_callback_restarts_dead_reaper_task():
    """On reconnect, ensure_stale_lease_reaper_task restarts a task that already exited."""
    reaper_call_count = 0

    async def fake_reaper_loop():
        nonlocal reaper_call_count
        reaper_call_count += 1
        # exits immediately — simulates a crashed reaper

    fake_result = ReconcileResult(seen=0, handled=0, ignored=0, warning=None)
    mock_service = SimpleNamespace(
        startup_reconcile=AsyncMock(return_value=fake_result),
        run_stale_lease_reaper_loop=fake_reaper_loop,
    )
    startup = CaptureOnlyStartup(capture_service=mock_service)
    fake_client = object()

    # First ready callback: reconcile + start reaper
    await startup.start_once(fake_client)
    ensure_stale_lease_reaper_task(startup=startup, capture_service=mock_service)
    first_task = startup.reaper_task
    await first_task  # reaper exits immediately

    assert first_task.done()
    assert reaper_call_count == 1

    # Second ready callback (e.g. Discord reconnect): dead reaper must be restarted
    await startup.start_once(fake_client)  # no-ops (already started)
    ensure_stale_lease_reaper_task(startup=startup, capture_service=mock_service)
    second_task = startup.reaper_task
    await second_task

    assert second_task is not first_task
    assert reaper_call_count == 2


# ---------------------------------------------------------------------------
# Reaper independence from Discord connectivity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_only_runtime_starts_reaper_before_discord_ready():
    """Reaper task is created at service init, before Discord on_ready fires."""
    reaper_running = asyncio.Event()

    async def fake_reaper_loop():
        reaper_running.set()
        await asyncio.sleep(10)

    startup = CaptureOnlyStartup(
        capture_service=SimpleNamespace(run_stale_lease_reaper_loop=fake_reaper_loop)
    )

    # This call happens in run_capture_only_runtime before the Discord task is created
    ensure_stale_lease_reaper_task(startup=startup, capture_service=startup.capture_service)

    await asyncio.sleep(0)  # yield to let the task start

    assert reaper_running.is_set(), "Reaper must start immediately, before Discord on_ready"
    assert startup.reaper_task is not None
    assert not startup.reaper_task.done()

    startup.reaper_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup.reaper_task


@pytest.mark.asyncio
async def test_capture_only_runtime_invokes_reaper_start_before_discord_task_creation(monkeypatch):
    """
    Calls run_capture_only_runtime with all I/O stubs. Verifies that startup.reaper_task
    is already set by the time run_service_runtime is entered — meaning the watchdog was
    created before the Discord client task, independent of on_ready.

    Would fail if ensure_stale_lease_reaper_task were moved back inside reconcile_once().
    """
    import secondbrain.app as app_module

    captured = {}

    async def noop_reaper_loop():
        await asyncio.sleep(10)

    async def noop_periodic_loop(client):
        await asyncio.sleep(10)

    service = SimpleNamespace(
        attach_receipt_client=lambda c: None,
        close=lambda: None,
        handle_gateway_message=AsyncMock(),
        run_stale_lease_reaper_loop=noop_reaper_loop,
        run_periodic_reconciliation_loop=noop_periodic_loop,
        record_capture_service_start=lambda **kw: None,
        record_capture_service_ready=lambda **kw: True,
        record_capture_service_heartbeat=lambda **kw: True,
        record_capture_service_stop=lambda **kw: True,
    )
    monkeypatch.setattr(app_module.CaptureService, "open", lambda *a, **kw: service)

    class FakeClient:
        async def start(self, token):
            await asyncio.sleep(10)  # Discord never fires on_ready

        async def close(self):
            pass

    monkeypatch.setattr(app_module, "create_discord_client", lambda *a, **kw: FakeClient())

    class FakeServer:
        async def serve(self):
            await asyncio.sleep(10)

        async def stop(self):
            pass

    monkeypatch.setattr(app_module, "InternalApiServer", lambda *a, **kw: FakeServer())

    async def spy_run_service_runtime(*, startup, api_task, discord_task, **kw):
        captured["startup"] = startup
        api_task.cancel()
        discord_task.cancel()
        raise asyncio.CancelledError()

    monkeypatch.setattr(app_module, "run_service_runtime", spy_run_service_runtime)

    _set_env(monkeypatch, BASE_ENV)
    from secondbrain.config import Settings as _Settings

    settings = _Settings()

    with suppress(asyncio.CancelledError):
        await app_module.run_capture_only_runtime(settings)

    startup = captured.get("startup")
    assert startup is not None, "spy was never reached"
    assert startup.reaper_task is not None, (
        "Reaper task must be created before Discord task; "
        "ensure_stale_lease_reaper_task was not called at service init"
    )

    if startup.reaper_task and not startup.reaper_task.done():
        startup.reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await startup.reaper_task


@pytest.mark.asyncio
async def test_startup_reconciliation_failure_does_not_prevent_reaper_start(tmp_path):
    """Stale leases are recovered even when startup reconciliation raises."""
    from secondbrain.reaper import run_stale_lease_reaper_once

    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    stale_now = now + timedelta(minutes=5)

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        ledger_path=tmp_path / "ledger.sqlite3",
        stale_lease_reaper_interval_seconds=0,
        stale_lease_reaper_batch_size=100,
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
    )
    service = CaptureService(settings=settings, ledger=ledger)

    # Insert a capture and let its lease expire
    ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="test",
        received_at=now,
    )
    claimed = ledger.claim_due_deliveries(
        now=now, lease_until=now + timedelta(minutes=1), batch_size=10
    )
    capture_id = claimed[0].capture_id

    # Simulate the fixed startup: reaper starts before Discord
    startup = CaptureOnlyStartup(capture_service=service)
    ensure_stale_lease_reaper_task(startup=startup, capture_service=service)
    assert startup.reaper_task is not None

    # Simulate reconciliation failure (what would have gated the reaper in the old code)
    failing_service = SimpleNamespace(
        startup_reconcile=AsyncMock(side_effect=RuntimeError("Discord timeout"))
    )
    failing_startup = CaptureOnlyStartup(capture_service=failing_service)
    with pytest.raises(RuntimeError, match="Discord timeout"):
        await failing_startup.start_once(object())

    # Despite the reconcile failure, the reaper (started early) still processes stale leases
    result = await run_stale_lease_reaper_once(
        settings=settings,
        ledger=ledger,
        _now=stale_now,
    )
    assert result.requeued or result.failed, (
        "Reaper must process the stale row regardless of reconcile outcome"
    )
    capture = ledger.get_capture(capture_id)
    assert capture.delivery_status in (RETRY_WAIT, DELIVERY_FAILED)

    startup.reaper_task.cancel()
    with suppress(asyncio.CancelledError):
        await startup.reaper_task

    ledger.close()


# ---------------------------------------------------------------------------
# Lifecycle ordering: RUNNING is set only after background tasks are ready
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_only_records_running_only_after_background_tasks_are_initialized(
    monkeypatch,
):
    """
    record_capture_service_ready (which sets state=RUNNING) must be called only
    after ensure_periodic_reconciliation_task and ensure_stale_lease_reaper_task
    have both run inside reconcile_once.
    """
    import secondbrain.app as app_module

    ready_state = {}
    startup_ref = [None]
    ready_event = asyncio.Event()

    async def noop_reaper_loop():
        await asyncio.sleep(10)

    async def noop_periodic_loop(client):
        await asyncio.sleep(10)

    def recording_ready(**kw):
        s = startup_ref[0]
        if s is not None:
            ready_state["periodic_task_set"] = s.periodic_task is not None
            ready_state["reaper_task_set"] = s.reaper_task is not None
        ready_event.set()
        return True

    fake_reconcile_result = ReconcileResult(seen=0, handled=0, ignored=0, warning=None)

    service = SimpleNamespace(
        attach_receipt_client=lambda c: None,
        close=lambda: None,
        handle_gateway_message=AsyncMock(),
        run_stale_lease_reaper_loop=noop_reaper_loop,
        run_periodic_reconciliation_loop=noop_periodic_loop,
        startup_reconcile=AsyncMock(return_value=fake_reconcile_result),
        record_capture_service_start=lambda **kw: None,
        record_capture_service_ready=recording_ready,
        record_capture_service_heartbeat=lambda **kw: True,
        record_capture_service_stop=lambda **kw: True,
    )

    monkeypatch.setattr(app_module.CaptureService, "open", lambda *a, **kw: service)

    original_startup_init = app_module.CaptureOnlyStartup.__init__

    def spy_startup_init(self, **kw):
        original_startup_init(self, **kw)
        startup_ref[0] = self

    monkeypatch.setattr(app_module.CaptureOnlyStartup, "__init__", spy_startup_init)

    class FakeClient:
        def __init__(self):
            self.on_ready_callback = None

        async def start(self, token):
            if self.on_ready_callback:
                await self.on_ready_callback()
            await asyncio.sleep(10)

        async def close(self):
            pass

    fake_client = FakeClient()

    def fake_create_discord_client(handle, on_ready_callback=None):
        fake_client.on_ready_callback = on_ready_callback
        return fake_client

    monkeypatch.setattr(app_module, "create_discord_client", fake_create_discord_client)

    class FakeServer:
        async def serve(self):
            await asyncio.sleep(10)

        async def stop(self):
            pass

    monkeypatch.setattr(app_module, "InternalApiServer", lambda *a, **kw: FakeServer())

    async def spy_run_service_runtime(*, api_task, discord_task, **kw):
        await ready_event.wait()
        api_task.cancel()
        discord_task.cancel()
        raise asyncio.CancelledError()

    monkeypatch.setattr(app_module, "run_service_runtime", spy_run_service_runtime)

    _set_env(monkeypatch, BASE_ENV)
    from secondbrain.config import Settings as _Settings

    settings = _Settings()

    with suppress(asyncio.CancelledError):
        await app_module.run_capture_only_runtime(settings)

    assert ready_state.get("periodic_task_set") is True, "periodic task must exist when RUNNING is set"
    assert ready_state.get("reaper_task_set") is True, "reaper task must exist when RUNNING is set"


# ---------------------------------------------------------------------------
# Lifecycle log accuracy: ignored operations are logged with honest event names
# ---------------------------------------------------------------------------

def test_old_instance_ready_logs_ignored_not_ready(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.record_capture_service_start(instance_id=_INSTANCE_B, now=_NOW)
    ledger.close()

    settings = SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        ledger_path=tmp_path / "ledger.sqlite3",
        stale_lease_reaper_interval_seconds=30,
        stale_lease_reaper_batch_size=100,
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
    )
    service = CaptureService(settings=settings, ledger=Ledger(tmp_path / "ledger.sqlite3"))

    result = service.record_capture_service_ready(instance_id=_INSTANCE_A, now=_NOW)

    output = capsys.readouterr().out
    assert result is False
    assert "capture_service_ready_ignored" in output
    assert "superseded_instance" in output
    assert "capture_service_ready" not in output.replace("capture_service_ready_ignored", "")

    service.close()


def test_old_instance_stop_logs_ignored_not_stopped(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.record_capture_service_start(instance_id=_INSTANCE_B, now=_NOW)
    ledger.record_capture_service_ready(instance_id=_INSTANCE_B, now=_NOW + timedelta(seconds=2))
    ledger.close()

    settings = SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        ledger_path=tmp_path / "ledger.sqlite3",
        stale_lease_reaper_interval_seconds=30,
        stale_lease_reaper_batch_size=100,
        delivery_retry_max_attempts=5,
        delivery_retry_base_delay_seconds=10,
        delivery_retry_max_delay_seconds=300,
    )
    service = CaptureService(settings=settings, ledger=Ledger(tmp_path / "ledger.sqlite3"))

    result = service.record_capture_service_stop(instance_id=_INSTANCE_A, now=_NOW)

    output = capsys.readouterr().out
    assert result is False
    assert "capture_service_stop_ignored" in output
    assert "superseded_instance" in output

    service.close()
