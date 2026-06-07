from types import SimpleNamespace

import pytest

from secondbrain.ledger import Ledger
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID, reconcile_discord_history
from secondbrain.worker import CaptureQueue
from secondbrain.app import create_capture_handler


def make_settings(**overrides):
    data = {
        "discord_guild_id": 100,
        "discord_capture_channel_id": 200,
        "discord_allowed_user_id": 300,
        "startup_reconcile_limit": 10,
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
        channel=SimpleNamespace(id=channel_id),
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
    handle_capture = create_capture_handler(settings, ledger, queue)

    result = await reconcile_discord_history(
        client=client,
        settings=settings,
        ledger=ledger,
        handle_capture=handle_capture,
    )

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)
    assert result.seen == 1
    assert result.handled == 1
    assert result.ignored == 0
    assert capture.discord_message_id == "1001"
    assert capture.raw_text == "Recovered note."
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"


@pytest.mark.asyncio
async def test_reconcile_advances_high_water_for_ignored_messages(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    queue = CaptureQueue()
    settings = make_settings()
    client = FakeClient([make_message(1001, author_bot=True)])
    handle_capture = create_capture_handler(settings, ledger, queue)

    result = await reconcile_discord_history(
        client=client,
        settings=settings,
        ledger=ledger,
        handle_capture=handle_capture,
    )

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
    handle_capture = create_capture_handler(settings, ledger, queue)

    result = await reconcile_discord_history(
        client=client,
        settings=settings,
        ledger=ledger,
        handle_capture=handle_capture,
    )

    capture_id = await queue.get()
    capture = ledger.get_capture(capture_id)
    assert result.seen == 1
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
    handle_capture = create_capture_handler(settings, ledger, queue)

    result = await reconcile_discord_history(
        client=client,
        settings=settings,
        ledger=ledger,
        handle_capture=handle_capture,
    )

    assert result.warning is not None
    assert result.seen == 1
    assert result.handled == 1
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1001"


class FakeClient:
    def __init__(self, messages):
        self.channel = FakeChannel(messages)

    def get_channel(self, channel_id):
        return self.channel


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
