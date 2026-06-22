from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

from secondbrain.observability import log_metadata


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    not_null: bool = False


@dataclass(frozen=True)
class ForeignKeySpec:
    from_column: str
    to_table: str
    to_column: str


@dataclass(frozen=True)
class SchemaAssertion:
    """Read-only schema check run once after a migration is applied."""
    table: str
    expected_columns: tuple[ColumnSpec, ...]
    expected_indexes: tuple[str, ...] = field(default_factory=tuple)
    expected_foreign_keys: tuple[ForeignKeySpec, ...] = field(default_factory=tuple)
    allow_triggers: bool = False
    allow_views: bool = False

    def verify(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(f"PRAGMA table_info({self.table})").fetchall()
        if not rows:
            raise RuntimeError(
                f"Schema assertion failed: table '{self.table}' does not exist"
            )
        actual = {row["name"]: row for row in rows}
        for spec in self.expected_columns:
            if spec.name not in actual:
                raise RuntimeError(
                    f"Schema assertion failed: column '{spec.name}' missing from '{self.table}'"
                )
            row = actual[spec.name]
            if row["type"].upper() != spec.type.upper():
                raise RuntimeError(
                    f"Schema assertion failed: column '{self.table}.{spec.name}' "
                    f"has type '{row['type']}', expected '{spec.type}'"
                )
            if spec.not_null and not row["notnull"]:
                raise RuntimeError(
                    f"Schema assertion failed: column '{self.table}.{spec.name}' "
                    f"must be NOT NULL"
                )
        if self.expected_indexes:
            idx_rows = conn.execute(f"PRAGMA index_list({self.table})").fetchall()
            idx_names = {r["name"] for r in idx_rows}
            for idx_name in self.expected_indexes:
                if idx_name not in idx_names:
                    raise RuntimeError(
                        f"Schema assertion failed: index '{idx_name}' missing from '{self.table}'"
                    )
        if self.expected_foreign_keys:
            fk_rows = conn.execute(f"PRAGMA foreign_key_list({self.table})").fetchall()
            # Build set of (from_col, to_table, to_col) from actual FK constraints
            actual_fks = {
                (r["from"], r["table"], r["to"])
                for r in fk_rows
            }
            for fk in self.expected_foreign_keys:
                key = (fk.from_column, fk.to_table, fk.to_column)
                if key not in actual_fks:
                    raise RuntimeError(
                        f"Schema assertion failed: foreign key "
                        f"'{self.table}.{fk.from_column}' -> '{fk.to_table}.{fk.to_column}' missing"
                    )
        if not self.allow_triggers:
            trigger_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                (self.table,),
            ).fetchall()
            if trigger_rows:
                names = ", ".join(r["name"] for r in trigger_rows)
                raise RuntimeError(
                    f"Schema assertion failed: unexpected trigger(s) on '{self.table}': {names}"
                )
        if not self.allow_views:
            view_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view'",
            ).fetchall()
            if view_rows:
                names = ", ".join(r["name"] for r in view_rows)
                raise RuntimeError(
                    f"Schema assertion failed: unexpected view(s) in schema: {names}"
                )


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    assertions: tuple[SchemaAssertion, ...] = field(default_factory=tuple)


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
        assertions=(
            SchemaAssertion(
                table="captures",
                expected_columns=(
                    ColumnSpec("capture_id", "TEXT", not_null=True),
                    ColumnSpec("discord_message_id", "TEXT", not_null=True),
                    ColumnSpec("discord_channel_id", "TEXT", not_null=True),
                    ColumnSpec("discord_guild_id", "TEXT", not_null=True),
                    ColumnSpec("discord_author_id", "TEXT", not_null=True),
                    ColumnSpec("raw_text", "TEXT"),
                    ColumnSpec("redacted_text", "TEXT"),
                    ColumnSpec("is_sensitive", "INTEGER", not_null=True),
                    ColumnSpec("sensitivity_flags", "TEXT"),
                    ColumnSpec("has_attachments", "INTEGER", not_null=True),
                    ColumnSpec("attachment_metadata_json", "TEXT"),
                    ColumnSpec("received_at", "TEXT", not_null=True),
                    ColumnSpec("status", "TEXT", not_null=True),
                    ColumnSpec("classification_json", "TEXT"),
                    ColumnSpec("derived_note_path", "TEXT"),
                    ColumnSpec("receipt_message_id", "TEXT"),
                    ColumnSpec("last_error", "TEXT"),
                    ColumnSpec("updated_at", "TEXT", not_null=True),
                ),
                expected_indexes=("idx_captures_status",),
            ),
            SchemaAssertion(
                table="capture_events",
                expected_columns=(
                    ColumnSpec("capture_id", "TEXT", not_null=True),
                    ColumnSpec("event_type", "TEXT", not_null=True),
                    ColumnSpec("event_payload_json", "TEXT"),
                    ColumnSpec("created_at", "TEXT", not_null=True),
                ),
                expected_indexes=("idx_capture_events_capture_id",),
                expected_foreign_keys=(
                    ForeignKeySpec("capture_id", "captures", "capture_id"),
                ),
            ),
            SchemaAssertion(
                table="system_state",
                expected_columns=(
                    ColumnSpec("key", "TEXT"),  # PRIMARY KEY; SQLite notnull=0 for non-INTEGER PKs
                    ColumnSpec("value", "TEXT", not_null=True),
                    ColumnSpec("updated_at", "TEXT", not_null=True),
                ),
            ),
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
        assertions=(
            SchemaAssertion(
                table="captures",
                expected_columns=(
                    ColumnSpec("delivery_status", "TEXT", not_null=True),
                    ColumnSpec("delivery_attempts", "INTEGER", not_null=True),
                    ColumnSpec("processing_lease_until", "TEXT"),
                    ColumnSpec("next_attempt_at", "TEXT"),
                ),
            ),
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
        assertions=(
            SchemaAssertion(
                table="captures",
                expected_columns=(
                    ColumnSpec("delivery_commit_hash", "TEXT"),
                    ColumnSpec("delivery_reason_type", "TEXT"),
                ),
            ),
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
        assertions=(
            SchemaAssertion(
                table="captures",
                expected_columns=(
                    ColumnSpec("retry_attempts", "INTEGER", not_null=True),
                ),
            ),
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
        assertions=(
            SchemaAssertion(
                table="captures",
                expected_columns=(
                    ColumnSpec("clarification_status", "TEXT"),
                    ColumnSpec("clarification_question", "TEXT"),
                ),
            ),
            SchemaAssertion(
                table="corrections",
                expected_columns=(
                    ColumnSpec("correction_id", "TEXT", not_null=True),
                    ColumnSpec("capture_id", "TEXT", not_null=True),
                    ColumnSpec("old_note_path", "TEXT", not_null=True),
                    ColumnSpec("new_note_path", "TEXT", not_null=True),
                ),
                expected_foreign_keys=(
                    ForeignKeySpec("capture_id", "captures", "capture_id"),
                ),
            ),
        ),
    ),
    Migration(
        version=6,
        name="vault_update_proposals",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS vault_update_proposals (
                proposal_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                operation TEXT NOT NULL,
                target_note_path TEXT NOT NULL,
                target_anchor_json TEXT,
                change_json TEXT NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                requires_approval INTEGER NOT NULL DEFAULT 1,
                submitted_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by TEXT,
                applied_at TEXT,
                rejected_reason TEXT,
                git_commit_hash TEXT,
                last_error TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_vault_update_proposals_status
                ON vault_update_proposals(status, submitted_at)
            """,
        ),
        assertions=(
            SchemaAssertion(
                table="vault_update_proposals",
                expected_columns=(
                    ColumnSpec("proposal_id", "TEXT"),  # PRIMARY KEY; SQLite notnull=0 for non-INTEGER PKs
                    ColumnSpec("source", "TEXT", not_null=True),
                    ColumnSpec("operation", "TEXT", not_null=True),
                    ColumnSpec("status", "TEXT", not_null=True),
                    ColumnSpec("submitted_at", "TEXT", not_null=True),
                ),
            ),
        ),
    ),
    Migration(
        version=7,
        name="vault_update_proposals_approval_message",
        statements=(
            "ALTER TABLE vault_update_proposals ADD COLUMN approval_message_id TEXT",
        ),
        assertions=(
            SchemaAssertion(
                table="vault_update_proposals",
                expected_columns=(
                    ColumnSpec("approval_message_id", "TEXT"),
                ),
            ),
        ),
    ),
    Migration(
        version=8,
        name="receipt_sync_tracking",
        statements=(
            "ALTER TABLE captures ADD COLUMN receipt_sync_status TEXT NOT NULL DEFAULT 'clean'",
            "ALTER TABLE captures ADD COLUMN receipt_sync_last_attempt_at TEXT",
            "ALTER TABLE captures ADD COLUMN receipt_sync_last_error_type TEXT",
        ),
        assertions=(
            SchemaAssertion(
                table="captures",
                expected_columns=(
                    ColumnSpec("receipt_sync_status", "TEXT", not_null=True),
                    ColumnSpec("receipt_sync_last_attempt_at", "TEXT"),
                    ColumnSpec("receipt_sync_last_error_type", "TEXT"),
                ),
            ),
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


import re as _re

_CREATE_TABLE_IF_NOT_EXISTS_RE = _re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", _re.IGNORECASE
)


def _tables_created_by_migration(migration: Migration) -> set[str]:
    """Return table names this migration would create with CREATE TABLE IF NOT EXISTS."""
    tables: set[str] = set()
    for stmt in migration.statements:
        for m in _CREATE_TABLE_IF_NOT_EXISTS_RE.finditer(stmt):
            tables.add(m.group(1).lower())
    return tables


def _existing_table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0].lower() for row in rows}


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

        # SB-140: if *any* table this migration would CREATE IF NOT EXISTS already
        # exists, treat the entire migration as a legacy-DB adoption and validate
        # ALL of its asserted tables before executing any statements.  This
        # prevents IF NOT EXISTS clauses from silently creating or repairing
        # missing tables/indexes on a legacy DB whose schema doesn't match.
        # Fresh empty databases have none of these tables yet, so they fall
        # through to the normal CREATE path.
        tables_this_migration_creates = _tables_created_by_migration(migration)
        if tables_this_migration_creates & _existing_table_names(connection):
            for assertion in migration.assertions:
                assertion.verify(connection)

        for statement in migration.statements:
            connection.execute(statement)

        for assertion in migration.assertions:
            assertion.verify(connection)

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
