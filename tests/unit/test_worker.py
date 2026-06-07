from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from secondbrain.ledger import FAILED, INBOX, Ledger
from secondbrain.vault_writer import VaultWriter
from secondbrain.worker import process_capture_once


VALID_CLASSIFICATION = {
    "folder": "projects",
    "project": "halo",
    "note_type": "task",
    "title": "Review WebSocket reconnect handling",
    "tags": ["telemetry", "websocket"],
    "body": "Review reconnect handling in the HALO telemetry dashboard.",
    "actions": [{"text": "Review WebSocket reconnect handling", "status": "open"}],
    "needs_clarification": False,
    "clarifying_question": None,
    "confidence": 0.91,
}


def make_settings():
    return SimpleNamespace(
        gemini_api_key="fake",
        gemini_model="gemini-test",
        classification_confidence_threshold=0.75,
    )


def insert_capture(ledger: Ledger):
    return ledger.insert_accepted_capture(
        discord_message_id="1513233540316266517",
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="Review reconnect handling.",
        received_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_process_capture_routes_classifier_failure_to_inbox(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    writer = VaultWriter(tmp_path / "vault")

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        ledger=ledger,
        vault_writer=writer,
        classifier_client=FakeClient(error=RuntimeError("timeout")),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == INBOX
    assert result.inbox_reason == "classifier failed: RuntimeError"
    assert updated.status == INBOX
    assert updated.derived_note_path == result.note_path

    note_path = tmp_path / "vault" / result.note_path
    assert note_path.exists()
    markdown = note_path.read_text(encoding="utf-8")
    assert "area: inbox" in markdown
    assert "# Unclassified capture" in markdown
    assert "Review reconnect handling." in markdown


@pytest.mark.asyncio
async def test_process_capture_routes_low_confidence_to_inbox(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)
    writer = VaultWriter(tmp_path / "vault")
    payload = {**VALID_CLASSIFICATION, "confidence": 0.2}

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        ledger=ledger,
        vault_writer=writer,
        classifier_client=FakeClient(parsed=payload),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == INBOX
    assert result.inbox_reason == "classification confidence below threshold"
    assert updated.status == INBOX
    assert updated.derived_note_path.startswith("00_inbox/")


@pytest.mark.asyncio
async def test_process_capture_marks_failed_when_vault_write_fails(tmp_path, capsys):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    capture = insert_capture(ledger)

    result = await process_capture_once(
        capture_id=capture.capture_id,
        settings=make_settings(),
        ledger=ledger,
        vault_writer=FailingVaultWriter(),
        classifier_client=FakeClient(parsed=VALID_CLASSIFICATION),
    )

    updated = ledger.get_capture(capture.capture_id)
    assert result is not None
    assert result.status == FAILED
    assert result.note_path is None
    assert updated.status == FAILED
    assert updated.raw_text == "Review reconnect handling."
    assert updated.derived_note_path is None
    assert updated.last_error == "vault write failed: OSError: vault unavailable"

    output = capsys.readouterr().out
    assert f"{capture.capture_id} failed: vault write failed" in output
    assert "vault unavailable" in output


class FakeClient:
    def __init__(self, *, parsed=None, error=None):
        self.aio = SimpleNamespace(models=FakeModels(parsed=parsed, error=error))


class FakeModels:
    def __init__(self, *, parsed, error):
        self.parsed = parsed
        self.error = error

    async def generate_content(self, **kwargs):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(parsed=self.parsed)


class FailingVaultWriter:
    def write_note(self, **kwargs):
        raise OSError("vault unavailable")
