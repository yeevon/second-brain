from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class OperationalStatusUnavailable(Exception):
    def __init__(self, safe_reason: str) -> None:
        super().__init__(safe_reason)
        self.safe_reason = safe_reason


@dataclass(frozen=True)
class StatusSettings:
    ledger_path: Path
    vault_path: Path | None
    status_timezone: str
    capture_service_health_stale_after_seconds: int

    @classmethod
    def from_env(cls) -> "StatusSettings":
        from dotenv import load_dotenv
        load_dotenv()

        ledger_path_str = os.getenv("LEDGER_PATH", "").strip()
        if not ledger_path_str:
            raise RuntimeError("LEDGER_PATH is required")

        vault_path_str = os.getenv("VAULT_PATH", "").strip() or None
        timezone_str = os.getenv("STATUS_TIMEZONE", "UTC").strip()
        stale_raw = os.getenv("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

        try:
            stale_after = int(stale_raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS must be an integer, got: {stale_raw!r}"
            ) from exc

        if stale_after < 1:
            raise RuntimeError("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS must be >= 1")

        try:
            ZoneInfo(timezone_str)
        except (ZoneInfoNotFoundError, KeyError) as exc:
            raise RuntimeError(
                f"STATUS_TIMEZONE is not a valid IANA timezone: {timezone_str!r}"
            ) from exc

        return cls(
            ledger_path=Path(ledger_path_str),
            vault_path=Path(vault_path_str) if vault_path_str else None,
            status_timezone=timezone_str,
            capture_service_health_stale_after_seconds=stale_after,
        )


@dataclass(frozen=True)
class OperationalStatusSnapshot:
    generated_at: datetime
    timezone_name: str

    ledger_path: Path
    vault_path: Path | None

    total_captures: int
    captures_received_today: int
    captures_filed_today: int
    captures_in_inbox: int
    captures_rejected_sensitive: int
    captures_failed: int
    captures_waiting_for_retry: int
    stale_leases: int

    last_reconciled_discord_message_id: str | None
    last_successful_reconciliation_at: datetime | None
    last_successful_reconciliation_mode: str | None
    last_successful_vault_write: str | None

    capture_service_health: str
    capture_service_state: str | None
    capture_service_instance_id: str | None
    capture_service_started_at: datetime | None
    capture_service_last_heartbeat_at: datetime | None
    capture_service_stopped_at: datetime | None

    captures_needing_clarification: int
    last_successful_backup_at: datetime | None
    last_successful_restore_validation_at: datetime | None
    receipt_repairs_today: int
    last_vault_write_at: datetime | None
    reaper_last_heartbeat_at: datetime | None
    reconcile_last_heartbeat_at: datetime | None
    delivery_last_heartbeat_at: datetime | None
    classifier_last_heartbeat_at: datetime | None
    background_task_stale: bool

    # SB-137: per-task status and last safe error type
    reaper_task_status: str | None
    reconcile_task_status: str | None
    delivery_task_status: str | None
    classifier_task_status: str | None
    reaper_last_error_type: str | None
    reconcile_last_error_type: str | None
    delivery_last_error_type: str | None
    classifier_last_error_type: str | None


def calculate_capture_service_health(
    *,
    service_state: str | None,
    last_heartbeat_at: datetime | None,
    now: datetime,
    stale_after_seconds: int,
) -> str:
    if service_state is None:
        return "UNKNOWN"
    if service_state == "STOPPED":
        return "STOPPED"
    if service_state in ("RUNNING", "STARTING"):
        if last_heartbeat_at is None:
            return "STALE"
        age = (now - last_heartbeat_at).total_seconds()
        if age > stale_after_seconds:
            return "STALE"
        return "HEALTHY" if service_state == "RUNNING" else "STARTING"
    return "UNKNOWN"


def read_operational_status(
    *,
    settings: StatusSettings,
    now: datetime | None = None,
) -> OperationalStatusSnapshot:
    conn = None
    try:
        if not settings.ledger_path.exists():
            raise OperationalStatusUnavailable("database file does not exist")

        conn = sqlite3.connect(
            f"file:{settings.ledger_path}?mode=ro",
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 1000")

        return _query_snapshot(conn, settings=settings, now=now or datetime.now(UTC))

    except OperationalStatusUnavailable:
        raise
    except sqlite3.DatabaseError as exc:
        raise OperationalStatusUnavailable(
            f"database query failed: {type(exc).__name__}"
        ) from exc
    except (ValueError, TypeError) as exc:
        raise OperationalStatusUnavailable(
            f"stored status metadata is invalid: {type(exc).__name__}"
        ) from exc
    finally:
        if conn is not None:
            conn.close()


def _query_snapshot(
    conn: sqlite3.Connection,
    *,
    settings: StatusSettings,
    now: datetime,
) -> OperationalStatusSnapshot:
    tz = ZoneInfo(settings.status_timezone)
    local_now = now.astimezone(tz)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    today_utc_iso = today_start.astimezone(UTC).isoformat()
    tomorrow_utc_iso = tomorrow_start.astimezone(UTC).isoformat()
    now_iso = now.isoformat()

    def get_state(key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return row["value"] or None

    def parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value)

    total_row = conn.execute("SELECT COUNT(*) AS c FROM captures").fetchone()
    total_captures = int(total_row["c"])

    received_today_row = conn.execute(
        "SELECT COUNT(*) AS c FROM captures WHERE received_at >= ? AND received_at < ?",
        (today_utc_iso, tomorrow_utc_iso),
    ).fetchone()
    captures_received_today = int(received_today_row["c"])

    filed_today_row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM capture_events ce
        JOIN captures c ON c.capture_id = ce.capture_id
        WHERE ce.event_type = 'CAPTURE_FILED'
          AND ce.created_at >= ? AND ce.created_at < ?
          AND (c.derived_note_path IS NULL OR c.derived_note_path NOT LIKE 'stub://%')
        """,
        (today_utc_iso, tomorrow_utc_iso),
    ).fetchone()
    captures_filed_today = int(filed_today_row["c"])

    inbox_row = conn.execute(
        "SELECT COUNT(*) AS c FROM captures WHERE status = 'INBOX'"
    ).fetchone()
    captures_in_inbox = int(inbox_row["c"])

    rejected_row = conn.execute(
        "SELECT COUNT(*) AS c FROM captures WHERE status = 'REJECTED_SENSITIVE'"
    ).fetchone()
    captures_rejected_sensitive = int(rejected_row["c"])

    failed_row = conn.execute(
        "SELECT COUNT(*) AS c FROM captures WHERE status = 'FAILED'"
    ).fetchone()
    captures_failed = int(failed_row["c"])

    retry_row = conn.execute(
        "SELECT COUNT(*) AS c FROM captures WHERE delivery_status = 'RETRY_WAIT'"
    ).fetchone()
    captures_waiting_for_retry = int(retry_row["c"])

    stale_row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM captures
        WHERE delivery_status IN ('FORWARDING', 'FORWARDED', 'CLASSIFYING')
          AND processing_lease_until IS NOT NULL
          AND processing_lease_until <= ?
        """,
        (now_iso,),
    ).fetchone()
    stale_leases = int(stale_row["c"])

    vault_row = conn.execute(
        """
        SELECT derived_note_path FROM captures
        WHERE status IN ('FILED', 'INBOX')
          AND derived_note_path IS NOT NULL
          AND derived_note_path NOT LIKE 'stub://%'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    last_successful_vault_write = vault_row["derived_note_path"] if vault_row else None
    last_vault_write_at = parse_dt(get_state("last_vault_write_at"))

    last_reconciled_discord_message_id = get_state("last_reconciled_discord_message_id")
    last_successful_reconciliation_at = parse_dt(get_state("last_successful_reconciliation_at"))
    last_successful_reconciliation_mode = get_state("last_successful_reconciliation_mode")

    service_state = get_state("capture_service_state")
    service_instance_id = get_state("capture_service_instance_id")
    service_started_at = parse_dt(get_state("capture_service_started_at"))
    service_heartbeat_at = parse_dt(get_state("capture_service_last_heartbeat_at"))
    service_stopped_at = parse_dt(get_state("capture_service_stopped_at"))

    health = calculate_capture_service_health(
        service_state=service_state,
        last_heartbeat_at=service_heartbeat_at,
        now=now,
        stale_after_seconds=settings.capture_service_health_stale_after_seconds,
    )

    # SB-117: captures needing clarification
    try:
        clarification_row = conn.execute(
            "SELECT COUNT(*) AS c FROM captures WHERE clarification_status = 'NEEDS_CLARIFICATION'"
        ).fetchone()
        captures_needing_clarification = int(clarification_row["c"])
    except Exception:
        captures_needing_clarification = 0

    # SB-119: backup timestamps
    last_successful_backup_at = parse_dt(get_state("last_successful_backup_at"))
    last_successful_restore_validation_at = parse_dt(get_state("last_successful_restore_validation_at"))

    # SB-134 TD-009: receipt repair count for today
    try:
        receipt_repairs_row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM capture_events
            WHERE event_type = 'RECEIPT_REPLACED'
              AND created_at >= ? AND created_at < ?
            """,
            (today_utc_iso, tomorrow_utc_iso),
        ).fetchone()
        receipt_repairs_today = int(receipt_repairs_row["c"])
    except Exception:
        receipt_repairs_today = 0

    return OperationalStatusSnapshot(
        generated_at=now,
        timezone_name=settings.status_timezone,
        ledger_path=settings.ledger_path,
        vault_path=settings.vault_path,
        total_captures=total_captures,
        captures_received_today=captures_received_today,
        captures_filed_today=captures_filed_today,
        captures_in_inbox=captures_in_inbox,
        captures_rejected_sensitive=captures_rejected_sensitive,
        captures_failed=captures_failed,
        captures_waiting_for_retry=captures_waiting_for_retry,
        stale_leases=stale_leases,
        last_reconciled_discord_message_id=last_reconciled_discord_message_id,
        last_successful_reconciliation_at=last_successful_reconciliation_at,
        last_successful_reconciliation_mode=last_successful_reconciliation_mode,
        last_successful_vault_write=last_successful_vault_write,
        capture_service_health=health,
        capture_service_state=service_state,
        capture_service_instance_id=service_instance_id,
        capture_service_started_at=service_started_at,
        capture_service_last_heartbeat_at=service_heartbeat_at,
        capture_service_stopped_at=service_stopped_at,
        captures_needing_clarification=captures_needing_clarification,
        last_successful_backup_at=last_successful_backup_at,
        last_successful_restore_validation_at=last_successful_restore_validation_at,
        receipt_repairs_today=receipt_repairs_today,
        last_vault_write_at=last_vault_write_at,
        reaper_last_heartbeat_at=parse_dt(get_state("reaper_last_heartbeat_at")),
        reconcile_last_heartbeat_at=parse_dt(get_state("reconcile_last_heartbeat_at")),
        delivery_last_heartbeat_at=parse_dt(get_state("delivery_last_heartbeat_at")),
        classifier_last_heartbeat_at=parse_dt(get_state("classifier_last_heartbeat_at")),
        background_task_stale=get_state("background_task_stale") == "true",
        reaper_task_status=get_state("reaper_task_status"),
        reconcile_task_status=get_state("reconcile_task_status"),
        delivery_task_status=get_state("delivery_task_status"),
        classifier_task_status=get_state("classifier_task_status"),
        reaper_last_error_type=get_state("reaper_last_error_type"),
        reconcile_last_error_type=get_state("reconcile_last_error_type"),
        delivery_last_error_type=get_state("delivery_last_error_type"),
        classifier_last_error_type=get_state("classifier_last_error_type"),
    )


def _fmt(value: datetime | str | Path | None) -> str:
    if value is None:
        return "none"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def format_operational_status(snapshot: OperationalStatusSnapshot) -> str:
    lines = [
        "Second Brain operational status",
        f"generated at: {_fmt(snapshot.generated_at)}",
        f"status timezone: {snapshot.timezone_name}",
        "",
        "Capture intake",
        f"  ledger path: {snapshot.ledger_path}",
        f"  vault path: {_fmt(snapshot.vault_path)}",
        f"  total captures: {snapshot.total_captures}",
        f"  captures received today: {snapshot.captures_received_today}",
        f"  captures rejected as sensitive: {snapshot.captures_rejected_sensitive}",
        "",
        "Note lifecycle",
        f"  captures filed today: {snapshot.captures_filed_today}",
        f"  captures in inbox: {snapshot.captures_in_inbox}",
        f"  captures needing clarification: {snapshot.captures_needing_clarification}",
        f"  captures failed: {snapshot.captures_failed}",
        f"  receipt repairs today: {snapshot.receipt_repairs_today}",
        f"  last successful vault write: {_fmt(snapshot.last_successful_vault_write)}",
        f"  last vault write at: {_fmt(snapshot.last_vault_write_at)}",
        "",
        "Delivery backlog",
        f"  captures waiting for retry: {snapshot.captures_waiting_for_retry}",
        f"  stale leases: {snapshot.stale_leases}",
        "",
        "Discord reconciliation",
        f"  last reconciled Discord message ID: {_fmt(snapshot.last_reconciled_discord_message_id)}",
        f"  last successful reconciliation: {_fmt(snapshot.last_successful_reconciliation_at)}",
        f"  last successful reconciliation mode: {_fmt(snapshot.last_successful_reconciliation_mode)}",
        "",
        "Capture service",
        f"  capture-service health: {snapshot.capture_service_health}",
        f"  capture-service state: {_fmt(snapshot.capture_service_state)}",
        f"  capture-service instance ID: {_fmt(snapshot.capture_service_instance_id)}",
        f"  capture-service started at: {_fmt(snapshot.capture_service_started_at)}",
        f"  capture-service last heartbeat: {_fmt(snapshot.capture_service_last_heartbeat_at)}",
        f"  capture-service stopped at: {_fmt(snapshot.capture_service_stopped_at)}",
        "",
        "Background tasks",
        f"  reaper: {_fmt(snapshot.reaper_task_status)} (last heartbeat: {_fmt(snapshot.reaper_last_heartbeat_at)}, last error: {_fmt(snapshot.reaper_last_error_type)})",
        f"  reconcile: {_fmt(snapshot.reconcile_task_status)} (last heartbeat: {_fmt(snapshot.reconcile_last_heartbeat_at)}, last error: {_fmt(snapshot.reconcile_last_error_type)})",
        f"  delivery: {_fmt(snapshot.delivery_task_status)} (last heartbeat: {_fmt(snapshot.delivery_last_heartbeat_at)}, last error: {_fmt(snapshot.delivery_last_error_type)})",
        f"  classifier: {_fmt(snapshot.classifier_task_status)} (last heartbeat: {_fmt(snapshot.classifier_last_heartbeat_at)}, last error: {_fmt(snapshot.classifier_last_error_type)})",
        f"  background task stale: {snapshot.background_task_stale}",
        "",
        "Backup",
        f"  last successful backup: {_fmt(snapshot.last_successful_backup_at)}",
        f"  last successful restore validation: {_fmt(snapshot.last_successful_restore_validation_at)}",
    ]
    return "\n".join(lines)
