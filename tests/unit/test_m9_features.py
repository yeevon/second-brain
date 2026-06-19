"""Tests for Milestone 9 production deployment features (SB-126 through SB-129)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from secondbrain.ledger import Ledger
from secondbrain.status import StatusSettings, format_operational_status, read_operational_status


# ---------------------------------------------------------------------------
# SB-128: backup timestamps in status
# ---------------------------------------------------------------------------


def test_status_reports_last_successful_backup_at(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    now_iso = datetime.now(UTC).isoformat()
    ledger.set_system_state("last_successful_backup_at", now_iso)
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    assert snapshot.last_successful_backup_at is not None
    assert isinstance(snapshot.last_successful_backup_at, datetime)


def test_status_reports_last_successful_restore_validation_at(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    now_iso = datetime.now(UTC).isoformat()
    ledger.set_system_state("last_successful_restore_validation_at", now_iso)
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    assert snapshot.last_successful_restore_validation_at is not None
    assert isinstance(snapshot.last_successful_restore_validation_at, datetime)


def test_status_backup_timestamps_are_none_when_not_set(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    assert snapshot.last_successful_backup_at is None
    assert snapshot.last_successful_restore_validation_at is None


def test_status_format_includes_backup_section(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    now_iso = datetime.now(UTC).isoformat()
    ledger.set_system_state("last_successful_backup_at", now_iso)
    ledger.set_system_state("last_successful_restore_validation_at", now_iso)
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    output = format_operational_status(snapshot)

    assert "Backup" in output
    assert "last successful backup" in output
    assert "last successful restore validation" in output


def test_status_format_shows_none_for_missing_backup_timestamps(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    output = format_operational_status(snapshot)

    assert "last successful backup: none" in output
    assert "last successful restore validation: none" in output


# ---------------------------------------------------------------------------
# SB-129: OPERATIONS.md exists and covers required sections
# ---------------------------------------------------------------------------


def _ops_doc() -> str:
    path = Path("docs/OPERATIONS.md")
    assert path.exists(), "docs/OPERATIONS.md is missing — required by SB-129"
    return path.read_text()


def test_operations_md_exists():
    assert Path("docs/OPERATIONS.md").exists()


def test_operations_md_covers_service_management():
    doc = _ops_doc()
    assert "docker compose up" in doc
    assert "docker compose down" in doc
    assert "docker compose logs" in doc


def test_operations_md_covers_health_checks():
    doc = _ops_doc()
    assert "127.0.0.1:8000/health" in doc
    assert "127.0.0.1:8001/health" in doc
    assert "State.Health.Status" in doc
    assert "secondbrain status" in doc


def test_operations_md_covers_manual_retry():
    doc = _ops_doc()
    assert "secondbrain retry" in doc
    assert "SB-YYYYMMDD-NNNN" in doc


def test_operations_md_covers_n8n_access():
    doc = _ops_doc()
    assert "5678" in doc
    assert "ssh" in doc.lower() or "SSH" in doc


def test_operations_md_covers_backup_and_restore():
    doc = _ops_doc()
    assert "backup" in doc.lower()
    assert "restore" in doc.lower()
    assert "restore-validate.sh" in doc


def test_operations_md_covers_common_failure_modes():
    doc = _ops_doc()
    assert "Symptom" in doc or "failure" in doc.lower()
    assert "capture-service" in doc
    assert "writer-service" in doc


def test_operations_md_covers_credential_isolation():
    doc = _ops_doc()
    assert "Discord" in doc
    assert "deploy key" in doc.lower() or "DISCORD_BOT_TOKEN" in doc
