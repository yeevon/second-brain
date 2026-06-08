import pytest

from secondbrain.ledger import FILED

from tests.fakes.discord import FakeDiscordMessage
from tests.support import drain_worker, event_types, ingest_if_allowed, ledger_rows, make_app, note_files


@pytest.mark.asyncio
async def test_receipt_edit_failure_sends_one_replacement_receipt(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_classifier,
    fake_channel,
    fake_discord,
    capture_handler,
):
    fake_channel.fail_receipt_edit = True
    message = FakeDiscordMessage(
        message_id=1201,
        channel=fake_channel,
        content="Review reconnect handling.",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, fake_classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    capture = ledger.get_capture(ledger_rows(ledger)[0]["capture_id"])

    assert capture.status == FILED
    assert len(note_files(test_settings.vault_path)) == 1
    assert fake_channel.edit_attempts == 1
    assert len(fake_channel.replacement_receipts) == 1
    assert fake_channel.replacement_receipts[0][1].startswith(f"✅ {capture.capture_id} filed.")
    assert capture.receipt_message_id == str(fake_channel.replacement_receipts[0][0])
    assert event_types(ledger, capture.capture_id).count("RECEIPT_REPLACED") == 1
