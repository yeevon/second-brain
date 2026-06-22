from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import uuid

from secondbrain.capture_models import (
    ALL_DELIVERY_STATUSES,
    ALL_PROPOSAL_STATUSES,
    ALL_STATUSES,
    CLASSIFYING,
    COMPLETE,
    DELIVERY_CLASSIFYING,
    DELIVERY_FAILED,
    DELIVERY_FORWARDED,
    FAILED,
    FILED,
    FORWARDED,
    FORWARDING,
    INBOX,
    LEASABLE_DELIVERY_STATUSES,
    NOT_APPLICABLE,
    PENDING_FORWARD,
    PROPOSAL_APPLYING,
    PROPOSAL_APPLIED,
    PROPOSAL_FAILED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    RECEIVED,
    REJECTED_SENSITIVE,
    RETRY_WAIT,
    TERMINAL_STATUSES,
    CaptureRecord,
    DeliveryMutationResult,
    FailedLease,
    LeaseReaperResult,
    ProposalRecord,
    RequeuedLease,
    RetryDisposition,
    TransitionResult,
    WorkflowErrorOutcome,
)
from secondbrain.sqlite_runtime import SQLiteRuntime


UNSET = object()

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def _validate_safe_slug(value: str) -> None:
    if not _SAFE_SLUG_RE.match(value):
        raise ValueError(f"unsafe delivery category string: {value!r}")


UNSET = object()

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def _validate_safe_slug(value: str) -> None:
    if not _SAFE_SLUG_RE.match(value):
        raise ValueError(f"unsafe delivery category string: {value!r}")


@dataclass(frozen=True)
class InsertResult:
    capture: CaptureRecord
    created: bool


class Ledger:
    def __init__(self, path: Path | str, settings: Any = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        busy_timeout_ms           = getattr(settings, "sqlite_busy_timeout_ms",            1000)  if settings else 1000
        retry_attempts            = getattr(settings, "sqlite_busy_retry_attempts",           5)  if settings else 5
        retry_base_delay_ms       = getattr(settings, "sqlite_busy_retry_base_delay_ms",     25)  if settings else 25
        job_queue_maxsize         = getattr(settings, "sqlite_job_queue_maxsize",         10000)  if settings else 10000
        startup_timeout_s         = getattr(settings, "sqlite_startup_timeout_s",            10)  if settings else 10
        queue_wait_timeout_s      = getattr(settings, "sqlite_queue_wait_timeout_s",         30)  if settings else 30
        job_completion_timeout_s  = getattr(settings, "sqlite_job_completion_timeout_s",     60)  if settings else 60
        shutdown_drain_timeout_s  = getattr(settings, "sqlite_shutdown_drain_timeout_s",     10)  if settings else 10

        self._runtime = SQLiteRuntime(
            self.path,
            busy_timeout_ms=busy_timeout_ms,
            retry_attempts=retry_attempts,
            retry_base_delay_ms=retry_base_delay_ms,
            job_queue_maxsize=job_queue_maxsize,
            startup_timeout_s=startup_timeout_s,
            queue_wait_timeout_s=queue_wait_timeout_s,
            job_completion_timeout_s=job_completion_timeout_s,
            shutdown_timeout_s=shutdown_drain_timeout_s,
        )

    def close(self) -> None:
        self._runtime.close()

    # ------------------------------------------------------------------
    # Internal dispatch helpers
    # ------------------------------------------------------------------

    def _write(self, operation_name: str, operation):
        return self._runtime.write(operation, operation_name=operation_name)

    def _read(self, operation_name: str, operation):
        return self._runtime.read(operation, operation_name=operation_name)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def insert_accepted_capture(
        self,
        *,
        discord_message_id: str,
        discord_channel_id: str,
        discord_guild_id: str,
        discord_author_id: str,
        raw_text: str,
        has_attachments: bool = False,
        attachment_metadata: list[dict[str, Any]] | None = None,
        received_at: datetime | None = None,
        initial_delivery_status: str = PENDING_FORWARD,
    ) -> InsertResult:
        received_at = received_at or _now()
        return self._write(
            "insert_accepted_capture",
            lambda conn: self._insert_accepted_capture(
                conn,
                discord_message_id=discord_message_id,
                discord_channel_id=discord_channel_id,
                discord_guild_id=discord_guild_id,
                discord_author_id=discord_author_id,
                raw_text=raw_text,
                has_attachments=has_attachments,
                attachment_metadata=attachment_metadata,
                received_at=received_at,
                initial_delivery_status=initial_delivery_status,
            ),
        )

    def insert_sensitive_rejection(
        self,
        *,
        discord_message_id: str,
        discord_channel_id: str,
        discord_guild_id: str,
        discord_author_id: str,
        redacted_text: str,
        sensitivity_flags: tuple[str, ...] | list[str],
        received_at: datetime | None = None,
    ) -> InsertResult:
        received_at = received_at or _now()
        flags = list(sensitivity_flags)
        return self._write(
            "insert_sensitive_rejection",
            lambda conn: self._insert_sensitive_rejection(
                conn,
                discord_message_id=discord_message_id,
                discord_channel_id=discord_channel_id,
                discord_guild_id=discord_guild_id,
                discord_author_id=discord_author_id,
                redacted_text=redacted_text,
                sensitivity_flags=flags,
                received_at=received_at,
            ),
        )

    def set_receipt_message_id(self, capture_id: str, receipt_message_id: str) -> None:
        self.update_capture(
            capture_id,
            receipt_message_id=receipt_message_id,
            event_type="RECEIPT_STORED",
            event_payload={"receipt_message_id": receipt_message_id},
        )

    def mark_classifying(self, capture_id: str) -> bool:
        return self._write(
            "mark_classifying",
            lambda conn: self._mark_classifying(conn, capture_id),
        )

    def reset_classifying_to_received(self) -> int:
        return self._write("reset_classifying_to_received", self._reset_classifying_to_received)

    def update_capture(
        self,
        capture_id: str,
        *,
        status: str | None = None,
        classification_json: dict[str, Any] | None = None,
        derived_note_path: str | None = None,
        receipt_message_id: str | None = None,
        last_error: str | None = None,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        if status is None and classification_json is None and derived_note_path is None \
                and receipt_message_id is None and last_error is None and event_type is None:
            return
        if status is not None:
            _validate_status(status)
        self._write(
            "update_capture",
            lambda conn: self._update_capture(
                conn,
                capture_id,
                status=status,
                classification_json=classification_json,
                derived_note_path=derived_note_path,
                receipt_message_id=receipt_message_id,
                last_error=last_error,
                event_type=event_type,
                event_payload=event_payload,
            ),
        )

    def transition_capture(
        self,
        capture_id: str,
        *,
        from_statuses: set[str],
        to_status: str,
        classification_json: dict[str, Any] | None | object = UNSET,
        derived_note_path: str | None | object = UNSET,
        last_error: str | None | object = UNSET,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        delivery_status: str | None = None,
    ) -> TransitionResult | None:
        _validate_status(to_status)
        for s in from_statuses:
            _validate_status(s)
        return self._write(
            "transition_capture",
            lambda conn: self._transition_capture(
                conn,
                capture_id,
                from_statuses=from_statuses,
                to_status=to_status,
                classification_json=classification_json,
                derived_note_path=derived_note_path,
                last_error=last_error,
                event_type=event_type,
                event_payload=event_payload,
                delivery_status=delivery_status,
            ),
        )

    # ------------------------------------------------------------------
    # Delivery ledger methods
    # ------------------------------------------------------------------

    def claim_due_deliveries(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        batch_size: int,
    ) -> list[CaptureRecord]:
        return self._write(
            "claim_due_deliveries",
            lambda conn: self._claim_due_deliveries(
                conn, now=now, lease_until=lease_until, batch_size=batch_size
            ),
        )

    def mark_forwarded(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        lease_until: datetime,
    ) -> DeliveryMutationResult:
        return self._write(
            "mark_forwarded",
            lambda conn: self._mark_forwarded(
                conn, capture_id=capture_id, delivery_attempt=delivery_attempt, lease_until=lease_until
            ),
        )

    def mark_classifying_delivery(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        lease_until: datetime,
    ) -> DeliveryMutationResult:
        return self._write(
            "mark_classifying_delivery",
            lambda conn: self._mark_classifying_delivery(
                conn, capture_id=capture_id, delivery_attempt=delivery_attempt, lease_until=lease_until
            ),
        )

    def mark_filed(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        derived_note_path: str,
        git_commit_hash: str | None = None,
        classification_json: dict | None = None,
    ) -> DeliveryMutationResult:
        return self._write(
            "mark_filed",
            lambda conn: self._mark_delivery_terminal(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                note_status=FILED,
                derived_note_path=derived_note_path,
                git_commit_hash=git_commit_hash,
                event_type="CAPTURE_FILED",
                classification_json=classification_json,
            ),
        )

    def mark_inbox(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        derived_note_path: str,
        git_commit_hash: str | None = None,
        reason_type: str = "",
        classification_json: dict | None = None,
    ) -> DeliveryMutationResult:
        if reason_type:
            _validate_safe_slug(reason_type)
        return self._write(
            "mark_inbox",
            lambda conn: self._mark_delivery_terminal(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                note_status=INBOX,
                derived_note_path=derived_note_path,
                git_commit_hash=git_commit_hash,
                event_type="CAPTURE_INBOX",
                extra_payload={"reason_type": reason_type} if reason_type else None,
                classification_json=classification_json,
            ),
        )

    def schedule_retry(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        now: datetime,
        error_type: str,
        reason_type: str,
        max_attempts: int,
        base_delay_seconds: int,
        max_delay_seconds: int,
    ) -> RetryDisposition:
        return self._write(
            "schedule_retry",
            lambda conn: self._schedule_retry(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                now=now,
                error_type=error_type,
                reason_type=reason_type,
                max_attempts=max_attempts,
                base_delay_seconds=base_delay_seconds,
                max_delay_seconds=max_delay_seconds,
            ),
        )

    def renew_delivery_lease(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        lease_until: datetime,
    ) -> DeliveryMutationResult:
        return self._write(
            "renew_delivery_lease",
            lambda conn: self._renew_delivery_lease(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                lease_until=lease_until,
            ),
        )

    def mark_delivery_failed_terminally(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        reason: str = "",
    ) -> DeliveryMutationResult:
        return self._write(
            "mark_delivery_failed_terminally",
            lambda conn: self._mark_delivery_failed_terminally(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                reason=reason,
            ),
        )

    def report_workflow_error(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        disposition: str,
        error_type: str,
        reason_type: str,
        workflow_id: str,
        workflow_name: str,
        execution_id: str | None,
        stage: str,
        max_attempts: int,
        base_delay_seconds: int,
        max_delay_seconds: int,
    ) -> WorkflowErrorOutcome:
        now = _now()
        return self._write(
            "report_workflow_error",
            lambda conn: self._report_workflow_error(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                disposition=disposition,
                error_type=error_type,
                reason_type=reason_type,
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                execution_id=execution_id,
                stage=stage,
                max_attempts=max_attempts,
                base_delay_seconds=base_delay_seconds,
                max_delay_seconds=max_delay_seconds,
                now=now,
            ),
        )

    def reap_expired_processing_leases(
        self,
        *,
        now: datetime,
        batch_size: int,
        retry_max_attempts: int,
        retry_base_delay_seconds: int,
        retry_max_delay_seconds: int,
    ) -> LeaseReaperResult:
        return self._write(
            "reap_expired_processing_leases",
            lambda conn: self._reap_expired_processing_leases(
                conn,
                now=now,
                batch_size=batch_size,
                retry_max_attempts=retry_max_attempts,
                retry_base_delay_seconds=retry_base_delay_seconds,
                retry_max_delay_seconds=retry_max_delay_seconds,
            ),
        )

    def manual_retry_capture(self, *, capture_id: str, now: datetime) -> bool:
        return self._write(
            "manual_retry_capture",
            lambda conn: self._manual_retry_capture(conn, capture_id=capture_id, now=now),
        )

    def normalize_delivery_for_local_full(self) -> int:
        """Set PENDING_FORWARD/RETRY_WAIT captures to NOT_APPLICABLE for local-full mode startup."""
        return self._write(
            "normalize_delivery_for_local_full",
            self._normalize_delivery_for_local_full,
        )

    # ------------------------------------------------------------------
    # SB-117: Clarification methods
    # ------------------------------------------------------------------

    def record_clarification(
        self,
        *,
        capture_id: str,
        question: str,
    ) -> bool:
        return self._write(
            "record_clarification",
            lambda conn: self._record_clarification(conn, capture_id=capture_id, question=question),
        )

    def resolve_clarification(self, capture_id: str) -> bool:
        return self._write(
            "resolve_clarification",
            lambda conn: self._resolve_clarification(conn, capture_id),
        )

    def captures_needing_clarification(self) -> list[CaptureRecord]:
        return self._read(
            "captures_needing_clarification",
            self._captures_needing_clarification,
        )

    def count_needs_clarification(self) -> int:
        return self._read(
            "count_needs_clarification",
            self._count_needs_clarification,
        )

    # ------------------------------------------------------------------
    # SB-118: Correction methods
    # ------------------------------------------------------------------

    def get_capture_by_receipt_message_id(self, receipt_message_id: str) -> CaptureRecord | None:
        return self._read(
            "get_capture_by_receipt_message_id",
            lambda conn: self._get_capture_by_receipt_message_id(conn, receipt_message_id),
        )

    def record_correction(
        self,
        *,
        capture_id: str,
        old_note_path: str,
        new_note_path: str,
        git_commit_hash: str | None,
        correction_reason: str | None,
        move_outcome: str = "moved",
    ) -> str:
        return self._write(
            "record_correction",
            lambda conn: self._record_correction(
                conn,
                capture_id=capture_id,
                old_note_path=old_note_path,
                new_note_path=new_note_path,
                git_commit_hash=git_commit_hash,
                correction_reason=correction_reason,
                move_outcome=move_outcome,
            ),
        )

    def corrections_for_capture(self, capture_id: str) -> list[dict[str, Any]]:
        return self._read(
            "corrections_for_capture",
            lambda conn: self._corrections_for_capture(conn, capture_id),
        )

    def capture_events(self, capture_id: str) -> list[dict[str, Any]]:
        return self._read(
            "capture_events",
            lambda conn: [
                dict(row)
                for row in conn.execute(
                    "SELECT event_type, event_payload_json, created_at FROM capture_events WHERE capture_id = ? ORDER BY id",
                    (capture_id,),
                ).fetchall()
            ],
        )

    def delivery_status_counts(self) -> dict[str, int]:
        return self._read("delivery_status_counts", self._delivery_status_counts)

    def delivery_snapshot(self) -> dict[str, Any]:
        return self._read("delivery_snapshot", self._delivery_snapshot)

    # ------------------------------------------------------------------
    # System state
    # ------------------------------------------------------------------

    def set_system_state(self, key: str, value: str) -> None:
        self._write("set_system_state", lambda conn: self._set_system_state(conn, key, value))

    def advance_system_state_snowflake(self, key: str, candidate: str) -> None:
        self._write(
            "advance_system_state_snowflake",
            lambda conn: self._advance_system_state_snowflake(conn, key, candidate),
        )

    def increment_system_counter(self, key: str, amount: int = 1) -> int:
        return self._write(
            "increment_system_counter",
            lambda conn: self._increment_system_counter(conn, key, amount),
        )

    def set_system_states(self, values: dict[str, str]) -> None:
        self._write("set_system_states", lambda conn: self._set_system_states(conn, values))

    def record_capture_service_start(self, *, instance_id: str, now: datetime) -> None:
        now_iso = _iso(now)
        self._write(
            "record_capture_service_start",
            lambda conn: self._record_capture_service_start(conn, instance_id=instance_id, now_iso=now_iso),
        )

    def record_capture_service_ready(self, *, instance_id: str, now: datetime) -> bool:
        now_iso = _iso(now)
        return self._write(
            "record_capture_service_ready",
            lambda conn: self._record_capture_service_ready(conn, instance_id=instance_id, now_iso=now_iso),
        )

    def record_capture_service_heartbeat(self, *, instance_id: str, now: datetime) -> bool:
        now_iso = _iso(now)
        return self._write(
            "record_capture_service_heartbeat",
            lambda conn: self._record_capture_service_heartbeat(conn, instance_id=instance_id, now_iso=now_iso),
        )

    def record_capture_service_stop(self, *, instance_id: str, now: datetime) -> bool:
        now_iso = _iso(now)
        return self._write(
            "record_capture_service_stop",
            lambda conn: self._record_capture_service_stop(conn, instance_id=instance_id, now_iso=now_iso),
        )

    def record_successful_reconciliation(self, *, mode: str, now: datetime) -> None:
        now_iso = _iso(now)
        self._write(
            "record_successful_reconciliation",
            lambda conn: self._set_system_states(conn, {
                "last_successful_reconciliation_at": now_iso,
                "last_successful_reconciliation_mode": mode,
            }),
        )

    def periodic_reconcile_snapshot(self) -> dict:
        keys = [
            "periodic_reconcile_runs_total",
            "periodic_reconcile_recovered_total",
            "periodic_reconcile_duplicates_total",
            "periodic_reconcile_ignored_total",
            "periodic_reconcile_failures_total",
            "periodic_reconcile_limit_exceeded_total",
            "periodic_reconcile_last_run_at",
            "periodic_reconcile_last_success_at",
            "periodic_reconcile_last_recovered_count",
            "periodic_reconcile_last_warning",
            "periodic_reconcile_last_error_type",
        ]
        return self._read(
            "periodic_reconcile_snapshot",
            lambda conn: {key: self._get_system_state(conn, key) for key in keys},
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_capture(self, capture_id: str) -> CaptureRecord:
        return self._read("get_capture", lambda conn: self._get_capture(conn, capture_id))

    def capture_classification_json(self, capture_id: str) -> dict[str, Any] | None:
        return self._read(
            "capture_classification_json",
            lambda conn: self._capture_classification_json(conn, capture_id),
        )

    def enqueueable_capture_ids(self) -> list[str]:
        return self._read("enqueueable_capture_ids", self._enqueueable_capture_ids)

    def captures_by_status(self, status: str) -> list[CaptureRecord]:
        return self._read("captures_by_status", lambda conn: self._captures_by_status(conn, status))

    def status_counts(self) -> dict[str, int]:
        return self._read("status_counts", self._status_counts)

    def total_captures(self) -> int:
        return self._read("total_captures", self._total_captures)

    def ping(self) -> None:
        self._read("ping", lambda conn: conn.execute("SELECT 1").fetchone())

    def last_successful_vault_write(self) -> str | None:
        return self._read("last_successful_vault_write", self._last_successful_vault_write)

    def get_system_state(self, key: str) -> str | None:
        return self._read("get_system_state", lambda conn: self._get_system_state(conn, key))

    def daily_digest_snapshot(self, *, since: datetime, now: datetime) -> dict:
        return self._read(
            "daily_digest_snapshot",
            lambda conn: self._daily_digest_snapshot(conn, since=since, now=now),
        )

    def weekly_digest_snapshot(self, *, since: datetime, now: datetime) -> dict:
        return self._read(
            "weekly_digest_snapshot",
            lambda conn: self._weekly_digest_snapshot(conn, since=since, now=now),
        )

    # ------------------------------------------------------------------
    # SB-136: Vault update proposal public methods
    # ------------------------------------------------------------------

    def create_proposal(
        self,
        *,
        source: str,
        requested_by: str,
        operation: str,
        target_note_path: str,
        target_anchor_json: str | None,
        change_json: str,
        reason: str | None,
        requires_approval: bool = True,
    ) -> ProposalRecord:
        submitted_at = _now()
        return self._write(
            "create_proposal",
            lambda conn: self._create_proposal(
                conn,
                source=source,
                requested_by=requested_by,
                operation=operation,
                target_note_path=target_note_path,
                target_anchor_json=target_anchor_json,
                change_json=change_json,
                reason=reason,
                requires_approval=requires_approval,
                submitted_at=submitted_at,
            ),
        )

    def get_proposal(self, proposal_id: str) -> ProposalRecord:
        return self._read(
            "get_proposal",
            lambda conn: self._get_proposal(conn, proposal_id),
        )

    def list_proposals(self, *, status: str | None = None, limit: int = 50) -> list[ProposalRecord]:
        return self._read(
            "list_proposals",
            lambda conn: self._list_proposals(conn, status=status, limit=limit),
        )

    def update_proposal(
        self,
        proposal_id: str,
        *,
        status: str | None = None,
        reviewed_at: datetime | None = None,
        reviewed_by: str | None = None,
        applied_at: datetime | None = None,
        rejected_reason: str | None = None,
        git_commit_hash: str | None = None,
        last_error: str | None = None,
        approval_message_id: str | None = None,
    ) -> ProposalRecord:
        return self._write(
            "update_proposal",
            lambda conn: self._update_proposal(
                conn,
                proposal_id,
                status=status,
                reviewed_at=reviewed_at,
                reviewed_by=reviewed_by,
                applied_at=applied_at,
                rejected_reason=rejected_reason,
                git_commit_hash=git_commit_hash,
                last_error=last_error,
                approval_message_id=approval_message_id,
            ),
        )

    # ------------------------------------------------------------------
    # Private write implementations (run inside worker-owned connection)
    # ------------------------------------------------------------------

    def _insert_accepted_capture(
        self,
        conn: sqlite3.Connection,
        *,
        discord_message_id: str,
        discord_channel_id: str,
        discord_guild_id: str,
        discord_author_id: str,
        raw_text: str,
        has_attachments: bool,
        attachment_metadata: list[dict[str, Any]] | None,
        received_at: datetime,
        initial_delivery_status: str = PENDING_FORWARD,
    ) -> InsertResult:
        existing = self._get_by_discord_message_id(conn, discord_message_id)
        if existing is not None:
            return InsertResult(capture=_record_from_row(existing), created=False)

        capture_id = self._next_capture_id(conn, received_at)
        now = _iso(_now())
        conn.execute(
            """
            INSERT INTO captures (
                capture_id,
                discord_message_id,
                discord_channel_id,
                discord_guild_id,
                discord_author_id,
                raw_text,
                is_sensitive,
                has_attachments,
                attachment_metadata_json,
                received_at,
                status,
                delivery_status,
                delivery_attempts,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                capture_id,
                discord_message_id,
                discord_channel_id,
                discord_guild_id,
                discord_author_id,
                raw_text,
                int(has_attachments),
                _json_dumps(attachment_metadata or []),
                _iso(received_at),
                RECEIVED,
                initial_delivery_status,
                now,
            ),
        )
        self._append_event(conn, capture_id, "CAPTURE_RECEIVED", {"status": RECEIVED})
        return InsertResult(
            capture=_record_from_row(self._get_by_capture_id(conn, capture_id)),
            created=True,
        )

    def _insert_sensitive_rejection(
        self,
        conn: sqlite3.Connection,
        *,
        discord_message_id: str,
        discord_channel_id: str,
        discord_guild_id: str,
        discord_author_id: str,
        redacted_text: str,
        sensitivity_flags: list[str],
        received_at: datetime,
    ) -> InsertResult:
        existing = self._get_by_discord_message_id(conn, discord_message_id)
        if existing is not None:
            return InsertResult(capture=_record_from_row(existing), created=False)

        capture_id = self._next_capture_id(conn, received_at)
        now = _iso(_now())
        conn.execute(
            """
            INSERT INTO captures (
                capture_id,
                discord_message_id,
                discord_channel_id,
                discord_guild_id,
                discord_author_id,
                redacted_text,
                is_sensitive,
                sensitivity_flags,
                has_attachments,
                received_at,
                status,
                delivery_status,
                delivery_attempts,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?, ?, 0, ?)
            """,
            (
                capture_id,
                discord_message_id,
                discord_channel_id,
                discord_guild_id,
                discord_author_id,
                redacted_text,
                _json_dumps(sensitivity_flags),
                _iso(received_at),
                REJECTED_SENSITIVE,
                NOT_APPLICABLE,
                now,
            ),
        )
        self._append_event(
            conn,
            capture_id,
            "CAPTURE_REJECTED_SENSITIVE",
            {"flags": sensitivity_flags},
        )
        return InsertResult(
            capture=_record_from_row(self._get_by_capture_id(conn, capture_id)),
            created=True,
        )

    def _mark_classifying(self, conn: sqlite3.Connection, capture_id: str) -> bool:
        now = _iso(_now())
        cursor = conn.execute(
            """
            UPDATE captures
            SET status = ?, updated_at = ?
            WHERE capture_id = ? AND status = ?
            """,
            (CLASSIFYING, now, capture_id, RECEIVED),
        )
        if cursor.rowcount == 0:
            return False
        self._append_event(conn, capture_id, "CAPTURE_CLASSIFYING", {"status": CLASSIFYING})
        return True

    def _reset_classifying_to_received(self, conn: sqlite3.Connection) -> int:
        rows = conn.execute(
            "SELECT capture_id FROM captures WHERE status = ? ORDER BY id",
            (CLASSIFYING,),
        ).fetchall()
        if not rows:
            return 0

        now = _iso(_now())
        conn.execute(
            "UPDATE captures SET status = ?, updated_at = ? WHERE status = ?",
            (RECEIVED, now, CLASSIFYING),
        )
        for row in rows:
            self._append_event(
                conn,
                row["capture_id"],
                "CAPTURE_REQUEUED",
                {"from_status": CLASSIFYING, "status": RECEIVED},
            )
        return len(rows)

    def _update_capture(
        self,
        conn: sqlite3.Connection,
        capture_id: str,
        *,
        status: str | None,
        classification_json: dict[str, Any] | None,
        derived_note_path: str | None,
        receipt_message_id: str | None,
        last_error: str | None,
        event_type: str | None,
        event_payload: dict[str, Any] | None,
    ) -> None:
        updates: list[str] = []
        values: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if classification_json is not None:
            updates.append("classification_json = ?")
            values.append(_json_dumps(classification_json))
        if derived_note_path is not None:
            updates.append("derived_note_path = ?")
            values.append(derived_note_path)
        if receipt_message_id is not None:
            updates.append("receipt_message_id = ?")
            values.append(receipt_message_id)
        if last_error is not None:
            updates.append("last_error = ?")
            values.append(last_error)

        if updates:
            updates.append("updated_at = ?")
            values.append(_iso(_now()))
            values.append(capture_id)
            conn.execute(
                f"UPDATE captures SET {', '.join(updates)} WHERE capture_id = ?",
                values,
            )
        if event_type is not None:
            self._append_event(conn, capture_id, event_type, event_payload or {})

    def _transition_capture(
        self,
        conn: sqlite3.Connection,
        capture_id: str,
        *,
        from_statuses: set[str],
        to_status: str,
        classification_json: dict[str, Any] | None | object,
        derived_note_path: str | None | object,
        last_error: str | None | object,
        event_type: str,
        event_payload: dict[str, Any] | None,
        delivery_status: str | None = None,
    ) -> TransitionResult | None:
        current_row = self._get_by_capture_id(conn, capture_id)
        previous_status = current_row["status"]
        if previous_status not in from_statuses:
            return None

        updates = ["status = ?"]
        values: list[Any] = [to_status]
        if classification_json is not UNSET:
            updates.append("classification_json = ?")
            values.append(None if classification_json is None else _json_dumps(classification_json))
        if derived_note_path is not UNSET:
            updates.append("derived_note_path = ?")
            values.append(derived_note_path)
        if last_error is not UNSET:
            updates.append("last_error = ?")
            values.append(last_error)
        if delivery_status is not None:
            updates.append("delivery_status = ?")
            values.append(delivery_status)

        updates.append("updated_at = ?")
        values.append(_iso(_now()))
        values.append(capture_id)
        values.extend(sorted(from_statuses))

        placeholders = ", ".join("?" for _ in from_statuses)
        cursor = conn.execute(
            f"""
            UPDATE captures
            SET {', '.join(updates)}
            WHERE capture_id = ?
              AND status IN ({placeholders})
            """,
            values,
        )
        if cursor.rowcount == 0:
            return None
        self._append_event(conn, capture_id, event_type, event_payload or {})
        return TransitionResult(
            capture_id=capture_id,
            previous_status=previous_status,
            status=to_status,
            changed=True,
        )

    # ------------------------------------------------------------------
    # Private delivery write implementations
    # ------------------------------------------------------------------

    def _claim_due_deliveries(
        self,
        conn: sqlite3.Connection,
        *,
        now: datetime,
        lease_until: datetime,
        batch_size: int,
    ) -> list[CaptureRecord]:
        now_iso = _iso(now)
        lease_iso = _iso(lease_until)
        rows = conn.execute(
            """
            SELECT * FROM captures
            WHERE status = 'RECEIVED'
              AND (
                  delivery_status = 'PENDING_FORWARD'
                  OR (delivery_status = 'RETRY_WAIT' AND next_attempt_at <= ?)
              )
            ORDER BY id
            LIMIT ?
            """,
            (now_iso, batch_size),
        ).fetchall()

        claimed = []
        for row in rows:
            capture_id = row["capture_id"]
            new_attempts = row["delivery_attempts"] + 1
            conn.execute(
                """
                UPDATE captures
                SET delivery_status = ?, delivery_attempts = ?, processing_lease_until = ?,
                    next_attempt_at = NULL, last_error = NULL, updated_at = ?
                WHERE capture_id = ?
                """,
                (FORWARDING, new_attempts, lease_iso, _iso(_now()), capture_id),
            )
            self._append_event(
                conn,
                capture_id,
                "DELIVERY_ATTEMPT_CLAIMED",
                {"delivery_attempt": new_attempts, "lease_until": lease_iso},
            )
            claimed.append(_record_from_row(self._get_by_capture_id(conn, capture_id)))
        return claimed

    def _mark_forwarded(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        lease_until: datetime,
    ) -> DeliveryMutationResult:
        lease_iso = _iso(lease_until)
        row = self._get_by_capture_id(conn, capture_id)

        def _result(outcome: str, changed: bool = False) -> DeliveryMutationResult:
            return DeliveryMutationResult(
                capture_id=capture_id,
                delivery_status=row["delivery_status"],
                delivery_attempts=row["delivery_attempts"],
                changed=changed,
                outcome=outcome,
            )

        if row["delivery_attempts"] != delivery_attempt:
            return _result("stale_attempt")

        current_ds = row["delivery_status"]
        if current_ds == DELIVERY_FORWARDED:
            return _result("idempotent_replay")
        if current_ds != FORWARDING:
            return _result("invalid_state")

        conn.execute(
            """
            UPDATE captures
            SET delivery_status = ?, processing_lease_until = ?, next_attempt_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE capture_id = ?
            """,
            (DELIVERY_FORWARDED, lease_iso, _iso(_now()), capture_id),
        )
        self._append_event(
            conn,
            capture_id,
            "CAPTURE_FORWARDED",
            {"delivery_attempt": delivery_attempt, "lease_until": lease_iso},
        )
        return DeliveryMutationResult(
            capture_id=capture_id,
            delivery_status=DELIVERY_FORWARDED,
            delivery_attempts=delivery_attempt,
            changed=True,
            outcome="changed",
        )

    def _mark_classifying_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        lease_until: datetime,
    ) -> DeliveryMutationResult:
        lease_iso = _iso(lease_until)
        row = self._get_by_capture_id(conn, capture_id)

        def _result(outcome: str, changed: bool = False) -> DeliveryMutationResult:
            return DeliveryMutationResult(
                capture_id=capture_id,
                delivery_status=row["delivery_status"],
                delivery_attempts=row["delivery_attempts"],
                changed=changed,
                outcome=outcome,
            )

        if row["delivery_attempts"] != delivery_attempt:
            return _result("stale_attempt")

        current_ds = row["delivery_status"]
        if current_ds not in (DELIVERY_FORWARDED, DELIVERY_CLASSIFYING):
            return _result("invalid_state")

        is_renewal = current_ds == DELIVERY_CLASSIFYING
        conn.execute(
            """
            UPDATE captures
            SET delivery_status = ?, processing_lease_until = ?, updated_at = ?
            WHERE capture_id = ?
            """,
            (DELIVERY_CLASSIFYING, lease_iso, _iso(_now()), capture_id),
        )
        event = "PROCESSING_LEASE_RENEWED" if is_renewal else "CAPTURE_CLASSIFYING"
        self._append_event(
            conn,
            capture_id,
            event,
            {"delivery_attempt": delivery_attempt, "lease_until": lease_iso},
        )
        return DeliveryMutationResult(
            capture_id=capture_id,
            delivery_status=DELIVERY_CLASSIFYING,
            delivery_attempts=delivery_attempt,
            changed=True,
            outcome="changed",
        )

    def _mark_delivery_terminal(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        note_status: str,
        derived_note_path: str,
        git_commit_hash: str | None,
        event_type: str,
        extra_payload: dict[str, Any] | None = None,
        classification_json: dict | None = None,
    ) -> DeliveryMutationResult:
        row = self._get_by_capture_id(conn, capture_id)
        current_ds = row["delivery_status"]

        def _result(outcome: str, changed: bool) -> DeliveryMutationResult:
            return DeliveryMutationResult(
                capture_id=capture_id,
                delivery_status=row["delivery_status"],
                delivery_attempts=row["delivery_attempts"],
                changed=changed,
                outcome=outcome,
            )

        if row["delivery_attempts"] != delivery_attempt:
            return _result("stale_attempt", False)

        # Exact-equality idempotency check — all fields must match
        if row["status"] == note_status and current_ds == COMPLETE:
            incoming_reason = (extra_payload or {}).get("reason_type", None)
            if (
                row["derived_note_path"] != derived_note_path
                or row["delivery_commit_hash"] != git_commit_hash
                or row["delivery_reason_type"] != incoming_reason
            ):
                return _result("conflicting_replay", False)
            return _result("idempotent_replay", False)

        # Any other complete state with different note_status is a conflict
        if current_ds == COMPLETE:
            return _result("conflicting_replay", False)

        if current_ds not in (DELIVERY_FORWARDED, DELIVERY_CLASSIFYING):
            return _result("invalid_state", False)

        now = _iso(_now())
        reason_type = (extra_payload or {}).get("reason_type", None)
        if classification_json is not None:
            conn.execute(
                """
                UPDATE captures
                SET status = ?, delivery_status = ?, derived_note_path = ?,
                    delivery_commit_hash = ?, delivery_reason_type = ?,
                    processing_lease_until = NULL, next_attempt_at = NULL, last_error = NULL,
                    classification_json = ?,
                    updated_at = ?
                WHERE capture_id = ?
                """,
                (note_status, COMPLETE, derived_note_path, git_commit_hash, reason_type,
                 _json_dumps(classification_json), now, capture_id),
            )
        else:
            conn.execute(
                """
                UPDATE captures
                SET status = ?, delivery_status = ?, derived_note_path = ?,
                    delivery_commit_hash = ?, delivery_reason_type = ?,
                    processing_lease_until = NULL, next_attempt_at = NULL, last_error = NULL,
                    updated_at = ?
                WHERE capture_id = ?
                """,
                (note_status, COMPLETE, derived_note_path, git_commit_hash, reason_type, now, capture_id),
            )
        payload: dict[str, Any] = {
            "delivery_attempt": delivery_attempt,
            "derived_note_path": derived_note_path,
        }
        if git_commit_hash:
            payload["git_commit_hash"] = git_commit_hash
        if extra_payload:
            payload.update(extra_payload)
        self._append_event(conn, capture_id, event_type, payload)
        # Return fresh row state
        updated = self._get_by_capture_id(conn, capture_id)
        return DeliveryMutationResult(
            capture_id=capture_id,
            delivery_status=updated["delivery_status"],
            delivery_attempts=updated["delivery_attempts"],
            changed=True,
            outcome="changed",
        )

    def _renew_delivery_lease(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        lease_until: datetime,
    ) -> DeliveryMutationResult:
        row = self._get_by_capture_id(conn, capture_id)

        def _result(outcome: str, changed: bool = False) -> DeliveryMutationResult:
            return DeliveryMutationResult(
                capture_id=capture_id,
                delivery_status=row["delivery_status"],
                delivery_attempts=row["delivery_attempts"],
                changed=changed,
                outcome=outcome,
            )

        if row["delivery_attempts"] != delivery_attempt:
            return _result("stale_attempt")
        if row["delivery_status"] not in LEASABLE_DELIVERY_STATUSES:
            return _result("invalid_state")

        conn.execute(
            """
            UPDATE captures
            SET processing_lease_until = ?, updated_at = ?
            WHERE capture_id = ?
            """,
            (_iso(lease_until), _iso(_now()), capture_id),
        )
        return DeliveryMutationResult(
            capture_id=capture_id,
            delivery_status=row["delivery_status"],
            delivery_attempts=row["delivery_attempts"],
            changed=True,
            outcome="changed",
        )

    def _mark_delivery_failed_terminally(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        reason: str,
    ) -> DeliveryMutationResult:
        if reason:
            _validate_safe_slug(reason)

        row = self._get_by_capture_id(conn, capture_id)

        def _result(outcome: str, changed: bool = False) -> DeliveryMutationResult:
            return DeliveryMutationResult(
                capture_id=capture_id,
                delivery_status=row["delivery_status"],
                delivery_attempts=row["delivery_attempts"],
                changed=changed,
                outcome=outcome,
            )

        if row["delivery_attempts"] != delivery_attempt:
            return _result("stale_attempt")

        current_ds = row["delivery_status"]

        # Replay safety: already terminally failed
        if current_ds == DELIVERY_FAILED:
            stored_reason = row["last_error"] or ""
            if stored_reason == (reason or ""):
                return _result("idempotent_replay")
            return _result("conflicting_replay")

        # Replay safety: already succeeded (complete)
        if current_ds == COMPLETE:
            return _result("ignored_already_terminal")

        if current_ds not in (FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING):
            return _result("invalid_state")

        now_ts = _iso(_now())
        conn.execute(
            """
            UPDATE captures
            SET status = ?, delivery_status = ?, processing_lease_until = NULL,
                next_attempt_at = NULL, last_error = ?, updated_at = ?
            WHERE capture_id = ?
            """,
            (FAILED, DELIVERY_FAILED, reason or None, now_ts, capture_id),
        )
        self._append_event(
            conn,
            capture_id,
            "DELIVERY_FAILED_TERMINALLY",
            {
                "delivery_attempt": delivery_attempt,
                "delivery_status": DELIVERY_FAILED,
                "reason": reason,
            },
        )
        return DeliveryMutationResult(
            capture_id=capture_id,
            delivery_status=DELIVERY_FAILED,
            delivery_attempts=delivery_attempt,
            changed=True,
            outcome="changed",
        )

    def _schedule_retry(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        now: datetime,
        error_type: str,
        reason_type: str,
        max_attempts: int,
        base_delay_seconds: int,
        max_delay_seconds: int,
    ) -> RetryDisposition:
        _validate_safe_slug(error_type)
        if reason_type:
            _validate_safe_slug(reason_type)

        row = self._get_by_capture_id(conn, capture_id)
        current_ds = row["delivery_status"]

        # Replay safety: stale delivery attempt
        if row["delivery_attempts"] != delivery_attempt:
            return RetryDisposition(
                capture_id=capture_id,
                delivery_status=current_ds,
                delivery_attempts=row["delivery_attempts"],
                next_attempt_at=None,
                retry_scheduled=False,
                failed_terminally=False,
                outcome="ignored_stale_attempt",
            )

        # Replay safety: already terminal
        if current_ds in (COMPLETE, DELIVERY_FAILED):
            return RetryDisposition(
                capture_id=capture_id,
                delivery_status=current_ds,
                delivery_attempts=row["delivery_attempts"],
                next_attempt_at=None,
                retry_scheduled=False,
                failed_terminally=False,
                outcome="ignored_already_terminal",
            )

        # Replay safety: already waiting for retry on this same attempt
        if current_ds == RETRY_WAIT:
            from datetime import datetime as _dt
            raw_next = row["next_attempt_at"]
            next_dt = _dt.fromisoformat(raw_next) if raw_next else None
            return RetryDisposition(
                capture_id=capture_id,
                delivery_status=RETRY_WAIT,
                delivery_attempts=row["delivery_attempts"],
                next_attempt_at=next_dt,
                retry_scheduled=True,
                failed_terminally=False,
                outcome="ignored_retry_already_scheduled",
            )

        if current_ds not in (FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING):
            return RetryDisposition(
                capture_id=capture_id,
                delivery_status=current_ds,
                delivery_attempts=row["delivery_attempts"],
                next_attempt_at=None,
                retry_scheduled=False,
                failed_terminally=False,
                outcome="ignored_already_terminal",
            )

        now_ts = _iso(now)
        safe_reason = f"{reason_type}:{error_type}"
        next_retry_attempts = (row["retry_attempts"] if "retry_attempts" in row.keys() else 0) + 1

        if next_retry_attempts >= max_attempts:
            # Terminal failure
            conn.execute(
                """
                UPDATE captures
                SET status = ?, delivery_status = ?, retry_attempts = ?,
                    processing_lease_until = NULL,
                    next_attempt_at = NULL, last_error = ?, updated_at = ?
                WHERE capture_id = ?
                """,
                (FAILED, DELIVERY_FAILED, next_retry_attempts, safe_reason, now_ts, capture_id),
            )
            self._append_event(
                conn,
                capture_id,
                "RETRY_LIMIT_EXCEEDED",
                {
                    "delivery_attempt": delivery_attempt,
                    "retry_attempts": next_retry_attempts,
                    "delivery_status": DELIVERY_FAILED,
                    "error_type": error_type,
                    "reason_type": reason_type,
                },
            )
            return RetryDisposition(
                capture_id=capture_id,
                delivery_status=DELIVERY_FAILED,
                delivery_attempts=delivery_attempt,
                next_attempt_at=None,
                retry_scheduled=False,
                failed_terminally=True,
                outcome="terminal_failure",
            )

        delay = calculate_retry_delay_seconds(
            retry_attempts=next_retry_attempts,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )
        from datetime import timedelta
        next_attempt = now + timedelta(seconds=delay)
        next_iso = _iso(next_attempt)
        conn.execute(
            """
            UPDATE captures
            SET delivery_status = ?, retry_attempts = ?, processing_lease_until = NULL,
                next_attempt_at = ?, last_error = ?, updated_at = ?
            WHERE capture_id = ?
            """,
            (RETRY_WAIT, next_retry_attempts, next_iso, safe_reason, now_ts, capture_id),
        )
        self._append_event(
            conn,
            capture_id,
            "DELIVERY_RETRY_SCHEDULED",
            {
                "delivery_attempt": delivery_attempt,
                "retry_attempts": next_retry_attempts,
                "delivery_status": RETRY_WAIT,
                "next_attempt_at": next_iso,
                "error_type": error_type,
                "reason_type": reason_type,
            },
        )
        return RetryDisposition(
            capture_id=capture_id,
            delivery_status=RETRY_WAIT,
            delivery_attempts=delivery_attempt,
            next_attempt_at=next_attempt,
            retry_scheduled=True,
            failed_terminally=False,
            outcome="retry_scheduled",
        )

    def _report_workflow_error(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        disposition: str,
        error_type: str,
        reason_type: str,
        workflow_id: str,
        workflow_name: str,
        execution_id: str | None,
        stage: str,
        max_attempts: int,
        base_delay_seconds: int,
        max_delay_seconds: int,
        now: datetime,
    ) -> WorkflowErrorOutcome:
        row = self._get_by_capture_id(conn, capture_id)
        current_ds = row["delivery_status"]
        current_attempts = row["delivery_attempts"]
        retry_attempts = row["retry_attempts"] if "retry_attempts" in row.keys() else 0

        # Stale attempt: request's attempt doesn't match the active attempt
        if delivery_attempt != current_attempts:
            return WorkflowErrorOutcome(
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                delivery_status=current_ds,
                retry_attempts=retry_attempts,
                outcome="ignored_stale_attempt",
            )

        # Check for a prior N8N_WORKFLOW_ERROR_REPORTED event for this attempt
        prior_row = conn.execute(
            """
            SELECT event_payload_json FROM capture_events
            WHERE capture_id = ?
              AND event_type = 'N8N_WORKFLOW_ERROR_REPORTED'
              AND CAST(json_extract(event_payload_json, '$.delivery_attempt') AS INTEGER) = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (capture_id, delivery_attempt),
        ).fetchone()

        if prior_row is not None:
            prior_payload = json.loads(prior_row["event_payload_json"])
            prior_disposition = prior_payload.get("disposition")

            # Conflicting replay: different disposition for same attempt
            if prior_disposition != disposition:
                return WorkflowErrorOutcome(
                    capture_id=capture_id,
                    delivery_attempt=delivery_attempt,
                    delivery_status=current_ds,
                    retry_attempts=retry_attempts,
                    outcome="ignored_conflicting_replay",
                )

            # Idempotent replay: same disposition already reported
            if disposition == "retryable":
                outcome = "ignored_retry_already_scheduled" if current_ds == RETRY_WAIT else "ignored_already_terminal"
            else:
                outcome = "ignored_already_terminal"
            return WorkflowErrorOutcome(
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                delivery_status=current_ds,
                retry_attempts=retry_attempts,
                outcome=outcome,
            )

        # No prior event — already in a terminal delivery state
        if current_ds in (COMPLETE, DELIVERY_FAILED):
            return WorkflowErrorOutcome(
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                delivery_status=current_ds,
                retry_attempts=retry_attempts,
                outcome="ignored_already_terminal",
            )

        # Append the audit event before applying the transition
        event_payload: dict[str, Any] = {
            "delivery_attempt": delivery_attempt,
            "disposition": disposition,
            "error_type": error_type,
            "reason_type": reason_type,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "stage": stage,
        }
        if execution_id is not None:
            event_payload["execution_id"] = execution_id
        self._append_event(conn, capture_id, "N8N_WORKFLOW_ERROR_REPORTED", event_payload)

        if disposition == "retryable":
            retry_disp = self._schedule_retry(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                now=now,
                error_type=error_type,
                reason_type=reason_type,
                max_attempts=max_attempts,
                base_delay_seconds=base_delay_seconds,
                max_delay_seconds=max_delay_seconds,
            )
            updated = self._get_by_capture_id(conn, capture_id)
            return WorkflowErrorOutcome(
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                delivery_status=retry_disp.delivery_status,
                retry_attempts=updated["retry_attempts"] if "retry_attempts" in updated.keys() else 0,
                outcome=retry_disp.outcome,
            )
        else:  # terminal
            fail_result = self._mark_delivery_failed_terminally(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                reason=reason_type,
            )
            updated = self._get_by_capture_id(conn, capture_id)
            outcome = "terminal_failure" if fail_result.outcome == "changed" else fail_result.outcome
            return WorkflowErrorOutcome(
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                delivery_status=fail_result.delivery_status,
                retry_attempts=updated["retry_attempts"] if "retry_attempts" in updated.keys() else 0,
                outcome=outcome,
            )

    def _normalize_delivery_for_local_full(self, conn: sqlite3.Connection) -> int:
        """Normalize all non-terminal delivery states to NOT_APPLICABLE for local-full mode."""
        now = _iso(_now())
        affected = conn.execute(
            """
            SELECT capture_id FROM captures
            WHERE delivery_status IN (?, ?, ?, ?, ?)
            """,
            (PENDING_FORWARD, FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING, RETRY_WAIT),
        ).fetchall()

        if not affected:
            return 0

        conn.execute(
            """
            UPDATE captures
            SET delivery_status = ?, processing_lease_until = NULL,
                next_attempt_at = NULL, last_error = NULL, updated_at = ?
            WHERE delivery_status IN (?, ?, ?, ?, ?)
            """,
            (NOT_APPLICABLE, now,
             PENDING_FORWARD, FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING, RETRY_WAIT),
        )
        for row in affected:
            self._append_event(conn, row["capture_id"], "DELIVERY_DISABLED_FOR_LOCAL_FULL", {})

        return len(affected)

    # ------------------------------------------------------------------
    # Private system state implementations
    # ------------------------------------------------------------------

    def _set_system_state(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, _iso(_now())),
        )

    def _set_system_states(self, conn: sqlite3.Connection, values: dict[str, str]) -> None:
        now_iso = _iso(_now())
        for key, value in values.items():
            conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now_iso),
            )

    def _record_capture_service_start(
        self, conn: sqlite3.Connection, *, instance_id: str, now_iso: str
    ) -> None:
        for key, value in {
            "capture_service_instance_id": instance_id,
            "capture_service_state": "STARTING",
            "capture_service_started_at": now_iso,
            "capture_service_stopped_at": "",
        }.items():
            conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now_iso),
            )

    def _record_capture_service_ready(
        self, conn: sqlite3.Connection, *, instance_id: str, now_iso: str
    ) -> bool:
        current_id = self._get_system_state(conn, "capture_service_instance_id")
        if current_id != instance_id:
            return False
        for key, value in {
            "capture_service_state": "RUNNING",
            "capture_service_last_heartbeat_at": now_iso,
        }.items():
            conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now_iso),
            )
        return True

    def _record_capture_service_heartbeat(
        self, conn: sqlite3.Connection, *, instance_id: str, now_iso: str
    ) -> bool:
        current_id = self._get_system_state(conn, "capture_service_instance_id")
        if current_id != instance_id:
            return False
        conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            ("capture_service_last_heartbeat_at", now_iso, now_iso),
        )
        return True

    def _record_capture_service_stop(
        self, conn: sqlite3.Connection, *, instance_id: str, now_iso: str
    ) -> bool:
        current_id = self._get_system_state(conn, "capture_service_instance_id")
        if current_id != instance_id:
            return False
        for key, value in {
            "capture_service_state": "STOPPED",
            "capture_service_stopped_at": now_iso,
        }.items():
            conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now_iso),
            )
        return True

    def _increment_system_counter(
        self, conn: sqlite3.Connection, key: str, amount: int
    ) -> int:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        current = int(row["value"]) if row is not None else 0
        new_value = current + amount
        conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, str(new_value), _iso(_now())),
        )
        return new_value

    def _advance_system_state_snowflake(
        self, conn: sqlite3.Connection, key: str, candidate: str
    ) -> None:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        if row is not None and int(candidate) <= int(row["value"]):
            return
        conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, candidate, _iso(_now())),
        )

    # ------------------------------------------------------------------
    # Private read implementations
    # ------------------------------------------------------------------

    def _get_capture(self, conn: sqlite3.Connection, capture_id: str) -> CaptureRecord:
        row = conn.execute(
            "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"capture not found: {capture_id}")
        return _record_from_row(row)

    def _capture_classification_json(
        self, conn: sqlite3.Connection, capture_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT classification_json FROM captures WHERE capture_id = ?",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"capture not found: {capture_id}")
        if row["classification_json"] is None:
            return None
        return json.loads(row["classification_json"])

    def _enqueueable_capture_ids(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT capture_id FROM captures WHERE status IN (?, ?, ?) ORDER BY id",
            (RECEIVED, FORWARDED, CLASSIFYING),
        ).fetchall()
        return [row["capture_id"] for row in rows]

    def _captures_by_status(
        self, conn: sqlite3.Connection, status: str
    ) -> list[CaptureRecord]:
        rows = conn.execute(
            "SELECT * FROM captures WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
        return [_record_from_row(row) for row in rows]

    def _status_counts(self, conn: sqlite3.Connection) -> dict[str, int]:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM captures GROUP BY status"
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def _delivery_status_counts(self, conn: sqlite3.Connection) -> dict[str, int]:
        rows = conn.execute(
            "SELECT delivery_status, COUNT(*) AS count FROM captures GROUP BY delivery_status"
        ).fetchall()
        return {row["delivery_status"]: row["count"] for row in rows}

    def _reap_expired_processing_leases(
        self,
        conn: sqlite3.Connection,
        *,
        now: datetime,
        batch_size: int,
        retry_max_attempts: int,
        retry_base_delay_seconds: int,
        retry_max_delay_seconds: int,
    ) -> LeaseReaperResult:
        from datetime import timedelta
        now_iso = _iso(now)
        stale_rows = conn.execute(
            """
            SELECT capture_id, delivery_status, delivery_attempts, retry_attempts
            FROM captures
            WHERE delivery_status IN (?, ?, ?)
              AND processing_lease_until IS NOT NULL
              AND processing_lease_until <= ?
            ORDER BY processing_lease_until ASC, id ASC
            LIMIT ?
            """,
            (FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING, now_iso, batch_size),
        ).fetchall()

        requeued: list[RequeuedLease] = []
        failed: list[FailedLease] = []

        for row in stale_rows:
            capture_id = row["capture_id"]
            prev_delivery_status = row["delivery_status"]
            delivery_attempts = row["delivery_attempts"]
            next_retry_attempts = row["retry_attempts"] + 1

            if next_retry_attempts < retry_max_attempts:
                delay = calculate_retry_delay_seconds(
                    retry_attempts=next_retry_attempts,
                    base_delay_seconds=retry_base_delay_seconds,
                    max_delay_seconds=retry_max_delay_seconds,
                )
                next_attempt_at = now + timedelta(seconds=delay)
                next_attempt_at_iso = _iso(next_attempt_at)
                rowcount = conn.execute(
                    """
                    UPDATE captures
                    SET delivery_status = ?,
                        retry_attempts = ?,
                        processing_lease_until = NULL,
                        next_attempt_at = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE capture_id = ?
                      AND delivery_status = ?
                      AND processing_lease_until IS NOT NULL
                      AND processing_lease_until <= ?
                    """,
                    (
                        RETRY_WAIT,
                        next_retry_attempts,
                        next_attempt_at_iso,
                        "stale processing lease expired",
                        now_iso,
                        capture_id,
                        prev_delivery_status,
                        now_iso,
                    ),
                ).rowcount
                if rowcount > 0:
                    conn.execute(
                        "INSERT INTO capture_events (capture_id, event_type, event_payload_json, created_at) VALUES (?, 'REQUEUED_STALE_LEASE', ?, ?)",
                        (
                            capture_id,
                            _json_dumps({
                                "previous_delivery_status": prev_delivery_status,
                                "delivery_attempts": delivery_attempts,
                                "retry_attempts": next_retry_attempts,
                                "next_attempt_at": next_attempt_at_iso,
                            }),
                            now_iso,
                        ),
                    )
                    requeued.append(RequeuedLease(
                        capture_id=capture_id,
                        delivery_attempts=delivery_attempts,
                        retry_attempts=next_retry_attempts,
                        previous_delivery_status=prev_delivery_status,
                        next_attempt_at=next_attempt_at,
                    ))
            else:
                rowcount = conn.execute(
                    """
                    UPDATE captures
                    SET status = ?,
                        delivery_status = ?,
                        retry_attempts = ?,
                        processing_lease_until = NULL,
                        next_attempt_at = NULL,
                        last_error = ?,
                        updated_at = ?
                    WHERE capture_id = ?
                      AND delivery_status = ?
                      AND processing_lease_until IS NOT NULL
                      AND processing_lease_until <= ?
                    """,
                    (
                        FAILED,
                        DELIVERY_FAILED,
                        next_retry_attempts,
                        "retry limit exceeded after repeated stale processing leases",
                        now_iso,
                        capture_id,
                        prev_delivery_status,
                        now_iso,
                    ),
                ).rowcount
                if rowcount > 0:
                    conn.execute(
                        "INSERT INTO capture_events (capture_id, event_type, event_payload_json, created_at) VALUES (?, 'RETRY_LIMIT_EXCEEDED', ?, ?)",
                        (
                            capture_id,
                            _json_dumps({
                                "previous_delivery_status": prev_delivery_status,
                                "delivery_attempts": delivery_attempts,
                                "retry_attempts": next_retry_attempts,
                            }),
                            now_iso,
                        ),
                    )
                    failed.append(FailedLease(
                        capture_id=capture_id,
                        delivery_attempts=delivery_attempts,
                        retry_attempts=next_retry_attempts,
                        previous_delivery_status=prev_delivery_status,
                    ))

        return LeaseReaperResult(
            scanned=len(stale_rows),
            requeued=tuple(requeued),
            failed=tuple(failed),
        )

    def _manual_retry_capture(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        now: datetime,
    ) -> bool:
        now_iso = _iso(now)
        rowcount = conn.execute(
            """
            UPDATE captures
            SET status = ?,
                delivery_status = ?,
                retry_attempts = 0,
                processing_lease_until = NULL,
                next_attempt_at = ?,
                last_error = NULL,
                updated_at = ?
            WHERE capture_id = ?
              AND status = ?
              AND delivery_status = ?
            """,
            (RECEIVED, RETRY_WAIT, now_iso, now_iso, capture_id, FAILED, DELIVERY_FAILED),
        ).rowcount
        if rowcount > 0:
            conn.execute(
                "INSERT INTO capture_events (capture_id, event_type, event_payload_json, created_at) VALUES (?, 'MANUAL_RETRY_REQUESTED', ?, ?)",
                (capture_id, _json_dumps({"next_attempt_at": now_iso}), now_iso),
            )
        return rowcount > 0

    def _delivery_snapshot(self, conn: sqlite3.Connection) -> dict[str, Any]:
        counts = {row["delivery_status"]: row["count"] for row in conn.execute(
            "SELECT delivery_status, COUNT(*) AS count FROM captures GROUP BY delivery_status"
        ).fetchall()}
        total_attempts = conn.execute(
            "SELECT COALESCE(SUM(delivery_attempts), 0) AS total FROM captures"
        ).fetchone()["total"]
        next_row = conn.execute(
            """
            SELECT MIN(next_attempt_at) AS next
            FROM captures
            WHERE delivery_status = ? AND next_attempt_at IS NOT NULL
            """,
            (RETRY_WAIT,),
        ).fetchone()
        now_iso = _iso(_now())
        expired_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM captures
            WHERE delivery_status IN (?, ?, ?)
              AND processing_lease_until <= ?
            """,
            (FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING, now_iso),
        ).fetchone()["count"]
        active_leases_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM captures
            WHERE delivery_status IN (?, ?, ?)
              AND processing_lease_until IS NOT NULL
              AND processing_lease_until > ?
            """,
            (FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING, now_iso),
        ).fetchone()["count"]
        total_retry_row = conn.execute(
            "SELECT COALESCE(SUM(retry_attempts), 0) AS total FROM captures"
        ).fetchone()
        total_retry_attempts = int(total_retry_row["total"]) if total_retry_row else 0
        return {
            "pending_forward": counts.get(PENDING_FORWARD, 0),
            "forwarding": counts.get(FORWARDING, 0),
            "forwarded": counts.get(DELIVERY_FORWARDED, 0),
            "classifying": counts.get(DELIVERY_CLASSIFYING, 0),
            "retry_wait": counts.get(RETRY_WAIT, 0),
            "complete": counts.get(COMPLETE, 0),
            "failed": counts.get(DELIVERY_FAILED, 0),
            "not_applicable": counts.get(NOT_APPLICABLE, 0),
            "total_delivery_attempts": total_attempts,
            "total_retry_attempts": total_retry_attempts,
            "active_processing_leases": active_leases_count,
            "next_attempt_at": next_row["next"] if next_row else None,
            "expired_leases": expired_count,
        }

    def _total_captures(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS count FROM captures").fetchone()
        return int(row["count"])

    def _last_successful_vault_write(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            """
            SELECT derived_note_path
            FROM captures
            WHERE status IN (?, ?)
              AND derived_note_path IS NOT NULL
              AND derived_note_path NOT LIKE 'stub://%'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (FILED, INBOX),
        ).fetchone()
        return None if row is None else row["derived_note_path"]

    def _get_system_state(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    # ------------------------------------------------------------------
    # Shared helpers (called from both read and write contexts)
    # ------------------------------------------------------------------

    def _get_by_discord_message_id(
        self, conn: sqlite3.Connection, discord_message_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM captures WHERE discord_message_id = ?",
            (discord_message_id,),
        ).fetchone()

    def _get_by_capture_id(
        self, conn: sqlite3.Connection, capture_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"capture not found: {capture_id}")
        return row

    def _next_capture_id(self, conn: sqlite3.Connection, received_at: datetime) -> str:
        prefix = f"SB-{received_at.strftime('%Y%m%d')}-"
        row = conn.execute(
            "SELECT capture_id FROM captures WHERE capture_id LIKE ? ORDER BY capture_id DESC LIMIT 1",
            (f"{prefix}%",),
        ).fetchone()
        next_number = 1
        if row is not None:
            next_number = int(row["capture_id"].rsplit("-", 1)[1]) + 1
        return f"{prefix}{next_number:04d}"

    def _append_event(
        self,
        conn: sqlite3.Connection,
        capture_id: str,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO capture_events (
                capture_id,
                event_type,
                event_payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                capture_id,
                event_type,
                _json_dumps(event_payload or {}),
                _iso(_now()),
            ),
        )


    # ------------------------------------------------------------------
    # SB-117: Clarification private implementations
    # ------------------------------------------------------------------

    def _record_clarification(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        question: str,
    ) -> bool:
        now = _iso(_now())
        cursor = conn.execute(
            """
            UPDATE captures
            SET clarification_status = 'NEEDS_CLARIFICATION',
                clarification_question = ?,
                updated_at = ?
            WHERE capture_id = ? AND status = 'INBOX'
            """,
            (question, now, capture_id),
        )
        if cursor.rowcount == 0:
            return False
        self._append_event(
            conn,
            capture_id,
            "CLARIFICATION_SENT",
            {"question": question},
        )
        return True

    def _resolve_clarification(self, conn: sqlite3.Connection, capture_id: str) -> bool:
        now = _iso(_now())
        cursor = conn.execute(
            """
            UPDATE captures
            SET clarification_status = 'RESOLVED',
                updated_at = ?
            WHERE capture_id = ? AND clarification_status = 'NEEDS_CLARIFICATION'
            """,
            (now, capture_id),
        )
        if cursor.rowcount == 0:
            return False
        self._append_event(conn, capture_id, "CLARIFICATION_RESOLVED", {})
        return True

    def _captures_needing_clarification(self, conn: sqlite3.Connection) -> list[CaptureRecord]:
        rows = conn.execute(
            "SELECT * FROM captures WHERE clarification_status = 'NEEDS_CLARIFICATION' ORDER BY id"
        ).fetchall()
        return [_record_from_row(row) for row in rows]

    def _count_needs_clarification(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM captures WHERE clarification_status = 'NEEDS_CLARIFICATION'"
        ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # SB-118: Correction private implementations
    # ------------------------------------------------------------------

    def _get_capture_by_receipt_message_id(
        self, conn: sqlite3.Connection, receipt_message_id: str
    ) -> CaptureRecord | None:
        row = conn.execute(
            "SELECT * FROM captures WHERE receipt_message_id = ?",
            (receipt_message_id,),
        ).fetchone()
        return None if row is None else _record_from_row(row)

    def _record_correction(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        old_note_path: str,
        new_note_path: str,
        git_commit_hash: str | None,
        correction_reason: str | None,
        move_outcome: str = "moved",
    ) -> str:
        now = _iso(_now())
        correction_id = f"COR-{uuid.uuid4().hex}-{capture_id}"
        conn.execute(
            """
            INSERT INTO corrections
                (correction_id, capture_id, old_note_path, new_note_path,
                 git_commit_hash, correction_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (correction_id, capture_id, old_note_path, new_note_path,
             git_commit_hash, correction_reason, now),
        )
        if move_outcome != "no_op":
            conn.execute(
                """
                UPDATE captures
                SET derived_note_path = ?, delivery_commit_hash = ?, updated_at = ?
                WHERE capture_id = ?
                """,
                (new_note_path, git_commit_hash, now, capture_id),
            )
        self._append_event(
            conn,
            capture_id,
            "CORRECTION_APPLIED",
            {
                "correction_id": correction_id,
                "old_note_path": old_note_path,
                "new_note_path": new_note_path,
                "git_commit_hash": git_commit_hash,
                "correction_reason": correction_reason,
                "move_outcome": move_outcome,
            },
        )
        return correction_id

    def _corrections_for_capture(
        self, conn: sqlite3.Connection, capture_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT correction_id, capture_id, old_note_path, new_note_path,
                   git_commit_hash, correction_reason, created_at
            FROM corrections
            WHERE capture_id = ?
            ORDER BY id
            """,
            (capture_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # SB-136: Vault update proposal private implementations
    # ------------------------------------------------------------------

    def _next_proposal_id(self, conn: sqlite3.Connection, submitted_at: datetime) -> str:
        prefix = f"VUP-{submitted_at.strftime('%Y%m%d')}-"
        row = conn.execute(
            "SELECT proposal_id FROM vault_update_proposals WHERE proposal_id LIKE ? ORDER BY proposal_id DESC LIMIT 1",
            (f"{prefix}%",),
        ).fetchone()
        next_number = 1
        if row is not None:
            next_number = int(row["proposal_id"].rsplit("-", 1)[1]) + 1
        return f"{prefix}{next_number:04d}"

    def _create_proposal(
        self,
        conn: sqlite3.Connection,
        *,
        source: str,
        requested_by: str,
        operation: str,
        target_note_path: str,
        target_anchor_json: str | None,
        change_json: str,
        reason: str | None,
        requires_approval: bool,
        submitted_at: datetime,
    ) -> ProposalRecord:
        proposal_id = self._next_proposal_id(conn, submitted_at)
        submitted_at_iso = _iso(submitted_at)
        conn.execute(
            """
            INSERT INTO vault_update_proposals (
                proposal_id, source, requested_by, operation,
                target_note_path, target_anchor_json, change_json, reason,
                status, requires_approval, submitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            (
                proposal_id, source, requested_by, operation,
                target_note_path, target_anchor_json, change_json, reason,
                1 if requires_approval else 0, submitted_at_iso,
            ),
        )
        return self._get_proposal(conn, proposal_id)

    def _get_proposal(self, conn: sqlite3.Connection, proposal_id: str) -> ProposalRecord:
        row = conn.execute(
            "SELECT * FROM vault_update_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        return _proposal_from_row(row)

    def _list_proposals(
        self, conn: sqlite3.Connection, *, status: str | None, limit: int
    ) -> list[ProposalRecord]:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM vault_update_proposals WHERE status = ? ORDER BY submitted_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vault_update_proposals ORDER BY submitted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_proposal_from_row(row) for row in rows]

    def _update_proposal(
        self,
        conn: sqlite3.Connection,
        proposal_id: str,
        *,
        status: str | None,
        reviewed_at: datetime | None,
        reviewed_by: str | None,
        applied_at: datetime | None,
        rejected_reason: str | None,
        git_commit_hash: str | None,
        last_error: str | None,
        approval_message_id: str | None,
    ) -> ProposalRecord:
        row = conn.execute(
            "SELECT * FROM vault_update_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        sets = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if reviewed_at is not None:
            sets.append("reviewed_at = ?")
            params.append(_iso(reviewed_at))
        if reviewed_by is not None:
            sets.append("reviewed_by = ?")
            params.append(reviewed_by)
        if applied_at is not None:
            sets.append("applied_at = ?")
            params.append(_iso(applied_at))
        if rejected_reason is not None:
            sets.append("rejected_reason = ?")
            params.append(rejected_reason)
        if git_commit_hash is not None:
            sets.append("git_commit_hash = ?")
            params.append(git_commit_hash)
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        if approval_message_id is not None:
            sets.append("approval_message_id = ?")
            params.append(approval_message_id)
        if sets:
            params.append(proposal_id)
            conn.execute(
                f"UPDATE vault_update_proposals SET {', '.join(sets)} WHERE proposal_id = ?",
                params,
            )
        return self._get_proposal(conn, proposal_id)

    def _daily_digest_snapshot(
        self, conn: sqlite3.Connection, *, since: datetime, now: datetime
    ) -> dict:
        since_iso = _iso(since)
        now_iso = _iso(now)

        new_captures = conn.execute(
            "SELECT COUNT(*) AS c FROM captures WHERE received_at >= ? AND received_at < ?",
            (since_iso, now_iso),
        ).fetchone()["c"]

        filed_notes = conn.execute(
            """
            SELECT COUNT(*) AS c FROM capture_events
            WHERE event_type = 'CAPTURE_FILED' AND created_at >= ? AND created_at < ?
            """,
            (since_iso, now_iso),
        ).fetchone()["c"]

        inbox_backlog = conn.execute(
            "SELECT COUNT(*) AS c FROM captures WHERE status = 'INBOX'"
        ).fetchone()["c"]

        awaiting_clarification = conn.execute(
            "SELECT COUNT(*) AS c FROM captures WHERE clarification_status = 'NEEDS_CLARIFICATION'"
        ).fetchone()["c"]

        failed_captures = conn.execute(
            """
            SELECT COUNT(*) AS c FROM capture_events
            WHERE event_type IN ('RETRY_LIMIT_EXCEEDED', 'DELIVERY_FAILED_TERMINALLY')
              AND created_at >= ? AND created_at < ?
            """,
            (since_iso, now_iso),
        ).fetchone()["c"]

        retry_events = conn.execute(
            """
            SELECT COUNT(*) AS c FROM capture_events
            WHERE event_type = 'DELIVERY_RETRY_SCHEDULED'
              AND created_at >= ? AND created_at < ?
            """,
            (since_iso, now_iso),
        ).fetchone()["c"]

        sensitive_rejections = conn.execute(
            """
            SELECT COUNT(*) AS c FROM captures
            WHERE is_sensitive = 1 AND received_at >= ? AND received_at < ?
            """,
            (since_iso, now_iso),
        ).fetchone()["c"]

        attachment_warnings = conn.execute(
            """
            SELECT COUNT(*) AS c FROM captures
            WHERE has_attachments = 1
              AND status NOT IN ('FILED', 'INBOX', 'REJECTED_SENSITIVE', 'FAILED')
            """
        ).fetchone()["c"]

        return {
            "new_captures_count": int(new_captures),
            "filed_notes_count": int(filed_notes),
            "inbox_backlog_count": int(inbox_backlog),
            "awaiting_clarification_count": int(awaiting_clarification),
            "failed_captures_count": int(failed_captures),
            "retry_events_count": int(retry_events),
            "sensitive_rejections_count": int(sensitive_rejections),
            "attachment_warnings_count": int(attachment_warnings),
        }

    def _weekly_digest_snapshot(
        self, conn: sqlite3.Connection, *, since: datetime, now: datetime
    ) -> dict:
        since_iso = _iso(since)
        now_iso = _iso(now)

        new_captures = conn.execute(
            "SELECT COUNT(*) AS c FROM captures WHERE received_at >= ? AND received_at < ?",
            (since_iso, now_iso),
        ).fetchone()["c"]

        filed_notes = conn.execute(
            """
            SELECT COUNT(*) AS c FROM capture_events
            WHERE event_type IN ('CAPTURE_FILED', 'CAPTURE_INBOX')
              AND created_at >= ? AND created_at < ?
            """,
            (since_iso, now_iso),
        ).fetchone()["c"]

        # Count note types for filed captures in the window
        note_type_rows = conn.execute(
            """
            SELECT json_extract(c.classification_json, '$.note_type') AS note_type,
                   COUNT(*) AS cnt
            FROM captures c
            JOIN capture_events e ON e.capture_id = c.capture_id
            WHERE e.event_type IN ('CAPTURE_FILED', 'CAPTURE_INBOX')
              AND e.created_at >= ? AND e.created_at < ?
              AND c.classification_json IS NOT NULL
              AND c.is_sensitive = 0
            GROUP BY note_type
            """,
            (since_iso, now_iso),
        ).fetchall()
        note_type_counts: dict[str, int] = {
            row["note_type"]: int(row["cnt"])
            for row in note_type_rows
            if row["note_type"] is not None
        }

        inbox_backlog = conn.execute(
            "SELECT COUNT(*) AS c FROM captures WHERE status = 'INBOX'"
        ).fetchone()["c"]

        corrections = conn.execute(
            "SELECT COUNT(*) AS c FROM corrections WHERE created_at >= ? AND created_at < ?",
            (since_iso, now_iso),
        ).fetchone()["c"]

        failures = conn.execute(
            """
            SELECT COUNT(*) AS c FROM capture_events
            WHERE event_type IN ('RETRY_LIMIT_EXCEEDED', 'DELIVERY_FAILED_TERMINALLY')
              AND created_at >= ? AND created_at < ?
            """,
            (since_iso, now_iso),
        ).fetchone()["c"]

        retries = conn.execute(
            """
            SELECT COUNT(*) AS c FROM capture_events
            WHERE event_type = 'DELIVERY_RETRY_SCHEDULED'
              AND created_at >= ? AND created_at < ?
            """,
            (since_iso, now_iso),
        ).fetchone()["c"]

        sensitive_rejections = conn.execute(
            """
            SELECT COUNT(*) AS c FROM captures
            WHERE is_sensitive = 1 AND received_at >= ? AND received_at < ?
            """,
            (since_iso, now_iso),
        ).fetchone()["c"]

        return {
            "new_captures_count": int(new_captures),
            "filed_notes_count": int(filed_notes),
            "created_tasks_count": note_type_counts.get("task", 0),
            "completed_actions_count": note_type_counts.get("done", 0) + note_type_counts.get("fix", 0),
            "decisions_count": note_type_counts.get("decision", 0),
            "inbox_backlog_count": int(inbox_backlog),
            "corrections_count": int(corrections),
            "failures_count": int(failures),
            "retries_count": int(retries),
            "sensitive_rejections_count": int(sensitive_rejections),
        }


def _record_from_row(row: sqlite3.Row) -> CaptureRecord:
    def _parse_dt(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    keys = row.keys()
    return CaptureRecord(
        capture_id=row["capture_id"],
        discord_message_id=row["discord_message_id"],
        discord_channel_id=row["discord_channel_id"],
        discord_guild_id=row["discord_guild_id"],
        discord_author_id=row["discord_author_id"],
        status=row["status"],
        delivery_status=row["delivery_status"],
        delivery_attempts=row["delivery_attempts"],
        processing_lease_until=_parse_dt(row["processing_lease_until"]),
        next_attempt_at=_parse_dt(row["next_attempt_at"]),
        raw_text=row["raw_text"],
        redacted_text=row["redacted_text"],
        is_sensitive=bool(row["is_sensitive"]),
        has_attachments=bool(row["has_attachments"]),
        attachment_metadata=json.loads(row["attachment_metadata_json"] or "[]"),
        received_at=datetime.fromisoformat(row["received_at"]),
        receipt_message_id=row["receipt_message_id"],
        derived_note_path=row["derived_note_path"],
        last_error=row["last_error"],
        retry_attempts=row["retry_attempts"] if "retry_attempts" in keys else 0,
        delivery_commit_hash=row["delivery_commit_hash"],
        delivery_reason_type=row["delivery_reason_type"],
        clarification_status=row["clarification_status"] if "clarification_status" in keys else None,
        clarification_question=row["clarification_question"] if "clarification_question" in keys else None,
    )


def _proposal_from_row(row: sqlite3.Row) -> ProposalRecord:
    def _parse_dt(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    keys = row.keys()
    return ProposalRecord(
        proposal_id=row["proposal_id"],
        source=row["source"],
        requested_by=row["requested_by"],
        operation=row["operation"],
        target_note_path=row["target_note_path"],
        target_anchor_json=row["target_anchor_json"],
        change_json=row["change_json"],
        reason=row["reason"],
        status=row["status"],
        requires_approval=bool(row["requires_approval"]),
        submitted_at=datetime.fromisoformat(row["submitted_at"]),
        reviewed_at=_parse_dt(row["reviewed_at"]),
        reviewed_by=row["reviewed_by"],
        applied_at=_parse_dt(row["applied_at"]),
        rejected_reason=row["rejected_reason"],
        git_commit_hash=row["git_commit_hash"],
        last_error=row["last_error"],
        approval_message_id=row["approval_message_id"] if "approval_message_id" in keys else None,
    )


def calculate_retry_delay_seconds(
    *,
    retry_attempts: int,
    base_delay_seconds: int,
    max_delay_seconds: int,
) -> int:
    delay = base_delay_seconds * (2 ** max(retry_attempts - 1, 0))
    return min(delay, max_delay_seconds)



def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _validate_status(status: str) -> None:
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown capture status: {status}")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()
