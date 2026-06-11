import asyncio
from datetime import UTC, datetime, timedelta
import os
import signal
from types import SimpleNamespace

import pytest

from secondbrain.app import (
    LocalWorkerStartup,
    ensure_periodic_reconciliation_task,
    main,
    run_discord_listener,
    run_manual_retry,
    run_service_runtime,
    run_status,
    start_local_worker_and_enqueue_recovered,
)
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import FAILED, FILED, FORWARDED, INBOX, RECEIVED, REJECTED_SENSITIVE, Ledger
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID
from secondbrain.worker import CaptureQueue
from secondbrain.vault_writer import VaultWriter


def make_settings(tmp_path):
    return SimpleNamespace(
        discord_guild_id=300,
        discord_capture_channel_id=200,
        discord_allowed_user_id=400,
        classifier_queue_maxsize=10,
        startup_reconcile_limit=10,
        periodic_reconcile_limit=10,
        periodic_reconcile_interval_seconds=60,
        gemini_api_key="fake",
        gemini_model="gemini-test",
        classification_confidence_threshold=0.75,
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
    )


def make_message(*, content="capture this", message_id=1001, attachments=None):
    channel = FakeChannel()
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=300),
        channel=channel,
        author=SimpleNamespace(id=400, bot=False),
        webhook_id=None,
        content=content,
        attachments=attachments or [],
    )


def make_service(settings, ledger, queue, receipt_client=None):
    return CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=queue.enqueue,
        receipt_client=receipt_client,
    )


@pytest.mark.asyncio
async def test_capture_handler_persists_receipts_and_enqueues_capture(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    message = make_message(content="Review reconnect handling.")
    service = make_service(make_settings(tmp_path), ledger, queue, receipt_client=FakeClient(message.channel))

    await service.handle_gateway_message(message)

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)

    assert capture.status == RECEIVED
    assert capture.raw_text == "Review reconnect handling."
    assert capture.receipt_message_id == "9001"
    assert message.channel.last_content == (
        f"⏳ {capture.capture_id} received.\n"
        "Your note is saved. Processing…"
    )


@pytest.mark.asyncio
async def test_capture_handler_logs_metadata_without_raw_text(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    service = make_service(make_settings(tmp_path), ledger, queue)

    await service.handle_gateway_message(make_message(content="Review reconnect handling."))

    output = capsys.readouterr().out
    assert "capture_received" in output
    assert "discord_message_id" in output
    assert "Review reconnect handling." not in output


@pytest.mark.asyncio
async def test_startup_reconcile_advances_marker_after_commit(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    settings = make_settings(tmp_path)
    service = make_service(settings, ledger, queue)

    await service.startup_reconcile(FakeHistoryClient([make_message(content="Review reconnect handling.", message_id=1002)]))

    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1002"


@pytest.mark.asyncio
async def test_duplicate_capture_does_not_send_new_receipt_or_enqueue(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    service = make_service(make_settings(tmp_path), ledger, queue)
    original = make_message(content="Review reconnect handling.", message_id=1001)
    duplicate = make_message(content="Review reconnect handling again.", message_id=1001)

    await service.handle_gateway_message(original)
    await queue.get()
    await service.handle_gateway_message(duplicate)

    assert queue.qsize() == 0
    assert duplicate.channel.sent_contents == []
    assert ledger.status_counts() == {RECEIVED: 1}


@pytest.mark.asyncio
async def test_saved_receipt_failure_does_not_block_enqueue(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    message = make_message(content="Review reconnect handling.")
    message.channel.fail_send = True
    service = make_service(make_settings(tmp_path), ledger, queue, receipt_client=FakeClient(message.channel))

    await service.handle_gateway_message(message)

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)
    assert capture.status == RECEIVED
    assert capture.receipt_message_id is None


@pytest.mark.asyncio
async def test_saved_receipt_warns_when_attachment_is_not_archived(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    message = make_message(
        content="Review this attached sketch.",
        attachments=[SimpleNamespace(filename="sketch.png", content_type="image/png", size=100, url="url")],
    )
    service = make_service(make_settings(tmp_path), ledger, queue, receipt_client=FakeClient(message.channel))

    await service.handle_gateway_message(message)

    assert "⚠️ Attachment detected but not archived in the MVP." in message.channel.last_content


@pytest.mark.asyncio
async def test_capture_handler_rejects_sensitive_message_without_enqueueing(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    service = make_service(make_settings(tmp_path), ledger, queue)

    message = make_message(content="password=hunter2")

    await service.handle_gateway_message(message)

    assert queue.qsize() == 0
    assert ledger.status_counts() == {REJECTED_SENSITIVE: 1}
    assert ledger.enqueueable_capture_ids() == []

    rejected = ledger.captures_by_status(REJECTED_SENSITIVE)
    assert len(rejected) == 1
    assert rejected[0].raw_text is None
    assert rejected[0].redacted_text == "password=[REDACTED]"
    assert "hunter2" not in rejected[0].redacted_text
    assert rejected[0].receipt_message_id == "9001"
    assert "hunter2" not in message.channel.last_content
    assert message.channel.last_content == (
        "⚠️ Message rejected.\n"
        "It appears to contain a credential or sensitive identifier.\n"
        "The original text was not saved or sent to Gemini."
    )
    output = capsys.readouterr().out
    assert "capture_rejected_sensitive" in output
    assert "hunter2" not in output
    assert "password=[REDACTED]" not in output


def test_format_operational_status_includes_basic_counts(tmp_path):
    from secondbrain.status import StatusSettings, read_operational_status, format_operational_status

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    filed = insert_capture(ledger, discord_message_id="1001")
    inbox = insert_capture(ledger, discord_message_id="1002")
    ledger.insert_sensitive_rejection(
        discord_message_id="1003",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        redacted_text="password=[REDACTED]",
        sensitivity_flags=("password_assignment",),
    )
    failed = insert_capture(ledger, discord_message_id="1004")
    ledger.update_capture(filed.capture_id, status=FILED, derived_note_path="20_projects/halo/filed.md")
    ledger.update_capture(inbox.capture_id, status=INBOX, derived_note_path="00_inbox/inbox.md")
    ledger.update_capture(failed.capture_id, status=FAILED)
    ledger.set_system_state(LAST_RECONCILED_MESSAGE_ID, "1513233540316266517")
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    report = format_operational_status(snapshot)

    assert f"ledger path: {tmp_path / 'ledger.sqlite3'}" in report
    assert f"vault path: {tmp_path / 'vault'}" in report
    assert "total captures: 4" in report
    assert "captures in inbox: 1" in report
    assert "captures rejected as sensitive: 1" in report
    assert "captures failed: 1" in report
    assert "last reconciled Discord message ID: 1513233540316266517" in report
    assert "last successful vault write: 00_inbox/inbox.md" in report


def test_main_reports_configuration_errors_without_traceback(monkeypatch, capsys):
    def fail_to_run():
        raise RuntimeError("Missing required configuration: CAPTURE_SERVICE_INTERNAL_TOKEN")

    monkeypatch.setattr("secondbrain.app.run_discord_listener", fail_to_run)

    exit_code = main(["run"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err == "error: Missing required configuration: CAPTURE_SERVICE_INTERNAL_TOKEN\n"
    assert "Traceback" not in captured.err


def test_run_discord_listener_handles_keyboard_interrupt_cleanly(monkeypatch, capsys):
    def raise_keyboard_interrupt(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "secondbrain.app.asyncio.run",
        raise_keyboard_interrupt,
    )

    run_discord_listener()

    captured = capsys.readouterr()
    assert "shutdown complete" in captured.out


@pytest.mark.asyncio
async def test_startup_recovery_does_not_deadlock_when_backlog_exceeds_queue_size(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    first = insert_attachment_only_capture(ledger, discord_message_id="1001")
    second = insert_attachment_only_capture(ledger, discord_message_id="1002")
    queue = CaptureQueue(maxsize=1)
    service = CaptureService(
        settings=settings,
        ledger=ledger,
        notify_capture=queue.enqueue,
    )

    worker_task, capture_ids = await asyncio.wait_for(
        start_local_worker_and_enqueue_recovered(
            settings=settings,
            capture_service=service,
            queue=queue,
            vault_writer=VaultWriter(settings.vault_path),
        ),
        timeout=1,
    )

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    assert capture_ids == [first.capture_id, second.capture_id]
    assert ledger.status_counts() == {INBOX: 2}


@pytest.mark.asyncio
async def test_reconciliation_failure_does_not_permanently_block_worker_startup(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = insert_attachment_only_capture(ledger, discord_message_id="1001")
    queue = CaptureQueue(maxsize=1)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)
    startup = LocalWorkerStartup(
        settings=settings,
        capture_service=service,
        queue=queue,
        vault_writer=VaultWriter(settings.vault_path),
    )

    with pytest.raises(RuntimeError, match="simulated Discord history failure"):
        await startup.start_once(FailingHistoryClient())

    assert startup.worker_task is None

    result = await startup.start_once(FakeHistoryClient([]))
    assert result is not None
    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        startup.worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup.worker_task

    assert result.capture_ids == [capture.capture_id]
    assert ledger.status_counts() == {INBOX: 1}


@pytest.mark.asyncio
async def test_forwarded_capture_is_recovered_after_restart(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = insert_attachment_only_capture(ledger, discord_message_id="1001")
    queue = CaptureQueue(maxsize=1)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)

    transition = service.mark_forwarded(capture.capture_id)
    assert transition.status == FORWARDED

    worker_task, capture_ids = await start_local_worker_and_enqueue_recovered(
        settings=settings,
        capture_service=service,
        queue=queue,
        vault_writer=VaultWriter(settings.vault_path),
    )

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    updated = service.get_capture(capture.capture_id)
    assert capture_ids == [capture.capture_id]
    assert updated.status == INBOX
    assert updated.derived_note_path is not None


@pytest.mark.asyncio
async def test_repeated_ready_callback_does_not_start_second_worker(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    insert_attachment_only_capture(ledger, discord_message_id="1001")
    queue = CaptureQueue(maxsize=1)
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)
    startup = LocalWorkerStartup(
        settings=settings,
        capture_service=service,
        queue=queue,
        vault_writer=VaultWriter(settings.vault_path),
    )

    first = await startup.start_once(FakeHistoryClient([]))
    assert first is not None
    second = await startup.start_once(FakeHistoryClient([]))

    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        first.worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first.worker_task

    assert second is None
    assert startup.worker_task is first.worker_task


@pytest.mark.asyncio
async def test_enqueue_failure_cancels_started_worker_and_allows_clean_retry(tmp_path, monkeypatch):
    started = []
    cancelled = []

    async def recording_worker(**kwargs):
        marker = object()
        started.append(marker)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(marker)
            raise

    monkeypatch.setattr("secondbrain.app.run_capture_worker", recording_worker)

    settings = make_settings(tmp_path)
    queue = CaptureQueue(maxsize=1)
    service = FlakyEnqueueService()
    startup = LocalWorkerStartup(
        settings=settings,
        capture_service=service,
        queue=queue,
        vault_writer=VaultWriter(settings.vault_path),
    )

    with pytest.raises(RuntimeError, match="simulated enqueue failure"):
        await startup.start_once(FakeHistoryClient([]))

    assert startup.worker_task is None
    assert len(started) == 1
    assert cancelled == started

    result = await startup.start_once(FakeHistoryClient([]))
    await asyncio.sleep(0)

    assert result is not None
    assert result.capture_ids == ["recovered-capture"]
    assert startup.worker_task is result.worker_task
    assert len(started) == 2
    assert cancelled == started[:1]
    assert not result.worker_task.done()

    result.worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await result.worker_task

    assert cancelled == started


@pytest.mark.asyncio
async def test_runtime_stops_other_component_when_api_task_exits():
    events = []
    api_server = FakeRuntimeApiServer(events, exits_immediately=True)
    client = FakeRuntimeDiscordClient(events)
    startup = SimpleNamespace(worker_task=None)
    capture_service = FakeRuntimeCaptureService(events)

    await run_service_runtime(
        api_task=asyncio.create_task(api_server.serve()),
        discord_task=asyncio.create_task(client.start("token")),
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
    )

    assert "api.stop" in events
    assert "discord.close" in events
    assert "discord.cancelled" in events
    assert events[-1] == "capture_service.close"


@pytest.mark.asyncio
async def test_runtime_stops_other_component_when_discord_task_exits():
    events = []
    api_server = FakeRuntimeApiServer(events)
    client = FakeRuntimeDiscordClient(events, exits_immediately=True)
    startup = SimpleNamespace(worker_task=None)
    capture_service = FakeRuntimeCaptureService(events)

    await run_service_runtime(
        api_task=asyncio.create_task(api_server.serve()),
        discord_task=asyncio.create_task(client.start("token")),
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
    )

    assert "api.stop" in events
    assert "api.cancelled" in events
    assert "discord.close" in events
    assert events[-1] == "capture_service.close"


@pytest.mark.asyncio
async def test_runtime_cancels_worker_before_closing_capture_service():
    events = []
    api_server = FakeRuntimeApiServer(events, exits_immediately=True)
    client = FakeRuntimeDiscordClient(events)
    worker_task = asyncio.create_task(runtime_worker(events))
    await asyncio.sleep(0)
    startup = SimpleNamespace(worker_task=worker_task)
    capture_service = FakeRuntimeCaptureService(events)

    await run_service_runtime(
        api_task=asyncio.create_task(api_server.serve()),
        discord_task=asyncio.create_task(client.start("token")),
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
    )

    assert events.index("worker.cancelled") < events.index("capture_service.close")


@pytest.mark.asyncio
async def test_runtime_closes_capture_service_last():
    events = []
    api_server = FakeRuntimeApiServer(events, exits_immediately=True)
    client = FakeRuntimeDiscordClient(events)
    worker_task = asyncio.create_task(runtime_worker(events))
    await asyncio.sleep(0)
    startup = SimpleNamespace(worker_task=worker_task)
    capture_service = FakeRuntimeCaptureService(events)

    await run_service_runtime(
        api_task=asyncio.create_task(api_server.serve()),
        discord_task=asyncio.create_task(client.start("token")),
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
    )

    assert events[-1] == "capture_service.close"


@pytest.mark.asyncio
async def test_runtime_shuts_down_cleanly_on_sigterm():
    """SIGTERM sets the stop event, wakes asyncio.wait, and completes the full shutdown sequence."""
    events = []
    api_server = FakeRuntimeApiServer(events)
    client = FakeRuntimeDiscordClient(events)
    worker_task = asyncio.create_task(runtime_worker(events))
    await asyncio.sleep(0)
    startup = SimpleNamespace(worker_task=worker_task)
    capture_service = FakeRuntimeCaptureService(events)

    async def fire_sigterm():
        await asyncio.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(fire_sigterm())

    await run_service_runtime(
        api_task=asyncio.create_task(api_server.serve()),
        discord_task=asyncio.create_task(client.start("token")),
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
        instance_id="test-instance",
    )

    assert "api.stop" in events
    assert "discord.close" in events
    assert "worker.cancelled" in events
    assert len(capture_service.stop_calls) == 1
    assert capture_service.stop_calls[0] == "test-instance"
    assert events[-1] == "capture_service.close"


@pytest.mark.asyncio
async def test_runtime_removes_signal_handlers_after_shutdown():
    """Signal handlers installed for SIGTERM and SIGINT are removed when the runtime exits."""
    events = []
    api_server = FakeRuntimeApiServer(events)
    client = FakeRuntimeDiscordClient(events)
    startup = SimpleNamespace(worker_task=None)
    capture_service = FakeRuntimeCaptureService(events)

    loop = asyncio.get_running_loop()

    async def fire_sigterm():
        await asyncio.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(fire_sigterm())

    await run_service_runtime(
        api_task=asyncio.create_task(api_server.serve()),
        discord_task=asyncio.create_task(client.start("token")),
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
    )

    assert not loop.remove_signal_handler(signal.SIGTERM)
    assert not loop.remove_signal_handler(signal.SIGINT)


@pytest.mark.asyncio
async def test_runtime_record_stop_called_exactly_once_and_close_is_last():
    """record_capture_service_stop fires exactly once and capture_service.close is the final action."""
    events = []
    api_server = FakeRuntimeApiServer(events, exits_immediately=True)
    client = FakeRuntimeDiscordClient(events)
    startup = SimpleNamespace(worker_task=None)
    capture_service = FakeRuntimeCaptureService(events)

    await run_service_runtime(
        api_task=asyncio.create_task(api_server.serve()),
        discord_task=asyncio.create_task(client.start("token")),
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
        instance_id="unique-id",
    )

    assert len(capture_service.stop_calls) == 1
    assert capture_service.stop_calls[0] == "unique-id"
    assert events[-1] == "capture_service.close"


class FakeChannel:
    def __init__(self):
        self.id = 200
        self.fail_send = False
        self.sent_contents = []

    async def send(self, content):
        if self.fail_send:
            raise RuntimeError("discord unavailable")
        self.sent_contents.append(content)
        self.last_content = content
        return SimpleNamespace(id=9001)


class FakeClient:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


class FakeHistoryClient:
    def __init__(self, messages):
        self.channel = FakeHistoryChannel(messages)

    def get_channel(self, channel_id):
        return self.channel


class FailingHistoryClient:
    def get_channel(self, channel_id):
        return FailingHistoryChannel()


class FailingHistoryChannel:
    def history(self, *, limit, after, oldest_first):
        raise RuntimeError("simulated Discord history failure")


class FlakyEnqueueService:
    def __init__(self):
        self.enqueue_calls = 0

    async def startup_reconcile(self, client):
        from secondbrain.reconcile import ReconcileResult

        return ReconcileResult(seen=0, handled=0, ignored=0, warning=None)

    async def enqueue_unfinished_captures(self):
        self.enqueue_calls += 1
        if self.enqueue_calls == 1:
            await asyncio.sleep(0)
            raise RuntimeError("simulated enqueue failure")
        return ["recovered-capture"]


class FakeRuntimeApiServer:
    def __init__(self, events, *, exits_immediately=False):
        self.events = events
        self.exits_immediately = exits_immediately

    async def serve(self):
        self.events.append("api.start")
        if self.exits_immediately:
            self.events.append("api.exit")
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.events.append("api.cancelled")
            raise

    async def stop(self):
        self.events.append("api.stop")


class FakeRuntimeDiscordClient:
    def __init__(self, events, *, exits_immediately=False):
        self.events = events
        self.exits_immediately = exits_immediately

    async def start(self, token):
        self.events.append("discord.start")
        if self.exits_immediately:
            self.events.append("discord.exit")
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.events.append("discord.cancelled")
            raise

    async def close(self):
        self.events.append("discord.close")


class FakeRuntimeCaptureService:
    def __init__(self, events):
        self.events = events
        self.stop_calls: list[str] = []

    def record_capture_service_stop(self, *, instance_id: str, now) -> bool:
        self.stop_calls.append(instance_id)
        return True

    def close(self):
        self.events.append("capture_service.close")


async def runtime_worker(events):
    events.append("worker.start")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        events.append("worker.cancelled")
        raise


class FakeHistoryChannel:
    def __init__(self, messages):
        self.messages = messages

    def history(self, *, limit, after, oldest_first):
        after_id = 0 if after is None else after.id
        messages = [message for message in self.messages if message.id > after_id]
        if oldest_first:
            messages = sorted(messages, key=lambda message: message.id)
        return FakeHistory(messages[:limit])


class FakeHistory:
    def __init__(self, messages):
        self.messages = messages

    def __aiter__(self):
        self._iterator = iter(self.messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


@pytest.mark.asyncio
async def test_live_gateway_capture_does_not_advance_reconcile_marker(tmp_path):
    """handle_gateway_message is the live path and must never touch the history-scan cursor."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    service = make_service(make_settings(tmp_path), ledger, queue)

    await service.handle_gateway_message(make_message(message_id=1002, content="Live capture."))

    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) is None
    assert queue.qsize() == 1


def insert_capture(ledger: Ledger, *, discord_message_id: str = "1001"):
    return ledger.insert_accepted_capture(
        discord_message_id=discord_message_id,
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
    ).capture


def insert_attachment_only_capture(ledger: Ledger, *, discord_message_id: str):
    return ledger.insert_accepted_capture(
        discord_message_id=discord_message_id,
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="",
        has_attachments=True,
        attachment_metadata=[
            {
                "filename": "image.png",
                "content_type": "image/png",
                "size": 100,
                "url": "https://cdn.discordapp.com/attachments/image.png",
            }
        ],
    ).capture


def test_service_periodic_reconcile_snapshot_forwards_ledger_metrics(tmp_path):
    """CaptureService.periodic_reconcile_snapshot() returns the ledger metrics dict."""
    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)

    snapshot = service.periodic_reconcile_snapshot()

    assert isinstance(snapshot, dict)
    assert "periodic_reconcile_runs_total" in snapshot
    assert "periodic_reconcile_last_warning" in snapshot
    assert "periodic_reconcile_last_error_type" in snapshot


def test_run_status_reports_operational_status_sections(tmp_path):
    """format_operational_status includes all required sections."""
    from secondbrain.status import StatusSettings, read_operational_status, format_operational_status

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    report = format_operational_status(snapshot)

    assert "Second Brain operational status" in report
    assert "Capture intake" in report
    assert "Note lifecycle" in report
    assert "Delivery backlog" in report
    assert "Discord reconciliation" in report
    assert "Capture service" in report
    assert "capture-service health:" in report


@pytest.mark.asyncio
async def test_ensure_periodic_task_creates_task_when_none_exists(tmp_path, monkeypatch):
    """ensure_periodic_reconciliation_task creates a task when startup.periodic_task is None."""
    async def mock_loop(self, client):
        await asyncio.Event().wait()

    monkeypatch.setattr(CaptureService, "run_periodic_reconciliation_loop", mock_loop)

    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)
    startup = SimpleNamespace(periodic_task=None)

    ensure_periodic_reconciliation_task(startup=startup, client=None, capture_service=service)
    await asyncio.sleep(0)

    assert startup.periodic_task is not None
    assert not startup.periodic_task.done()

    startup.periodic_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup.periodic_task


@pytest.mark.asyncio
async def test_ensure_periodic_task_restarts_dead_task(tmp_path, monkeypatch):
    """ensure_periodic_reconciliation_task replaces a completed (dead) task."""
    async def mock_loop(self, client):
        return  # returns immediately — task will be done

    monkeypatch.setattr(CaptureService, "run_periodic_reconciliation_loop", mock_loop)

    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)
    startup = SimpleNamespace(periodic_task=None)

    ensure_periodic_reconciliation_task(startup=startup, client=None, capture_service=service)
    first_task = startup.periodic_task
    await first_task  # let it finish

    assert first_task.done()

    ensure_periodic_reconciliation_task(startup=startup, client=None, capture_service=service)

    assert startup.periodic_task is not first_task


@pytest.mark.asyncio
async def test_ensure_periodic_task_does_not_replace_running_task(tmp_path, monkeypatch):
    """ensure_periodic_reconciliation_task is a no-op when a live task already exists."""
    async def mock_loop(self, client):
        await asyncio.Event().wait()

    monkeypatch.setattr(CaptureService, "run_periodic_reconciliation_loop", mock_loop)

    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)
    startup = SimpleNamespace(periodic_task=None)

    ensure_periodic_reconciliation_task(startup=startup, client=None, capture_service=service)
    await asyncio.sleep(0)
    first_task = startup.periodic_task

    ensure_periodic_reconciliation_task(startup=startup, client=None, capture_service=service)

    assert startup.periodic_task is first_task

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task


def test_run_status_cli_path_reports_operational_status(tmp_path, monkeypatch, capsys):
    """run_status() reads the ledger read-only and prints the operational report."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.close()

    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

    exit_code = run_status()

    output = capsys.readouterr().out
    assert "Second Brain operational status" in output
    assert "Capture intake" in output
    assert "capture-service health:" in output
    assert exit_code in (0, 1)


def test_run_status_via_main_cli(tmp_path, monkeypatch, capsys):
    """main(['status']) executes run_status() end-to-end."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.close()

    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("STATUS_TIMEZONE", "UTC")
    monkeypatch.setenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

    exit_code = main(["status"])

    output = capsys.readouterr().out
    assert "Second Brain operational status" in output
    assert exit_code in (0, 1)


# ---------------------------------------------------------------------------
# local-full runtime lifecycle — STARTING → RUNNING → STOPPED
# ---------------------------------------------------------------------------

_LOCAL_FULL_ENV = {
    "CAPTURE_PROCESSING_MODE": "local-full",
    "DISCORD_BOT_TOKEN": "discord-token",
    "DISCORD_GUILD_ID": "100",
    "DISCORD_CAPTURE_CHANNEL_ID": "200",
    "DISCORD_ALLOWED_USER_ID": "300",
    "GEMINI_API_KEY": "gemini-key",
    "GEMINI_MODEL": "gemini-test",
    "CLASSIFICATION_CONFIDENCE_THRESHOLD": "0.75",
    "CLASSIFIER_WORKER_COUNT": "1",
    "CLASSIFIER_QUEUE_MAXSIZE": "100",
    "VAULT_PATH": "/tmp/test-vault",
    "LEDGER_PATH": "",           # overridden per-test
    "STARTUP_RECONCILE_LIMIT": "100",
    "CAPTURE_SERVICE_INTERNAL_TOKEN": "x" * 32,
    "CAPTURE_API_HOST": "127.0.0.1",
    "CAPTURE_API_PORT": "8000",
}


def _set_local_full_env(monkeypatch, tmp_path):
    for key in _LOCAL_FULL_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in _LOCAL_FULL_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))


async def _sleep_forever(*args, **kwargs):
    await asyncio.sleep(10)


async def _noop_async(*args, **kwargs):
    pass


def _make_local_full_stub(*, on_start=None, on_ready=None, on_stop=None):
    from unittest.mock import AsyncMock

    async def noop_periodic(client):
        await asyncio.sleep(10)

    return SimpleNamespace(
        attach_receipt_client=lambda c: None,
        close=lambda: None,
        handle_gateway_message=AsyncMock(),
        run_stale_lease_reaper_loop=_sleep_forever,
        run_periodic_reconciliation_loop=noop_periodic,
        startup_reconcile=AsyncMock(
            return_value=SimpleNamespace(seen=0, handled=0, ignored=0, warning=None)
        ),
        enqueue_unfinished_captures=AsyncMock(return_value=[]),
        record_capture_service_start=on_start or (lambda **kw: None),
        record_capture_service_ready=on_ready or (lambda **kw: True),
        record_capture_service_heartbeat=lambda **kw: True,
        record_capture_service_stop=on_stop or (lambda **kw: True),
    )


def _wire_local_full_stubs(monkeypatch, tmp_path, stub, *, on_ready_callback=None):
    import secondbrain.app as app_module

    monkeypatch.setattr(app_module.CaptureService, "open", lambda *a, **kw: stub)
    monkeypatch.setattr(app_module, "VaultWriter", lambda path: SimpleNamespace())
    monkeypatch.setattr(
        app_module, "InternalApiServer",
        lambda *a, **kw: SimpleNamespace(serve=_sleep_forever, stop=_noop_async),
    )
    monkeypatch.setattr(app_module, "run_capture_worker", lambda **kw: _sleep_forever())

    class FakeClient:
        def __init__(self):
            self._on_ready = on_ready_callback

        async def start(self, token):
            if self._on_ready:
                await self._on_ready()
            await asyncio.sleep(10)

        async def close(self):
            pass

    fake_client = FakeClient()

    def fake_create_discord_client(handle, on_ready_callback=None):
        fake_client._on_ready = on_ready_callback
        return fake_client

    monkeypatch.setattr(app_module, "create_discord_client", fake_create_discord_client)

    _set_local_full_env(monkeypatch, tmp_path)


@pytest.mark.asyncio
async def test_local_full_runtime_records_starting_state(monkeypatch, tmp_path):
    """initialize_capture_service_lifecycle is called before run_service_runtime, so
    record_capture_service_start fires before Discord on_ready."""
    import secondbrain.app as app_module
    from contextlib import suppress

    start_calls = []
    stub = _make_local_full_stub(on_start=lambda **kw: start_calls.append(kw))

    startup_holder = []

    async def spy_run_service_runtime(*, startup, api_task, discord_task, **kw):
        startup_holder.append(startup)
        api_task.cancel()
        discord_task.cancel()
        raise asyncio.CancelledError()

    monkeypatch.setattr(app_module, "run_service_runtime", spy_run_service_runtime)
    _wire_local_full_stubs(monkeypatch, tmp_path, stub)

    from secondbrain.config import Settings as _Settings
    settings = _Settings()

    with suppress(asyncio.CancelledError):
        await app_module.run_local_full_runtime(settings)

    assert len(start_calls) == 1, "record_capture_service_start must fire exactly once"
    assert "instance_id" in start_calls[0]


@pytest.mark.asyncio
async def test_local_full_runtime_starts_heartbeat_before_discord_ready(monkeypatch, tmp_path):
    """Heartbeat task is created by initialize_capture_service_lifecycle, which runs
    before the Discord client task is even created."""
    import secondbrain.app as app_module
    from contextlib import suppress

    stub = _make_local_full_stub()
    startup_holder = []

    async def spy_run_service_runtime(*, startup, api_task, discord_task, **kw):
        startup_holder.append(startup)
        api_task.cancel()
        discord_task.cancel()
        raise asyncio.CancelledError()

    monkeypatch.setattr(app_module, "run_service_runtime", spy_run_service_runtime)
    _wire_local_full_stubs(monkeypatch, tmp_path, stub)

    from secondbrain.config import Settings as _Settings
    settings = _Settings()

    with suppress(asyncio.CancelledError):
        await app_module.run_local_full_runtime(settings)

    assert len(startup_holder) == 1
    startup = startup_holder[0]
    assert startup.heartbeat_task is not None, (
        "heartbeat_task must be set before run_service_runtime is entered"
    )

    if not startup.heartbeat_task.done():
        startup.heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await startup.heartbeat_task


@pytest.mark.asyncio
async def test_local_full_runtime_records_running_after_worker_startup(monkeypatch, tmp_path):
    """record_capture_service_ready (→ RUNNING) is called only after start_once completes,
    which means the worker task is already running."""
    import secondbrain.app as app_module
    from contextlib import suppress

    ready_event = asyncio.Event()
    ready_calls = []

    def recording_ready(**kw):
        ready_calls.append(True)
        ready_event.set()
        return True

    stub = _make_local_full_stub(on_ready=recording_ready)
    _wire_local_full_stubs(monkeypatch, tmp_path, stub)

    async def spy_run_service_runtime(*, startup, api_task, discord_task, **kw):
        await ready_event.wait()
        # Worker is set by start_once before ready fires
        ready_calls.append(("worker_task_set", startup.worker_task is not None))
        api_task.cancel()
        discord_task.cancel()
        raise asyncio.CancelledError()

    monkeypatch.setattr(app_module, "run_service_runtime", spy_run_service_runtime)

    from secondbrain.config import Settings as _Settings
    settings = _Settings()

    with suppress(asyncio.CancelledError):
        await app_module.run_local_full_runtime(settings)

    assert True in ready_calls, "record_capture_service_ready must be called"
    assert ("worker_task_set", True) in ready_calls, (
        "worker_task must exist when record_capture_service_ready fires"
    )


@pytest.mark.asyncio
async def test_local_full_runtime_records_stopped_state_on_shutdown(monkeypatch, tmp_path):
    """record_capture_service_stop fires in run_service_runtime's finally block when
    the discord task exits normally (client.start returns instead of sleeping)."""
    import secondbrain.app as app_module
    from contextlib import suppress

    stop_calls = []

    def recording_stop(**kw):
        stop_calls.append(kw)
        return True

    stub = _make_local_full_stub(on_stop=recording_stop)

    class TerminatingClient:
        def __init__(self):
            self._on_ready = None

        async def start(self, token):
            # Fire on_ready then return immediately — makes discord_task complete
            if self._on_ready:
                await self._on_ready()

        async def close(self):
            pass

    fake_client = TerminatingClient()

    def fake_create_discord_client(handle, on_ready_callback=None):
        fake_client._on_ready = on_ready_callback
        return fake_client

    monkeypatch.setattr(app_module.CaptureService, "open", lambda *a, **kw: stub)
    monkeypatch.setattr(app_module, "VaultWriter", lambda path: SimpleNamespace())
    monkeypatch.setattr(
        app_module, "InternalApiServer",
        lambda *a, **kw: SimpleNamespace(serve=_sleep_forever, stop=_noop_async),
    )
    monkeypatch.setattr(app_module, "run_capture_worker", lambda **kw: _sleep_forever())
    monkeypatch.setattr(app_module, "create_discord_client", fake_create_discord_client)

    _set_local_full_env(monkeypatch, tmp_path)
    from secondbrain.config import Settings as _Settings
    settings = _Settings()

    await app_module.run_local_full_runtime(settings)

    assert len(stop_calls) == 1, "record_capture_service_stop must fire exactly once on shutdown"
    assert "instance_id" in stop_calls[0]


# ---------------------------------------------------------------------------
# run_manual_retry CLI path
# ---------------------------------------------------------------------------

def test_manual_retry_unknown_capture_reports_not_found_without_traceback(
    tmp_path, monkeypatch, capsys
):
    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)

    monkeypatch.setattr("secondbrain.app.Settings", lambda: settings)
    monkeypatch.setattr("secondbrain.app.CaptureService.open", lambda s: service)

    run_manual_retry("SB-20260609-9999")

    captured = capsys.readouterr()
    assert "manual retry rejected: capture not found" in captured.err
    assert "Traceback" not in captured.err
    assert "CaptureNotFoundError" not in captured.err


def test_manual_retry_known_nonfailed_capture_reports_invalid_state(
    tmp_path, monkeypatch, capsys
):
    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)

    # Insert a capture — it starts in RECEIVED state, not FAILED
    now = datetime.now(UTC)
    ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="test note",
        received_at=now,
    )
    captures = ledger.captures_by_status("RECEIVED")
    capture_id = captures[0].capture_id

    monkeypatch.setattr("secondbrain.app.Settings", lambda: settings)
    monkeypatch.setattr("secondbrain.app.CaptureService.open", lambda s: service)

    run_manual_retry(capture_id)

    captured = capsys.readouterr()
    assert "manual retry rejected: capture is not in terminal FAILED state" in captured.err


def test_manual_retry_success_cli_path(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)

    # Insert a capture and drive it to terminal FAILED
    now = datetime.now(UTC)
    ledger.insert_accepted_capture(
        discord_message_id="1001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="test note",
        received_at=now,
    )
    claimed = ledger.claim_due_deliveries(
        now=now, lease_until=now + timedelta(minutes=1), batch_size=10
    )
    capture_id = claimed[0].capture_id
    ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=1,
        now=now,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=1,
        base_delay_seconds=10,
        max_delay_seconds=300,
    )

    monkeypatch.setattr("secondbrain.app.Settings", lambda: settings)
    monkeypatch.setattr("secondbrain.app.CaptureService.open", lambda s: service)

    result = run_manual_retry(capture_id)

    output = capsys.readouterr().out
    assert result is True
    assert f"manual retry queued: {capture_id}" in output


# ---------------------------------------------------------------------------
# main() exit codes for retry command
# ---------------------------------------------------------------------------

def _make_failed_capture(ledger, tmp_path):
    now = datetime.now(UTC)
    ledger.insert_accepted_capture(
        discord_message_id="2001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="test note",
        received_at=now,
    )
    claimed = ledger.claim_due_deliveries(
        now=now, lease_until=now + timedelta(minutes=1), batch_size=10
    )
    capture_id = claimed[0].capture_id
    ledger.schedule_retry(
        capture_id=capture_id,
        delivery_attempt=1,
        now=now,
        error_type="TimeoutError",
        reason_type="webhook_failure",
        max_attempts=1,
        base_delay_seconds=10,
        max_delay_seconds=300,
    )
    return capture_id


def test_main_retry_success_returns_zero(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)
    capture_id = _make_failed_capture(ledger, tmp_path)

    monkeypatch.setattr("secondbrain.app.Settings", lambda: settings)
    monkeypatch.setattr("secondbrain.app.CaptureService.open", lambda s: service)

    exit_code = main(["retry", capture_id])

    assert exit_code == 0
    assert f"manual retry queued: {capture_id}" in capsys.readouterr().out


def test_main_retry_unknown_capture_returns_nonzero(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)

    monkeypatch.setattr("secondbrain.app.Settings", lambda: settings)
    monkeypatch.setattr("secondbrain.app.CaptureService.open", lambda s: service)

    exit_code = main(["retry", "SB-DOES-NOT-EXIST"])

    assert exit_code == 1
    assert "capture not found" in capsys.readouterr().err


def test_main_retry_known_nonfailed_capture_returns_nonzero(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    service = CaptureService(settings=settings, ledger=ledger)

    now = datetime.now(UTC)
    ledger.insert_accepted_capture(
        discord_message_id="3001",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="non-failed note",
        received_at=now,
    )
    captures = ledger.captures_by_status("RECEIVED")
    capture_id = captures[0].capture_id

    monkeypatch.setattr("secondbrain.app.Settings", lambda: settings)
    monkeypatch.setattr("secondbrain.app.CaptureService.open", lambda s: service)

    exit_code = main(["retry", capture_id])

    assert exit_code == 1
    assert "not in terminal FAILED state" in capsys.readouterr().err
