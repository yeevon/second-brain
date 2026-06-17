import pytest

from secondbrain.app import create_capture_handler
from secondbrain.ledger import FILED
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID, reconcile_discord_history
from secondbrain.vault_writer import VaultWriter
from secondbrain.worker import CaptureQueue, enqueue_capture_ids, unfinished_capture_ids

from tests.fakes.classifier import FakeClassifier
from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient, FakeDiscordMessage
from tests.support import drain_worker, ledger_rows, make_app, note_files


@pytest.mark.asyncio
async def test_startup_catchup_recovers_missed_message_exactly_once(
    test_settings,
    ledger,
):
    queue = CaptureQueue()
    missed = FakeDiscordMessage(message_id=1101, content="Missed while the bot was offline.")
    channel = FakeDiscordChannel([missed])
    discord = FakeDiscordClient(channel)
    handler = create_capture_handler(test_settings, ledger, queue, enqueue_captures=False)
    classifier = FakeClassifier()
    app = make_app(test_settings, ledger, queue, VaultWriter(test_settings.vault_path), classifier, discord)

    first = await reconcile_discord_history(
        client=discord,
        settings=test_settings,
        ledger=ledger,
        handle_capture=handler,
    )
    await enqueue_capture_ids(unfinished_capture_ids(ledger), queue)
    await drain_worker(app)
    second = await reconcile_discord_history(
        client=discord,
        settings=test_settings,
        ledger=ledger,
        handle_capture=handler,
    )
    await enqueue_capture_ids(unfinished_capture_ids(ledger), queue)
    await drain_worker(app)

    assert first.handled == 1
    assert second.seen == 0
    assert len(ledger_rows(ledger)) == 1
    assert ledger_rows(ledger)[0]["status"] == FILED
    assert len(note_files(test_settings.vault_path)) == 1
    assert classifier.call_count == 1
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1101"
    assert len(channel.sent_receipts) == 1
