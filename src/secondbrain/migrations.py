from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from secondbrain.observability import log_metadata


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


_MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="initial_mvp_schema",
        statements=(
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS capture_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                capture_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_captures_status ON captures(status)",
            "CREATE INDEX IF NOT EXISTS idx_capture_events_capture_id ON capture_events(capture_id)",
        ),
    ),
    Migration(
        version=2,
        name="delivery_leases",
        statements=(
            "ALTER TABLE captures ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'PENDING_FORWARD'",
            "ALTER TABLE captures ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE captures ADD COLUMN processing_lease_until TEXT",
            "ALTER TABLE captures ADD COLUMN next_attempt_at TEXT",
            # Normalize existing rows by lifecycle status
            "UPDATE captures SET delivery_status = 'NOT_APPLICABLE' WHERE status = 'REJECTED_SENSITIVE'",
            "UPDATE captures SET delivery_status = 'COMPLETE' WHERE status IN ('FILED', 'INBOX')",
            "UPDATE captures SET delivery_status = 'FAILED' WHERE status = 'FAILED'",
            # Reset in-flight legacy statuses to retryable RECEIVED/PENDING_FORWARD
            "UPDATE captures SET status = 'RECEIVED', delivery_status = 'PENDING_FORWARD' WHERE status IN ('CLASSIFYING', 'FORWARDED')",
            # Indexes for dispatcher and reaper hot paths
            "CREATE INDEX IF NOT EXISTS idx_captures_delivery_due ON captures(delivery_status, next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS idx_captures_processing_lease ON captures(delivery_status, processing_lease_until)",
        ),
    ),
    Migration(
        version=3,
        name="terminal_delivery_fields",
        statements=(
            # Store git commit hash and outcome reason for idempotent terminal callbacks
            "ALTER TABLE captures ADD COLUMN delivery_commit_hash TEXT",
            "ALTER TABLE captures ADD COLUMN delivery_reason_type TEXT",
        ),
    ),
    Migration(
        version=4,
        name="stale_lease_reaper",
        statements=(
            "ALTER TABLE captures ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_captures_stale_lease ON captures(delivery_status, processing_lease_until)",
            "CREATE INDEX IF NOT EXISTS idx_captures_retry_due ON captures(delivery_status, next_attempt_at)",
        ),
    ),
    Migration(
        version=5,
        name="clarifications_and_corrections",
        statements=(
            # SB-117: clarification sub-state on captures
            "ALTER TABLE captures ADD COLUMN clarification_status TEXT",
            "ALTER TABLE captures ADD COLUMN clarification_question TEXT",
            # SB-118: append-only correction history
            """
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correction_id TEXT NOT NULL UNIQUE,
                capture_id TEXT NOT NULL,
                old_note_path TEXT NOT NULL,
                new_note_path TEXT NOT NULL,
                git_commit_hash TEXT,
                correction_reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_corrections_capture_id ON corrections(capture_id)",
            "CREATE INDEX IF NOT EXISTS idx_captures_clarification ON captures(clarification_status)",
        ),
    ),
]


def run_migrations(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name    TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)

    applied = {
        row[0]
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }

    for migration in sorted(_MIGRATIONS, key=lambda m: m.version):
        if migration.version in applied:
            continue
        _apply(connection, migration)


def _apply(connection: sqlite3.Connection, migration: Migration) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        already_applied = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (migration.version,),
        ).fetchone()
        if already_applied is not None:
            connection.commit()
            return

        for statement in migration.statements:
            connection.execute(statement)

        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(UTC).isoformat()),
        )
    except BaseException:
        connection.rollback()
        raise

    connection.commit()
    log_metadata(
        "sqlite_migration_applied",
        version=migration.version,
        name=migration.name,
    )
