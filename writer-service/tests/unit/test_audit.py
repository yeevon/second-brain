"""Audit log tests for writer-service."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app

CLIENT = TestClient(app, raise_server_exceptions=False)
_TOKEN = "test-token-abc123"
_HEADERS = {"X-Second-Brain-Writer-Token": _TOKEN}


def _payload(capture_id: str = "SB-20260612-0001"):
    return {
        "capture_id": capture_id,
        "source_message_id": "111222333444555666",
        "created_at": "2026-06-12T18:00:00Z",
        "delivery_attempt": 1,
        "model": "gemini-3.5-flash",
        "prompt_version": "classifier-v1",
        "classification": {
            "folder": "projects",
            "project": "test-project",
            "note_type": "note",
            "title": "Audit test note",
            "tags": ["test"],
            "body": "Test body content.",
            "actions": [],
            "needs_clarification": False,
            "clarifying_question": None,
            "confidence": 0.9,
        },
        "inbox_reason": None,
    }


def test_note_filed_event_appended_after_write(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)

    resp = CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    assert resp.status_code == 200

    audit_path = tmp_path / "vault" / "99_log" / "events.ndjson"
    assert audit_path.exists()

    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "NOTE_FILED"
    assert event["capture_id"] == "SB-20260612-0001"
    assert "note_path" in event
    assert event["idempotent"] is False


def test_idempotent_replay_does_not_append_second_audit_event(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)

    CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)

    audit_path = tmp_path / "vault" / "99_log" / "events.ndjson"
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_audit_log_does_not_contain_raw_capture_text(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)

    CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)

    audit_path = tmp_path / "vault" / "99_log" / "events.ndjson"
    content = audit_path.read_text()
    assert "Test body content." not in content


def test_audit_log_does_not_contain_writer_token(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)

    CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)

    audit_path = tmp_path / "vault" / "99_log" / "events.ndjson"
    content = audit_path.read_text()
    assert "test-token-abc123" not in content
