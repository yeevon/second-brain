import pytest

from secondbrain.capture_service import CaptureService
from secondbrain.ledger import FILED
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID
from secondbrain.vault_writer import VaultWriter
from secondbrain.worker import CaptureQueue

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
    classifier = FakeClassifier()
    service = CaptureService(
        settings=test_settings,
        ledger=ledger,
        notify_capture=queue.enqueue,
        receipt_client=discord,
    )
    app = make_app(test_settings, ledger, queue, VaultWriter(test_settings.vault_path), classifier, discord)
    app.capture_service = service

    first = await service.startup_reconcile(discord)
    await service.enqueue_unfinished_captures()
    await drain_worker(app)
    second = await service.startup_reconcile(discord)
    await service.enqueue_unfinished_captures()
    await drain_worker(app)

    assert first.handled == 1
    assert second.seen == 0
    assert len(ledger_rows(ledger)) == 1
    assert ledger_rows(ledger)[0]["status"] == FILED
    assert len(note_files(test_settings.vault_path)) == 1
    assert classifier.call_count == 1
    assert ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID) == "1101"
    assert len(channel.sent_receipts) == 1
