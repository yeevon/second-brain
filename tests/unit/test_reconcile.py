from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger
from secondbrain.reconcile import (
    LAST_RECONCILED_MESSAGE_ID,
    _run_one_periodic_pass,
    run_periodic_reconciliation,
)
from secondbrain.worker import CaptureQueue


def make_settings(**overrides):
    data = {
        "discord_guild_id": 100,
        "discord_capture_channel_id": 200,
        "discord_allowed_user_id": 300,
        "startup_reconcile_limit": 10,
        "periodic_reconcile_limit": 10,
        "periodic_reconcile_interval_seconds": 60,
        "classifier_queue_maxsize": 10,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_message(
    message_id,
    *,
    guild_id=100,
    channel_id=200,
    author_id=300,
    author_bot=False,
    webhook_id=None,
    content="capture this",
):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=guild_id),
        channel=FakeMessageChannel(channel_id),
        author=SimpleNamespace(id=author_id, bot=author_bot),
        webhook_id=webhook_id,
        content=content,
        attachments=[],
    )


@pytest.mark.asyncio
async def test_reconcile_fetches_history_and_uses_capture_handler(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    settings = make_settings()
    client = FakeClient([make_message(1001, content="Recovered note.")])
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)

    result = await service.startup_reconcile(client)

    assert result.seen == 1
    assert result.handled == 1
    assert result.ignored == 0
    assert queue.qsize() == 0
    capture = ledger.captures_by_status("RECEIVED")[0]
    assert capture.discord_message_id == "1001"
    assert capture.raw_text == "Recovered note."
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"


@pytest.mark.asyncio
async def test_reconcile_advances_high_water_for_ignored_messages(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    settings = make_settings()
    client = FakeClient([make_message(1001, author_bot=True)])
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)

    result = await service.startup_reconcile(client)

    assert result.seen == 1
    assert result.handled == 0
    assert result.ignored == 1
    assert queue.qsize() == 0
    assert ledger.status_counts() == {}
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"


@pytest.mark.asyncio
async def test_reconcile_uses_last_reconciled_message_id_as_history_after(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.set_system_state(LAST_RECONCILED_MESSAGE_ID, "1001")
    queue = CaptureQueue()
    settings = make_settings()
    client = FakeClient(
        [
            make_message(1001, content="Already reconciled."),
            make_message(1002, content="New capture."),
        ]
    )
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)

    result = await service.startup_reconcile(client)

    assert result.seen == 1
    assert queue.qsize() == 0
    capture = ledger.captures_by_status("RECEIVED")[0]
    assert capture.discord_message_id == "1002"
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1002"


@pytest.mark.asyncio
async def test_reconcile_warns_when_limit_is_exceeded(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    settings = make_settings(startup_reconcile_limit=1)
    client = FakeClient(
        [
            make_message(1001, content="First."),
            make_message(1002, content="Second."),
        ]
    )
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)

    result = await service.startup_reconcile(client)

    assert result.warning is not None
    assert result.seen == 1
    assert result.handled == 1
    assert queue.qsize() == 0
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"


class FakeClient:
    def __init__(self, messages):
        self.channel = FakeChannel(messages)

    def get_channel(self, channel_id):
        return self.channel


class FakeMessageChannel:
    def __init__(self, channel_id):
        self.id = channel_id

    async def send(self, content):
        return SimpleNamespace(id=9001)


class FakeChannel:
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


class FakeFullChannel(FakeChannel):
    """FakeChannel extended with send() for warning-delivery tests."""

    def __init__(self, messages):
        super().__init__(messages)
        self.sent_contents = []

    async def send(self, content):
        self.sent_contents.append(content)
        return SimpleNamespace(id=9001)


class FakeClientWithChannel:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


# ---------------------------------------------------------------------------
# SB-106 — marker ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_gateway_does_not_advance_reconcile_marker(tmp_path):
    """handle_gateway_message must never advance the history-scan cursor."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    settings = make_settings()
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)

    await service.handle_gateway_message(make_message(1002, content="Live note."))

    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) is None
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_history_scan_advances_marker_for_both_handled_and_ignored(tmp_path):
    """reconcile_discord_history advances the marker for every message, including ignored ones."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    client = FakeClient([
        make_message(1001, author_bot=True),
        make_message(1002, content="Real note."),
    ])
    service = CaptureService(settings=settings, ledger=ledger)

    result = await service.startup_reconcile(client)

    assert result.ignored == 1
    assert result.handled == 1
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1002"


# ---------------------------------------------------------------------------
# SB-106 — periodic pass metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_pass_increments_runs_total(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    client = FakeClient([])
    service = CaptureService(settings=settings, ledger=ledger)

    await _run_one_periodic_pass(
        client=client, settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )
    await _run_one_periodic_pass(
        client=client, settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )

    assert ledger.get_system_state("periodic_reconcile_runs_total") == "2"
    assert ledger.get_system_state("periodic_reconcile_last_success_at") is not None


@pytest.mark.asyncio
async def test_periodic_pass_counts_new_messages_as_recovered(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    client = FakeClient([make_message(1001, content="New note.")])
    service = CaptureService(settings=settings, ledger=ledger)

    await _run_one_periodic_pass(
        client=client, settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )

    assert ledger.get_system_state("periodic_reconcile_recovered_total") == "1"
    assert ledger.get_system_state("periodic_reconcile_duplicates_total") == "0"
    assert ledger.get_system_state("periodic_reconcile_last_recovered_count") == "1"


@pytest.mark.asyncio
async def test_periodic_pass_counts_existing_messages_as_duplicates(tmp_path):
    """A message already in the ledger (captured via gateway) counts as a duplicate."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    queue = CaptureQueue()
    service = CaptureService(settings=settings, ledger=ledger, notify_capture=queue.enqueue)
    msg = make_message(1001, content="Already captured.")

    await service.handle_gateway_message(msg)
    await queue.get()

    client = FakeClient([msg])
    await _run_one_periodic_pass(
        client=client, settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=True),
    )

    assert ledger.get_system_state("periodic_reconcile_duplicates_total") == "1"
    assert ledger.get_system_state("periodic_reconcile_recovered_total") == "0"
    assert queue.qsize() == 0


# ---------------------------------------------------------------------------
# SB-106 — bounded scan and limit-exceeded behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_pass_uses_scan_limit_and_stops_at_boundary(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings(periodic_reconcile_limit=1)
    client = FakeClient([
        make_message(1001, content="First."),
        make_message(1002, content="Second."),
    ])
    service = CaptureService(settings=settings, ledger=ledger)

    await _run_one_periodic_pass(
        client=client, settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )

    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"
    assert ledger.total_captures() == 1


@pytest.mark.asyncio
async def test_periodic_pass_sets_limit_exceeded_counter_and_warning(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings(periodic_reconcile_limit=1)
    channel = FakeFullChannel([
        make_message(1001, content="First."),
        make_message(1002, content="Second."),
    ])
    service = CaptureService(settings=settings, ledger=ledger)

    await _run_one_periodic_pass(
        client=FakeClientWithChannel(channel), settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )

    assert ledger.get_system_state("periodic_reconcile_limit_exceeded_total") == "1"
    assert ledger.get_system_state("periodic_reconcile_last_warning") == "scan_limit_reached"
    assert len(channel.sent_contents) == 1
    assert "scan limit" in channel.sent_contents[0].lower()


@pytest.mark.asyncio
async def test_periodic_pass_continues_from_previous_batch_marker(tmp_path):
    """Second pass continues from where the first batch stopped."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings(periodic_reconcile_limit=1)
    channel = FakeFullChannel([
        make_message(1001, content="First."),
        make_message(1002, content="Second."),
    ])
    client = FakeClientWithChannel(channel)
    service = CaptureService(settings=settings, ledger=ledger)

    await _run_one_periodic_pass(
        client=client, settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )
    await _run_one_periodic_pass(
        client=client, settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )

    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1002"
    assert ledger.total_captures() == 2
    assert ledger.get_system_state("periodic_reconcile_recovered_total") == "2"


# ---------------------------------------------------------------------------
# SB-106 — structured log output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_pass_logs_completion_metadata_without_message_content(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    client = FakeClient([make_message(1001, content="Secret note content.")])
    service = CaptureService(settings=settings, ledger=ledger)

    await _run_one_periodic_pass(
        client=client, settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )

    output = capsys.readouterr().out
    completion_logs = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("event") == "discord_reconcile_completed":
            completion_logs.append(data)

    assert len(completion_logs) == 1
    assert completion_logs[0]["seen"] == 1
    assert completion_logs[0]["recovered"] == 1
    assert "Secret note content." not in output


# ---------------------------------------------------------------------------
# SB-106 — warnings and failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_pass_sends_visible_warning_on_scan_failure(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    channel = FakeFullChannel([make_message(1001, content="Trouble message.")])

    async def always_failing(message):
        raise RuntimeError("simulated capture failure")

    await _run_one_periodic_pass(
        client=FakeClientWithChannel(channel), settings=settings, ledger=ledger,
        handle_capture=always_failing,
    )

    assert ledger.get_system_state("periodic_reconcile_failures_total") == "1"
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) is None
    assert len(channel.sent_contents) == 1
    assert "reconciliation failed" in channel.sent_contents[0].lower()


@pytest.mark.asyncio
async def test_periodic_pass_sends_visible_warning_when_limit_exceeded(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings(periodic_reconcile_limit=1)
    channel = FakeFullChannel([
        make_message(1001, content="First."),
        make_message(1002, content="Second."),
    ])
    service = CaptureService(settings=settings, ledger=ledger)

    await _run_one_periodic_pass(
        client=FakeClientWithChannel(channel), settings=settings, ledger=ledger,
        handle_capture=service.make_capture_handler(notify_downstream=False),
    )

    assert len(channel.sent_contents) == 1
    assert "backlog" in channel.sent_contents[0].lower()


@pytest.mark.asyncio
async def test_warning_send_failure_does_not_stop_periodic_loop(tmp_path, capsys):
    """Warning delivery failure is logged but never propagated."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()

    class FailingSendChannel(FakeFullChannel):
        async def send(self, content):
            raise RuntimeError("Discord unavailable")

    channel = FailingSendChannel([make_message(1001, content="Trouble.")])

    async def always_failing(message):
        raise RuntimeError("capture failed")

    await _run_one_periodic_pass(
        client=FakeClientWithChannel(channel), settings=settings, ledger=ledger,
        handle_capture=always_failing,
    )

    output = capsys.readouterr().out
    logged_events = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            logged_events.append(json.loads(line).get("event", ""))
        except json.JSONDecodeError:
            pass
    assert "reconcile_warning_delivery_failed" in logged_events
    assert ledger.get_system_state("periodic_reconcile_failures_total") == "1"


@pytest.mark.asyncio
async def test_periodic_pass_never_raises_so_the_loop_always_retries(tmp_path):
    """_run_one_periodic_pass swallows all exceptions; two consecutive failures both complete."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    channel = FakeFullChannel([make_message(1001, content="Trouble.")])

    async def always_failing(message):
        raise RuntimeError("always fails")

    await _run_one_periodic_pass(
        client=FakeClientWithChannel(channel), settings=settings, ledger=ledger,
        handle_capture=always_failing,
    )
    await _run_one_periodic_pass(
        client=FakeClientWithChannel(channel), settings=settings, ledger=ledger,
        handle_capture=always_failing,
    )

    assert ledger.get_system_state("periodic_reconcile_failures_total") == "2"
    assert ledger.get_system_state("periodic_reconcile_runs_total") == "2"


# ---------------------------------------------------------------------------
# SB-106 — loop behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_reconcile_loop_does_not_run_concurrent_passes(tmp_path, monkeypatch):
    """Even with interval=0 the loop never runs two passes at the same time."""
    concurrent = 0
    max_concurrent = 0
    first_started = asyncio.Event()
    let_finish = asyncio.Event()

    async def controlled_pass(**kwargs):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        first_started.set()
        await let_finish.wait()
        concurrent -= 1

    monkeypatch.setattr("secondbrain.reconcile._run_one_periodic_pass", controlled_pass)

    settings = make_settings(periodic_reconcile_interval_seconds=0)
    task = asyncio.create_task(
        run_periodic_reconciliation(
            client=FakeClient([]),
            settings=settings,
            ledger=Ledger(tmp_path / "ledger.sqlite3"),
            handle_capture=lambda msg: None,
        )
    )

    await first_started.wait()
    for _ in range(10):
        await asyncio.sleep(0)

    assert max_concurrent == 1

    let_finish.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_on_ready_starts_only_one_periodic_reconcile_task(tmp_path, monkeypatch):
    """The periodic_task guard prevents a second task being created when the first is running."""
    starts = []

    async def mock_periodic(**kwargs):
        starts.append(1)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("secondbrain.app.run_periodic_reconciliation", mock_periodic)

    from secondbrain.app import LocalWorkerStartup, run_periodic_reconciliation as rpr
    from secondbrain.vault_writer import VaultWriter

    settings_ns = SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        periodic_reconcile_limit=10,
        periodic_reconcile_interval_seconds=60,
        classifier_queue_maxsize=10,
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
    )
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    service = CaptureService(settings=settings_ns, ledger=ledger, notify_capture=queue.enqueue)
    startup = LocalWorkerStartup(
        settings=settings_ns,
        capture_service=service,
        queue=queue,
        vault_writer=VaultWriter(tmp_path / "vault"),
    )

    async def simulate_on_ready(client):
        result = await startup.start_once(client)
        if result is None:
            return
        if startup.periodic_task is None or startup.periodic_task.done():
            startup.periodic_task = asyncio.create_task(mock_periodic())

    client = FakeClient([])
    await simulate_on_ready(client)
    await asyncio.sleep(0)
    first_task = startup.periodic_task
    assert first_task is not None

    await simulate_on_ready(client)

    assert startup.periodic_task is first_task
    assert len(starts) == 1

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    startup.worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup.worker_task
