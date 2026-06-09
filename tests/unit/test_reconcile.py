from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger
from secondbrain.reconcile import (
    LAST_RECONCILED_MESSAGE_ID,
    _record_periodic_failure_best_effort,
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
async def test_periodic_failure_persists_safe_warning_state(tmp_path):
    """A scan failure must set periodic_reconcile_last_warning to 'scan_failed'."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    channel = FakeFullChannel([make_message(1001, content="Trouble.")])

    async def always_failing(message):
        raise RuntimeError("scan error")

    await _run_one_periodic_pass(
        client=FakeClientWithChannel(channel), settings=settings, ledger=ledger,
        handle_capture=always_failing,
    )

    assert ledger.get_system_state("periodic_reconcile_last_warning") == "scan_failed"


@pytest.mark.asyncio
async def test_periodic_failure_persists_error_type_without_exception_message(tmp_path):
    """Failure stores the exception class name only, not the exception message."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    settings = make_settings()
    channel = FakeFullChannel([make_message(1001, content="Trouble.")])

    async def value_error(message):
        raise ValueError("secret internal details")

    await _run_one_periodic_pass(
        client=FakeClientWithChannel(channel), settings=settings, ledger=ledger,
        handle_capture=value_error,
    )

    error_type = ledger.get_system_state("periodic_reconcile_last_error_type")
    assert error_type == "ValueError"
    assert "secret internal details" not in (error_type or "")


@pytest.mark.asyncio
async def test_periodic_loop_survives_metric_write_failure(tmp_path, monkeypatch):
    """A metric write failure escaping _run_one_periodic_pass must not kill the loop."""
    passes = []

    async def failing_then_succeeding(**kwargs):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("SQLite busy — metric write failed after scan")

    monkeypatch.setattr("secondbrain.reconcile._run_one_periodic_pass", failing_then_succeeding)

    settings = make_settings(periodic_reconcile_interval_seconds=0)
    channel = FakeFullChannel([])
    task = asyncio.create_task(
        run_periodic_reconciliation(
            client=FakeClientWithChannel(channel),
            settings=settings,
            ledger=Ledger(tmp_path / "ledger.sqlite3"),
            handle_capture=lambda msg: None,
        )
    )

    for _ in range(40):
        await asyncio.sleep(0)
        if len(passes) >= 2:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(passes) >= 2


@pytest.mark.asyncio
async def test_periodic_loop_continues_after_unexpected_pass_failure(tmp_path, monkeypatch):
    """Loop continues normally even when _run_one_periodic_pass raises unexpectedly."""
    success = asyncio.Event()
    passes = []

    async def controlled(**kwargs):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("unexpected first-pass failure")
        success.set()

    monkeypatch.setattr("secondbrain.reconcile._run_one_periodic_pass", controlled)

    settings = make_settings(periodic_reconcile_interval_seconds=0)
    task = asyncio.create_task(
        run_periodic_reconciliation(
            client=FakeClientWithChannel(FakeFullChannel([])),
            settings=settings,
            ledger=Ledger(tmp_path / "ledger.sqlite3"),
            handle_capture=lambda msg: None,
        )
    )

    await asyncio.wait_for(success.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(passes) >= 2


@pytest.mark.asyncio
async def test_outer_loop_failure_persists_safe_failure_state(tmp_path, monkeypatch):
    """An exception escaping _run_one_periodic_pass must update the persisted failure counters."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    passes = []

    async def fail_once(**kwargs):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("metric write exploded")

    monkeypatch.setattr("secondbrain.reconcile._run_one_periodic_pass", fail_once)

    settings = make_settings(periodic_reconcile_interval_seconds=0)
    task = asyncio.create_task(
        run_periodic_reconciliation(
            client=FakeClientWithChannel(FakeFullChannel([])),
            settings=settings,
            ledger=ledger,
            handle_capture=lambda msg: None,
        )
    )

    for _ in range(40):
        await asyncio.sleep(0)
        if len(passes) >= 2:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ledger.get_system_state("periodic_reconcile_last_warning") == "scan_failed"
    assert ledger.get_system_state("periodic_reconcile_last_error_type") == "RuntimeError"
    assert int(ledger.get_system_state("periodic_reconcile_failures_total") or 0) >= 1


@pytest.mark.asyncio
async def test_failure_state_write_failure_does_not_kill_periodic_loop(tmp_path, monkeypatch):
    """A failure inside _record_periodic_failure_best_effort must not propagate to the loop."""
    successes = asyncio.Event()
    passes = []

    async def fail_first_pass(**kwargs):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("inner pass failed")
        successes.set()

    monkeypatch.setattr("secondbrain.reconcile._run_one_periodic_pass", fail_first_pass)

    class BrokenLedger:
        def increment_system_counter(self, *a, **kw):
            raise OSError("disk full — metric write failed")

        def set_system_state(self, *a, **kw):
            raise OSError("disk full — state write failed")

    # Verify helper does not raise even when every write fails
    _record_periodic_failure_best_effort(ledger=BrokenLedger(), exc=RuntimeError("boom"))

    settings = make_settings(periodic_reconcile_interval_seconds=0)
    task = asyncio.create_task(
        run_periodic_reconciliation(
            client=FakeClientWithChannel(FakeFullChannel([])),
            settings=settings,
            ledger=Ledger(tmp_path / "ledger.sqlite3"),
            handle_capture=lambda msg: None,
        )
    )

    await asyncio.wait_for(successes.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(passes) >= 2


@pytest.mark.asyncio
async def test_on_ready_starts_only_one_periodic_reconcile_task(tmp_path, monkeypatch):
    """ensure_periodic_reconciliation_task does not replace a running task."""
    starts = []

    async def mock_loop(self, client):
        starts.append(1)
        await asyncio.Event().wait()

    monkeypatch.setattr(CaptureService, "run_periodic_reconciliation_loop", mock_loop)

    from secondbrain.app import ensure_periodic_reconciliation_task

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
    startup = SimpleNamespace(periodic_task=None)
    client = FakeClient([])

    ensure_periodic_reconciliation_task(startup=startup, client=client, capture_service=service)
    await asyncio.sleep(0)
    first_task = startup.periodic_task
    assert first_task is not None
    assert len(starts) == 1

    ensure_periodic_reconciliation_task(startup=startup, client=client, capture_service=service)

    assert startup.periodic_task is first_task
    assert len(starts) == 1

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
