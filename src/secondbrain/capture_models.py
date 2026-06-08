from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


RECEIVED = "RECEIVED"
CLASSIFYING = "CLASSIFYING"
FILED = "FILED"
INBOX = "INBOX"
REJECTED_SENSITIVE = "REJECTED_SENSITIVE"
FAILED = "FAILED"

ALL_STATUSES = {RECEIVED, CLASSIFYING, FILED, INBOX, REJECTED_SENSITIVE, FAILED}
TERMINAL_STATUSES = {FILED, INBOX, REJECTED_SENSITIVE, FAILED}


@dataclass(frozen=True)
class CaptureRecord:
    capture_id: str
    discord_message_id: str
    discord_channel_id: str
    discord_guild_id: str
    discord_author_id: str
    status: str
    raw_text: str | None
    redacted_text: str | None
    is_sensitive: bool
    has_attachments: bool
    attachment_metadata: list[dict[str, Any]]
    received_at: datetime
    receipt_message_id: str | None
    derived_note_path: str | None
    last_error: str | None


@dataclass(frozen=True)
class CaptureStatusSnapshot:
    total_captures: int
    filed: int
    inbox: int
    rejected_sensitive: int
    failed: int
    last_reconciled_discord_message_id: str | None
    last_successful_vault_write: str | None
