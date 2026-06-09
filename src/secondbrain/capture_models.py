from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Note lifecycle statuses (what happened to the note)
# ---------------------------------------------------------------------------

RECEIVED = "RECEIVED"
FORWARDED = "FORWARDED"    # legacy local-worker status — kept for backward compat
CLASSIFYING = "CLASSIFYING"  # legacy local-worker status — kept for backward compat
FILED = "FILED"
INBOX = "INBOX"
REJECTED_SENSITIVE = "REJECTED_SENSITIVE"
FAILED = "FAILED"

ALL_STATUSES = {RECEIVED, FORWARDED, CLASSIFYING, FILED, INBOX, REJECTED_SENSITIVE, FAILED}
TERMINAL_STATUSES = {FILED, INBOX, REJECTED_SENSITIVE, FAILED}

# ---------------------------------------------------------------------------
# Delivery statuses (where is downstream delivery and processing)
# ---------------------------------------------------------------------------

NOT_APPLICABLE = "NOT_APPLICABLE"
PENDING_FORWARD = "PENDING_FORWARD"
FORWARDING = "FORWARDING"
DELIVERY_FORWARDED = "FORWARDED"    # same string as legacy note status; different column
DELIVERY_CLASSIFYING = "CLASSIFYING"  # same string; different column
RETRY_WAIT = "RETRY_WAIT"
COMPLETE = "COMPLETE"
DELIVERY_FAILED = "FAILED"          # same string as note FAILED; different column

ALL_DELIVERY_STATUSES = {
    NOT_APPLICABLE,
    PENDING_FORWARD,
    FORWARDING,
    DELIVERY_FORWARDED,
    DELIVERY_CLASSIFYING,
    RETRY_WAIT,
    COMPLETE,
    DELIVERY_FAILED,
}
# Delivery statuses that hold a processing lease
LEASABLE_DELIVERY_STATUSES = {FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING}
# Terminal delivery statuses
TERMINAL_DELIVERY_STATUSES = {NOT_APPLICABLE, COMPLETE, DELIVERY_FAILED}


@dataclass(frozen=True)
class CaptureRecord:
    capture_id: str
    discord_message_id: str
    discord_channel_id: str
    discord_guild_id: str
    discord_author_id: str

    status: str
    delivery_status: str
    delivery_attempts: int
    processing_lease_until: datetime | None
    next_attempt_at: datetime | None

    raw_text: str | None
    redacted_text: str | None
    is_sensitive: bool
    has_attachments: bool
    attachment_metadata: list[dict[str, Any]]

    received_at: datetime
    receipt_message_id: str | None
    derived_note_path: str | None
    last_error: str | None
    delivery_commit_hash: str | None = None
    delivery_reason_type: str | None = None


@dataclass(frozen=True)
class CaptureStatusSnapshot:
    total_captures: int
    filed: int
    inbox: int
    rejected_sensitive: int
    failed: int
    last_reconciled_discord_message_id: str | None
    last_successful_vault_write: str | None


@dataclass(frozen=True)
class TransitionResult:
    capture_id: str
    previous_status: str
    status: str
    changed: bool


@dataclass(frozen=True)
class RetryDisposition:
    capture_id: str
    delivery_status: str
    delivery_attempts: int
    next_attempt_at: datetime | None
    retry_scheduled: bool
    failed_terminally: bool


@dataclass(frozen=True)
class LeaseReaperResult:
    requeued: int
    terminal_failures: int
    failed_capture_ids: list[str]
