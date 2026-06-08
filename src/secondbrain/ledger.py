from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any

from secondbrain.capture_models import (
    ALL_STATUSES,
    CLASSIFYING,
    FAILED,
    FILED,
    FORWARDED,
    INBOX,
    RECEIVED,
    REJECTED_SENSITIVE,
    TERMINAL_STATUSES,
    CaptureRecord,
    TransitionResult,
)


UNSET = object()


@dataclass(frozen=True)
class InsertResult:
    capture: CaptureRecord
    created: bool


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def close(self) -> None:
        self._connection.close()

    def migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL UNIQUE,
                    discord_message_id TEXT NOT NULL UNIQUE,
                    discord_channel_id TEXT NOT NULL,
                    discord_guild_id TEXT NOT NULL,
                    discord_author_id TEXT NOT NULL,

                    raw_text TEXT,
                    redacted_text TEXT,
                    is_sensitive INTEGER NOT NULL DEFAULT 0,
                    sensitivity_flags TEXT,

                    has_attachments INTEGER NOT NULL DEFAULT 0,
                    attachment_metadata_json TEXT,

                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    classification_json TEXT,
                    derived_note_path TEXT,
                    receipt_message_id TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,

                    CHECK (
                        (
                            is_sensitive = 0
                            AND raw_text IS NOT NULL
                            AND (raw_text != '' OR has_attachments = 1)
                        )
                        OR
                        (is_sensitive = 1 AND raw_text IS NULL AND redacted_text IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS capture_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_payload_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
                );

                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_captures_status ON captures(status);
                CREATE INDEX IF NOT EXISTS idx_capture_events_capture_id
                    ON capture_events(capture_id);
                """
            )

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
    ) -> InsertResult:
        received_at = received_at or _now()
        with self._lock, self._connection:
            existing = self._get_by_discord_message_id(discord_message_id)
            if existing is not None:
                return InsertResult(capture=_record_from_row(existing), created=False)

            capture_id = self._next_capture_id(received_at)
            now = _iso(_now())
            self._connection.execute(
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
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
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
                    now,
                ),
            )
            self._append_event(capture_id, "CAPTURE_RECEIVED", {"status": RECEIVED})
            return InsertResult(capture=_record_from_row(self._get_by_capture_id(capture_id)), created=True)

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
        with self._lock, self._connection:
            existing = self._get_by_discord_message_id(discord_message_id)
            if existing is not None:
                return InsertResult(capture=_record_from_row(existing), created=False)

            capture_id = self._next_capture_id(received_at)
            now = _iso(_now())
            self._connection.execute(
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
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?, ?)
                """,
                (
                    capture_id,
                    discord_message_id,
                    discord_channel_id,
                    discord_guild_id,
                    discord_author_id,
                    redacted_text,
                    _json_dumps(list(sensitivity_flags)),
                    _iso(received_at),
                    REJECTED_SENSITIVE,
                    now,
                ),
            )
            self._append_event(
                capture_id,
                "CAPTURE_REJECTED_SENSITIVE",
                {"flags": list(sensitivity_flags)},
            )
            return InsertResult(capture=_record_from_row(self._get_by_capture_id(capture_id)), created=True)

    def get_capture(self, capture_id: str) -> CaptureRecord:
        row = self._connection.execute(
            "SELECT * FROM captures WHERE capture_id = ?",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"capture not found: {capture_id}")
        return _record_from_row(row)

    def set_receipt_message_id(self, capture_id: str, receipt_message_id: str) -> None:
        self.update_capture(
            capture_id,
            receipt_message_id=receipt_message_id,
            event_type="RECEIPT_STORED",
            event_payload={"receipt_message_id": receipt_message_id},
        )

    def mark_classifying(self, capture_id: str) -> bool:
        with self._lock, self._connection:
            now = _iso(_now())
            cursor = self._connection.execute(
                """
                UPDATE captures
                SET status = ?, updated_at = ?
                WHERE capture_id = ? AND status = ?
                """,
                (CLASSIFYING, now, capture_id, RECEIVED),
            )
            if cursor.rowcount == 0:
                return False
            self._append_event(capture_id, "CAPTURE_CLASSIFYING", {"status": CLASSIFYING})
            return True

    def reset_classifying_to_received(self) -> int:
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT capture_id FROM captures WHERE status = ? ORDER BY id",
                (CLASSIFYING,),
            ).fetchall()
            if not rows:
                return 0

            now = _iso(_now())
            self._connection.execute(
                """
                UPDATE captures
                SET status = ?, updated_at = ?
                WHERE status = ?
                """,
                (RECEIVED, now, CLASSIFYING),
            )
            for row in rows:
                self._append_event(
                    row["capture_id"],
                    "CAPTURE_REQUEUED",
                    {"from_status": CLASSIFYING, "status": RECEIVED},
                )
            return len(rows)

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
        updates: list[str] = []
        values: list[Any] = []
        if status is not None:
            _validate_status(status)
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

        if not updates and event_type is None:
            return

        with self._lock, self._connection:
            if updates:
                updates.append("updated_at = ?")
                values.append(_iso(_now()))
                values.append(capture_id)
                self._connection.execute(
                    f"UPDATE captures SET {', '.join(updates)} WHERE capture_id = ?",
                    values,
                )
            if event_type is not None:
                self._append_event(capture_id, event_type, event_payload or {})

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
    ) -> TransitionResult | None:
        _validate_status(to_status)
        for status in from_statuses:
            _validate_status(status)

        with self._lock, self._connection:
            current_row = self._get_by_capture_id(capture_id)
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

            updates.append("updated_at = ?")
            values.append(_iso(_now()))
            values.append(capture_id)
            values.extend(sorted(from_statuses))

            placeholders = ", ".join("?" for _ in from_statuses)
            cursor = self._connection.execute(
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
            self._append_event(capture_id, event_type, event_payload or {})
            return TransitionResult(
                capture_id=capture_id,
                previous_status=previous_status,
                status=to_status,
                changed=True,
            )

    def capture_classification_json(self, capture_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT classification_json FROM captures WHERE capture_id = ?",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"capture not found: {capture_id}")
        if row["classification_json"] is None:
            return None
        return json.loads(row["classification_json"])

    def enqueueable_capture_ids(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT capture_id FROM captures WHERE status IN (?, ?, ?) ORDER BY id",
            (RECEIVED, FORWARDED, CLASSIFYING),
        ).fetchall()
        return [row["capture_id"] for row in rows]

    def captures_by_status(self, status: str) -> list[CaptureRecord]:
        rows = self._connection.execute(
            "SELECT * FROM captures WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
        return [_record_from_row(row) for row in rows]

    def status_counts(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS count FROM captures GROUP BY status"
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def total_captures(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM captures").fetchone()
        return int(row["count"])

    def ping(self) -> None:
        self._connection.execute("SELECT 1").fetchone()

    def last_successful_vault_write(self) -> str | None:
        row = self._connection.execute(
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

    def set_system_state(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, _iso(_now())),
            )

    def advance_system_state_snowflake(self, key: str, candidate: str) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT value FROM system_state WHERE key = ?",
                (key,),
            ).fetchone()
            if row is not None and int(candidate) <= int(row["value"]):
                return

            self._connection.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, candidate, _iso(_now())),
            )

    def get_system_state(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM system_state WHERE key = ?",
            (key,),
        ).fetchone()
        return None if row is None else row["value"]

    def _get_by_discord_message_id(self, discord_message_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM captures WHERE discord_message_id = ?",
            (discord_message_id,),
        ).fetchone()

    def _get_by_capture_id(self, capture_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM captures WHERE capture_id = ?",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"capture not found: {capture_id}")
        return row

    def _next_capture_id(self, received_at: datetime) -> str:
        prefix = f"SB-{received_at.strftime('%Y%m%d')}-"
        row = self._connection.execute(
            "SELECT capture_id FROM captures WHERE capture_id LIKE ? ORDER BY capture_id DESC LIMIT 1",
            (f"{prefix}%",),
        ).fetchone()
        next_number = 1
        if row is not None:
            next_number = int(row["capture_id"].rsplit("-", 1)[1]) + 1
        return f"{prefix}{next_number:04d}"

    def _append_event(
        self,
        capture_id: str,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        self._assert_mutation_lock_held()
        self._connection.execute(
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

    def _assert_mutation_lock_held(self) -> None:
        if not self._lock.locked():
            raise RuntimeError("ledger mutation lock must be held for SQLite writes")


def _record_from_row(row: sqlite3.Row) -> CaptureRecord:
    return CaptureRecord(
        capture_id=row["capture_id"],
        discord_message_id=row["discord_message_id"],
        discord_channel_id=row["discord_channel_id"],
        discord_guild_id=row["discord_guild_id"],
        discord_author_id=row["discord_author_id"],
        status=row["status"],
        raw_text=row["raw_text"],
        redacted_text=row["redacted_text"],
        is_sensitive=bool(row["is_sensitive"]),
        has_attachments=bool(row["has_attachments"]),
        attachment_metadata=json.loads(row["attachment_metadata_json"] or "[]"),
        received_at=datetime.fromisoformat(row["received_at"]),
        receipt_message_id=row["receipt_message_id"],
        derived_note_path=row["derived_note_path"],
        last_error=row["last_error"],
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _validate_status(status: str) -> None:
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown capture status: {status}")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()
