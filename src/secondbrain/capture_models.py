from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Tuple


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

# Clarification sub-states (stored in clarification_status column, not main status)
NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
CLARIFICATION_RESOLVED = "RESOLVED"

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
    retry_attempts: int = 0
    delivery_commit_hash: str | None = None
    delivery_reason_type: str | None = None
    clarification_status: str | None = None
    clarification_question: str | None = None
    receipt_sync_status: str = "clean"
    receipt_sync_last_attempt_at: str | None = None
    receipt_sync_last_error_type: str | None = None


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
    # Populated for replay/stale cases: "retry_scheduled", "terminal_failure",
    # "ignored_stale_attempt", "ignored_already_terminal", "ignored_retry_already_scheduled"
    outcome: str = ""


@dataclass(frozen=True)
class RequeuedLease:
    capture_id: str
    delivery_attempts: int
    retry_attempts: int
    previous_delivery_status: str
    next_attempt_at: datetime


@dataclass(frozen=True)
class FailedLease:
    capture_id: str
    delivery_attempts: int
    retry_attempts: int
    previous_delivery_status: str


@dataclass(frozen=True)
class LeaseReaperResult:
    scanned: int
    requeued: Tuple[RequeuedLease, ...]
    failed: Tuple[FailedLease, ...]


@dataclass(frozen=True)
class WorkflowErrorOutcome:
    capture_id: str
    delivery_attempt: int
    delivery_status: str
    retry_attempts: int
    outcome: str


# ---------------------------------------------------------------------------
# Vault update proposal statuses (SB-136)
# ---------------------------------------------------------------------------

PROPOSAL_PENDING = "PENDING"
PROPOSAL_APPROVED = "APPROVED"
PROPOSAL_REJECTED = "REJECTED"
PROPOSAL_APPLYING = "APPLYING"
PROPOSAL_APPLIED = "APPLIED"
PROPOSAL_FAILED = "FAILED"

ALL_PROPOSAL_STATUSES = {
    PROPOSAL_PENDING,
    PROPOSAL_APPROVED,
    PROPOSAL_REJECTED,
    PROPOSAL_APPLYING,
    PROPOSAL_APPLIED,
    PROPOSAL_FAILED,
}

TERMINAL_PROPOSAL_STATUSES = {PROPOSAL_REJECTED, PROPOSAL_APPLIED}

ALLOWED_PROPOSAL_OPERATIONS = {
    "mark_task_done",
    "mark_task_open",
    "set_task_due_date",
    "set_task_priority",
    "append_task",
    "append_note_section",
    "move_note_to_folder",
    "add_project_tag",
    "add_weekly_review_entry",
}


@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: str
    source: str
    requested_by: str
    operation: str
    target_note_path: str
    target_anchor_json: str | None
    change_json: str
    reason: str | None
    status: str
    requires_approval: bool
    submitted_at: datetime
    reviewed_at: datetime | None
    reviewed_by: str | None
    applied_at: datetime | None
    rejected_reason: str | None
    git_commit_hash: str | None
    last_error: str | None
    approval_message_id: str | None = None



@dataclass(frozen=True)
class DeliveryMutationResult:
    capture_id: str
    delivery_status: str
    delivery_attempts: int
    changed: bool
    outcome: Literal[
        "changed",
        "idempotent_replay",
        "stale_attempt",
        "invalid_state",
        "conflicting_replay",
        "ignored_already_terminal",
    ]
