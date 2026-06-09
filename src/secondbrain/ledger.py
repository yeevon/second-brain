from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from secondbrain.capture_models import (
    ALL_DELIVERY_STATUSES,
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
    RECEIVED,
    REJECTED_SENSITIVE,
    RETRY_WAIT,
    TERMINAL_STATUSES,
    CaptureRecord,
    DeliveryMutationResult,
    RetryDisposition,
    TransitionResult,
)
from secondbrain.sqlite_runtime import SQLiteRuntime


UNSET = object()


@dataclass(frozen=True)
class InsertResult:
    capture: CaptureRecord
    created: bool


class Ledger:
    def __init__(self, path: Path | str, settings: Any = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        busy_timeout_ms      = getattr(settings, "sqlite_busy_timeout_ms",       1000)  if settings else 1000
        retry_attempts       = getattr(settings, "sqlite_busy_retry_attempts",      5)  if settings else 5
        retry_base_delay_ms  = getattr(settings, "sqlite_busy_retry_base_delay_ms", 25) if settings else 25
        job_queue_maxsize    = getattr(settings, "sqlite_job_queue_maxsize",     10000) if settings else 10000

        self._runtime = SQLiteRuntime(
            self.path,
            busy_timeout_ms=busy_timeout_ms,
            retry_attempts=retry_attempts,
            retry_base_delay_ms=retry_base_delay_ms,
            job_queue_maxsize=job_queue_maxsize,
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
    ) -> bool:
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
    ) -> bool:
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
    ) -> DeliveryMutationResult:
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
    ) -> bool:
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
    ) -> bool:
        return self._write(
            "mark_delivery_failed_terminally",
            lambda conn: self._mark_delivery_failed_terminally(
                conn,
                capture_id=capture_id,
                delivery_attempt=delivery_attempt,
                reason=reason,
            ),
        )

    def normalize_delivery_for_local_full(self) -> int:
        """Set PENDING_FORWARD/RETRY_WAIT captures to NOT_APPLICABLE for local-full mode startup."""
        return self._write(
            "normalize_delivery_for_local_full",
            self._normalize_delivery_for_local_full,
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
    ) -> bool:
        lease_iso = _iso(lease_until)
        row = self._get_by_capture_id(conn, capture_id)
        if row["delivery_status"] != FORWARDING or row["delivery_attempts"] != delivery_attempt:
            return False
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
        return True

    def _mark_classifying_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        lease_until: datetime,
    ) -> bool:
        lease_iso = _iso(lease_until)
        row = self._get_by_capture_id(conn, capture_id)
        if row["delivery_attempts"] != delivery_attempt:
            return False
        current_ds = row["delivery_status"]
        if current_ds not in (DELIVERY_FORWARDED, DELIVERY_CLASSIFYING):
            return False
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
        return True

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
    ) -> bool:
        row = self._get_by_capture_id(conn, capture_id)
        if row["delivery_attempts"] != delivery_attempt:
            return False
        if row["delivery_status"] not in LEASABLE_DELIVERY_STATUSES:
            return False
        conn.execute(
            """
            UPDATE captures
            SET processing_lease_until = ?, updated_at = ?
            WHERE capture_id = ?
            """,
            (_iso(lease_until), _iso(_now()), capture_id),
        )
        return True

    def _mark_delivery_failed_terminally(
        self,
        conn: sqlite3.Connection,
        *,
        capture_id: str,
        delivery_attempt: int,
        reason: str,
    ) -> bool:
        row = self._get_by_capture_id(conn, capture_id)
        if row["delivery_attempts"] != delivery_attempt:
            return False
        current_ds = row["delivery_status"]
        if current_ds not in (FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING):
            return False
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
        return True

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
        row = self._get_by_capture_id(conn, capture_id)
        current_ds = row["delivery_status"]
        if current_ds not in (FORWARDING, DELIVERY_FORWARDED, DELIVERY_CLASSIFYING):
            raise ValueError(f"cannot schedule retry from delivery_status={current_ds!r}")
        if row["delivery_attempts"] != delivery_attempt:
            raise ValueError(
                f"stale delivery_attempt: got {delivery_attempt}, "
                f"current={row['delivery_attempts']}"
            )

        now_ts = _iso(now)
        safe_reason = f"{reason_type}:{error_type}"

        if delivery_attempt >= max_attempts:
            # Terminal failure
            conn.execute(
                """
                UPDATE captures
                SET status = ?, delivery_status = ?, processing_lease_until = NULL,
                    next_attempt_at = NULL, last_error = ?, updated_at = ?
                WHERE capture_id = ?
                """,
                (FAILED, DELIVERY_FAILED, safe_reason, now_ts, capture_id),
            )
            self._append_event(
                conn,
                capture_id,
                "RETRY_LIMIT_EXCEEDED",
                {
                    "delivery_attempt": delivery_attempt,
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
            )

        delay = _calculate_retry_delay(
            delivery_attempts=delivery_attempt,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )
        from datetime import timedelta
        next_attempt = now + timedelta(seconds=delay)
        next_iso = _iso(next_attempt)
        conn.execute(
            """
            UPDATE captures
            SET delivery_status = ?, processing_lease_until = NULL, next_attempt_at = ?,
                last_error = ?, updated_at = ?
            WHERE capture_id = ?
            """,
            (RETRY_WAIT, next_iso, safe_reason, now_ts, capture_id),
        )
        self._append_event(
            conn,
            capture_id,
            "DELIVERY_RETRY_SCHEDULED",
            {
                "delivery_attempt": delivery_attempt,
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
        )

    def _normalize_delivery_for_local_full(self, conn: sqlite3.Connection) -> int:
        """Normalize PENDING_FORWARD/RETRY_WAIT rows to NOT_APPLICABLE for local-full mode."""
        now = _iso(_now())
        cursor = conn.execute(
            """
            UPDATE captures
            SET delivery_status = ?, updated_at = ?
            WHERE delivery_status IN (?, ?)
            """,
            (NOT_APPLICABLE, now, PENDING_FORWARD, RETRY_WAIT),
        )
        return cursor.rowcount

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
            WHERE status IN (?, ?) AND derived_note_path IS NOT NULL
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


def _record_from_row(row: sqlite3.Row) -> CaptureRecord:
    def _parse_dt(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

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
        delivery_commit_hash=row["delivery_commit_hash"],
        delivery_reason_type=row["delivery_reason_type"],
    )


def _calculate_retry_delay(
    *,
    delivery_attempts: int,
    base_delay_seconds: int,
    max_delay_seconds: int,
) -> int:
    delay = base_delay_seconds * (2 ** max(delivery_attempts - 1, 0))
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
