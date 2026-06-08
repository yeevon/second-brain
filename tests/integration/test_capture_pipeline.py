import pytest

from secondbrain.ledger import FAILED, FILED, INBOX, RECEIVED, REJECTED_SENSITIVE

from tests.fakes.classifier import FakeClassifier
from tests.fakes.discord import FakeDiscordAuthor, FakeDiscordMessage
from tests.fakes.vault_writer import FakeFailingVaultWriter
from tests.support import (
    audit_events,
    drain_worker,
    event_types,
    ingest_if_allowed,
    ledger_rows,
    make_app,
    note_files,
    sqlite_dump,
)


@pytest.mark.asyncio
async def test_normal_message_creates_one_capture_and_one_note(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_classifier,
    fake_channel,
    fake_discord,
    capture_handler,
):
    message = FakeDiscordMessage(
        message_id=1001,
        channel=fake_channel,
        content="Review reconnect handling.",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, fake_classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    rows = ledger_rows(ledger)
    capture = ledger.get_capture(rows[0]["capture_id"])
    notes = note_files(test_settings.vault_path)
    markdown = notes[0].read_text(encoding="utf-8")

    assert len(rows) == 1
    assert capture.raw_text == "Review reconnect handling."
    assert capture.status == FILED
    assert len(notes) == 1
    assert notes[0].relative_to(test_settings.vault_path).as_posix().startswith("20_projects/halo/")
    assert f'capture_id: "{capture.capture_id}"' in markdown
    assert 'source_message_id: "1001"' in markdown
    assert audit_events(test_settings.vault_path) == [
        {
            "capture_id": capture.capture_id,
            "event": "FILED",
            "path": capture.derived_note_path,
            "timestamp": audit_events(test_settings.vault_path)[0]["timestamp"],
        }
    ]
    assert fake_channel.sent_receipts == [
        (9001, f"⏳ {capture.capture_id} received.\nYour note is saved. Processing…")
    ]
    assert fake_channel.edited_receipts == [
        (
            9001,
            f"✅ {capture.capture_id} filed.\n"
            "Location: 20_projects / halo\n"
            "Type: task\n"
            "Tags: telemetry, websocket",
        )
    ]


@pytest.mark.asyncio
async def test_duplicate_discord_event_is_idempotent(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_classifier,
    fake_channel,
    fake_discord,
    capture_handler,
):
    message = FakeDiscordMessage(
        message_id=1001,
        channel=fake_channel,
        content="Review reconnect handling.",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, fake_classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    rows = ledger_rows(ledger)
    capture = ledger.get_capture(rows[0]["capture_id"])

    assert len(rows) == 1
    assert rows[0]["discord_message_id"] == "1001"
    assert len(note_files(test_settings.vault_path)) == 1
    assert fake_classifier.call_count == 1
    assert event_types(ledger, capture.capture_id).count("CAPTURE_FILED") == 1
    assert len(fake_channel.sent_receipts) == 1


@pytest.mark.asyncio
async def test_bot_authored_receipt_is_ignored(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_classifier,
    fake_channel,
    fake_discord,
    capture_handler,
):
    message = FakeDiscordMessage(
        message_id=1002,
        channel=fake_channel,
        author=FakeDiscordAuthor(bot=True),
        content="✅ SB-20260607-0001 filed.\nLocation: 20_projects / halo",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, fake_classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    assert ledger_rows(ledger) == []
    assert fake_classifier.call_count == 0
    assert note_files(test_settings.vault_path) == []
    assert fake_channel.sent_receipts == []


@pytest.mark.asyncio
async def test_secret_like_input_stores_redacted_rejection_only(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_classifier,
    fake_channel,
    fake_discord,
    capture_handler,
    capsys,
):
    secret = "TEST_ONLY_DO_NOT_USE_123456"
    message = FakeDiscordMessage(
        message_id=1003,
        channel=fake_channel,
        content=f"api_key={secret}",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, fake_classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    captured = capsys.readouterr()
    rows = ledger_rows(ledger)
    database_dump = sqlite_dump(ledger)

    assert len(rows) == 1
    assert rows[0]["status"] == REJECTED_SENSITIVE
    assert rows[0]["is_sensitive"] == 1
    assert rows[0]["raw_text"] is None
    assert rows[0]["redacted_text"] == "api_key=[REDACTED]"
    assert secret not in database_dump
    assert secret not in captured.out
    assert secret not in captured.err
    assert all(secret not in content for _id, content in fake_channel.sent_receipts)
    assert fake_classifier.call_count == 0
    assert note_files(test_settings.vault_path) == []
    assert len(fake_channel.sent_receipts) == 1
    assert "Message rejected." in fake_channel.sent_receipts[0][1]


@pytest.mark.asyncio
async def test_classifier_failure_routes_capture_to_inbox(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_channel,
    fake_discord,
    capture_handler,
):
    classifier = FakeClassifier.raise_error(RuntimeError("simulated Gemini outage"))
    message = FakeDiscordMessage(
        message_id=1004,
        channel=fake_channel,
        content="Save this even if Gemini is down.",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    capture = ledger_rows(ledger)[0]
    notes = note_files(test_settings.vault_path)
    markdown = notes[0].read_text(encoding="utf-8")
    final_receipt = fake_channel.edited_receipts[0][1]

    assert capture["raw_text"] == "Save this even if Gemini is down."
    assert capture["status"] == INBOX
    assert len(notes) == 1
    assert notes[0].relative_to(test_settings.vault_path).as_posix().startswith("00_inbox/")
    assert "Save this even if Gemini is down." in markdown
    assert "automatic classification failed" in final_receipt
    assert "Your note is safe." in final_receipt
    assert audit_events(test_settings.vault_path)[0]["event"] == "INBOX"


@pytest.mark.asyncio
async def test_empty_classifier_result_routes_capture_to_inbox(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_channel,
    fake_discord,
    capture_handler,
):
    classifier = FakeClassifier({})
    message = FakeDiscordMessage(
        message_id=1006,
        channel=fake_channel,
        content="This malformed classifier result should not file.",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    capture = ledger_rows(ledger)[0]

    assert classifier.call_count == 1
    assert capture["status"] == INBOX
    assert len(note_files(test_settings.vault_path)) == 1
    assert note_files(test_settings.vault_path)[0].relative_to(
        test_settings.vault_path
    ).as_posix().startswith("00_inbox/")


@pytest.mark.asyncio
async def test_duplicate_event_after_filing_does_not_refile_or_resend(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_classifier,
    fake_channel,
    fake_discord,
    capture_handler,
):
    message = FakeDiscordMessage(
        message_id=1007,
        channel=fake_channel,
        content="Review reconnect handling.",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, fake_classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)
    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    capture = ledger.get_capture(ledger_rows(ledger)[0]["capture_id"])

    assert len(ledger_rows(ledger)) == 1
    assert len(note_files(test_settings.vault_path)) == 1
    assert fake_classifier.call_count == 1
    assert len(fake_channel.sent_receipts) == 1
    assert event_types(ledger, capture.capture_id).count("CAPTURE_FILED") == 1


@pytest.mark.asyncio
async def test_initial_receipt_send_failure_still_files_and_sends_final_receipt(
    test_settings,
    ledger,
    queue,
    vault_writer,
    fake_classifier,
    fake_channel,
    fake_discord,
    capture_handler,
):
    fake_channel.fail_initial_send = True
    message = FakeDiscordMessage(
        message_id=1008,
        channel=fake_channel,
        content="File this even if the first receipt cannot send.",
    )
    app = make_app(test_settings, ledger, queue, vault_writer, fake_classifier, fake_discord)

    await ingest_if_allowed(message, test_settings, capture_handler)
    assert ledger_rows(ledger)[0]["status"] == RECEIVED
    await drain_worker(app)

    capture = ledger.get_capture(ledger_rows(ledger)[0]["capture_id"])

    assert capture.status == FILED
    assert len(note_files(test_settings.vault_path)) == 1
    assert fake_channel.initial_send_failed is True
    assert len(fake_channel.replacement_receipts) == 1
    assert fake_channel.replacement_receipts[0][1].startswith(f"✅ {capture.capture_id} filed.")
    assert capture.receipt_message_id == str(fake_channel.replacement_receipts[0][0])


@pytest.mark.asyncio
async def test_vault_write_failure_preserves_raw_capture(
    test_settings,
    ledger,
    queue,
    fake_classifier,
    fake_channel,
    fake_discord,
    capture_handler,
):
    message = FakeDiscordMessage(
        message_id=1005,
        channel=fake_channel,
        content="Keep my raw capture if the vault write fails.",
    )
    app = make_app(
        test_settings,
        ledger,
        queue,
        FakeFailingVaultWriter(),
        fake_classifier,
        fake_discord,
    )

    await ingest_if_allowed(message, test_settings, capture_handler)
    await drain_worker(app)

    capture = ledger_rows(ledger)[0]
    final_receipt = fake_channel.edited_receipts[0][1]

    assert capture["status"] == FAILED
    assert capture["raw_text"] == "Keep my raw capture if the vault write fails."
    assert capture["last_error"] == "vault write failed: OSError: simulated vault write failure"
    assert note_files(test_settings.vault_path) == []
    assert "vault filing failed" in final_receipt
    assert "safe in the local ledger" in final_receipt
