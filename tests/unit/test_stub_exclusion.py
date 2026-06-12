"""Unit tests for stub:// exclusion in status metrics and stub receipt formatters."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from secondbrain.ledger import Ledger
from secondbrain.receipts import (
    ATTACHMENT_WARNING,
    format_stub_filed_receipt,
    format_stub_inbox_receipt,
)
from secondbrain.status import StatusSettings, read_operational_status

_NOW = datetime.now(UTC)
_LEASE = _NOW + timedelta(seconds=300)

_MSG_ID = 0


def _next_msg() -> str:
    global _MSG_ID
    _MSG_ID += 1
    return str(50000 + _MSG_ID)


def _make_ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite3")


def _make_settings(tmp_path) -> StatusSettings:
    return StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )


def _file_capture(ledger: Ledger, *, derived_note_path: str) -> str:
    """Insert a capture and advance it to FILED/COMPLETE state."""
    result = ledger.insert_accepted_capture(
        discord_message_id=_next_msg(),
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="note text",
        received_at=_NOW,
    )
    capture_id = result.capture.capture_id
    claimed = ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    match = next(c for c in claimed if c.capture_id == capture_id)
    ledger.mark_forwarded(
        capture_id=capture_id,
        delivery_attempt=match.delivery_attempts,
        lease_until=_LEASE,
    )
    ledger.mark_filed(
        capture_id=capture_id,
        delivery_attempt=match.delivery_attempts,
        derived_note_path=derived_note_path,
    )
    return capture_id


def _inbox_capture(ledger: Ledger, *, derived_note_path: str) -> str:
    """Insert a capture and advance it to INBOX/COMPLETE state."""
    result = ledger.insert_accepted_capture(
        discord_message_id=_next_msg(),
        discord_channel_id="200",
        discord_guild_id="300",
        discord_author_id="400",
        raw_text="inbox note",
        received_at=_NOW,
    )
    capture_id = result.capture.capture_id
    claimed = ledger.claim_due_deliveries(now=_NOW, lease_until=_LEASE, batch_size=10)
    match = next(c for c in claimed if c.capture_id == capture_id)
    ledger.mark_forwarded(
        capture_id=capture_id,
        delivery_attempt=match.delivery_attempts,
        lease_until=_LEASE,
    )
    ledger.mark_inbox(
        capture_id=capture_id,
        delivery_attempt=match.delivery_attempts,
        derived_note_path=derived_note_path,
    )
    return capture_id


# ── captures_filed_today stub:// exclusion ────────────────────────────────────


def test_captures_filed_today_excludes_stub_paths(tmp_path):
    ledger = _make_ledger(tmp_path)
    _file_capture(ledger, derived_note_path="stub://cap-001")
    snapshot = read_operational_status(settings=_make_settings(tmp_path), now=_NOW)
    assert snapshot.captures_filed_today == 0


def test_captures_filed_today_counts_real_vault_paths(tmp_path):
    ledger = _make_ledger(tmp_path)
    _file_capture(ledger, derived_note_path="vault/notes/My Note.md")
    snapshot = read_operational_status(settings=_make_settings(tmp_path), now=_NOW)
    assert snapshot.captures_filed_today == 1


def test_captures_filed_today_counts_real_but_not_stub(tmp_path):
    ledger = _make_ledger(tmp_path)
    _file_capture(ledger, derived_note_path="vault/notes/Real.md")
    _file_capture(ledger, derived_note_path="stub://cap-002")
    _file_capture(ledger, derived_note_path="vault/notes/Also Real.md")
    snapshot = read_operational_status(settings=_make_settings(tmp_path), now=_NOW)
    assert snapshot.captures_filed_today == 2


# ── last_successful_vault_write stub:// exclusion ────────────────────────────


def test_last_successful_vault_write_is_none_when_only_stub_paths(tmp_path):
    ledger = _make_ledger(tmp_path)
    _file_capture(ledger, derived_note_path="stub://cap-abc")
    snapshot = read_operational_status(settings=_make_settings(tmp_path), now=_NOW)
    assert snapshot.last_successful_vault_write is None


def test_last_successful_vault_write_returns_real_path(tmp_path):
    ledger = _make_ledger(tmp_path)
    _file_capture(ledger, derived_note_path="vault/notes/Real Note.md")
    snapshot = read_operational_status(settings=_make_settings(tmp_path), now=_NOW)
    assert snapshot.last_successful_vault_write == "vault/notes/Real Note.md"


def test_last_successful_vault_write_skips_stub_returns_real(tmp_path):
    ledger = _make_ledger(tmp_path)
    _file_capture(ledger, derived_note_path="vault/notes/First.md")
    _file_capture(ledger, derived_note_path="stub://cap-003")
    snapshot = read_operational_status(settings=_make_settings(tmp_path), now=_NOW)
    # stub should not shadow the real path
    assert snapshot.last_successful_vault_write is not None
    assert not snapshot.last_successful_vault_write.startswith("stub://")


def test_last_successful_vault_write_includes_inbox_real_paths(tmp_path):
    ledger = _make_ledger(tmp_path)
    _inbox_capture(ledger, derived_note_path="vault/inbox/Note.md")
    snapshot = read_operational_status(settings=_make_settings(tmp_path), now=_NOW)
    assert snapshot.last_successful_vault_write == "vault/inbox/Note.md"


def test_last_successful_vault_write_excludes_stub_inbox_paths(tmp_path):
    ledger = _make_ledger(tmp_path)
    _inbox_capture(ledger, derived_note_path="stub://cap-inbox")
    snapshot = read_operational_status(settings=_make_settings(tmp_path), now=_NOW)
    assert snapshot.last_successful_vault_write is None


# ── Stub receipt formatters ───────────────────────────────────────────────────


def test_format_stub_filed_receipt_contains_capture_id():
    text = format_stub_filed_receipt("cap-xyz", has_attachments=False)
    assert "cap-xyz" in text


def test_format_stub_filed_receipt_uses_filed_label():
    text = format_stub_filed_receipt("cap-xyz", has_attachments=False)
    assert "filed (stub)" in text


def test_format_stub_filed_receipt_mentions_ledger():
    text = format_stub_filed_receipt("cap-xyz", has_attachments=False)
    assert "ledger" in text.lower()


def test_format_stub_filed_receipt_no_attachment_warning_when_no_attachments():
    text = format_stub_filed_receipt("cap-xyz", has_attachments=False)
    assert ATTACHMENT_WARNING not in text


def test_format_stub_filed_receipt_includes_attachment_warning_when_has_attachments():
    text = format_stub_filed_receipt("cap-xyz", has_attachments=True)
    assert ATTACHMENT_WARNING in text


def test_format_stub_inbox_receipt_contains_capture_id():
    text = format_stub_inbox_receipt("cap-abc", has_attachments=False)
    assert "cap-abc" in text


def test_format_stub_inbox_receipt_uses_inbox_label():
    text = format_stub_inbox_receipt("cap-abc", has_attachments=False)
    assert "inbox (stub)" in text


def test_format_stub_inbox_receipt_mentions_ledger():
    text = format_stub_inbox_receipt("cap-abc", has_attachments=False)
    assert "ledger" in text.lower()


def test_format_stub_inbox_receipt_no_attachment_warning_when_no_attachments():
    text = format_stub_inbox_receipt("cap-abc", has_attachments=False)
    assert ATTACHMENT_WARNING not in text


def test_format_stub_inbox_receipt_includes_attachment_warning_when_has_attachments():
    text = format_stub_inbox_receipt("cap-abc", has_attachments=True)
    assert ATTACHMENT_WARNING in text
