"""Integration tests for writer-service filing with a temporary vault."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app

CLIENT = TestClient(app, raise_server_exceptions=False)
_TOKEN = "test-token-abc123"
_HEADERS = {"X-Second-Brain-Writer-Token": _TOKEN}


def _filing_payload(
    capture_id: str = "SB-20260612-0001",
    folder: str = "projects",
    inbox_reason: str | None = None,
):
    return {
        "capture_id": capture_id,
        "source_message_id": "123456789012345678",
        "created_at": "2026-06-12T18:00:00Z",
        "delivery_attempt": 1,
        "model": "gemini-3.5-flash",
        "prompt_version": "classifier-v1",
        "classification": {
            "folder": folder,
            "project": "second-brain" if folder == "projects" else None,
            "note_type": "implementation-note",
            "title": "Integration test note",
            "tags": ["integration", "test"],
            "body": "Integration test body.",
            "actions": [],
            "needs_clarification": False,
            "clarifying_question": None,
            "confidence": 0.92,
        },
        "inbox_reason": inbox_reason,
    }


# ── Normal filing ─────────────────────────────────────────────────────────────


def test_valid_filing_creates_note_at_expected_path(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post("/internal/notes/file", json=_filing_payload(), headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "FILED"
    assert "note_path" in body
    assert "SB-20260612-0001" in body["note_path"]

    note_file = vault / body["note_path"]
    assert note_file.exists()


def test_frontmatter_capture_id_matches_request(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post("/internal/notes/file", json=_filing_payload("SB-20260612-0042"), headers=_HEADERS)
    note_file = vault / resp.json()["note_path"]
    content = note_file.read_text()
    assert 'capture_id: "SB-20260612-0042"' in content


def test_audit_event_appended_on_filing(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_filing_payload(), headers=_HEADERS)

    audit = vault / "99_log" / "events.ndjson"
    assert audit.exists()
    event = json.loads(audit.read_text().strip())
    assert event["event"] == "NOTE_FILED"


# ── Idempotent replay ─────────────────────────────────────────────────────────


def test_second_request_with_same_capture_id_returns_idempotent_true(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_filing_payload(), headers=_HEADERS)
    resp = CLIENT.post("/internal/notes/file", json=_filing_payload(), headers=_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["idempotent"] is True


def test_note_content_unchanged_on_idempotent_replay(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp1 = CLIENT.post("/internal/notes/file", json=_filing_payload(), headers=_HEADERS)
    note_path = vault / resp1.json()["note_path"]
    content_before = note_path.read_text()

    CLIENT.post("/internal/notes/file", json=_filing_payload(), headers=_HEADERS)
    assert note_path.read_text() == content_before


def test_no_duplicate_audit_event_on_replay(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_filing_payload(), headers=_HEADERS)
    CLIENT.post("/internal/notes/file", json=_filing_payload(), headers=_HEADERS)

    audit = vault / "99_log" / "events.ndjson"
    lines = audit.read_text().strip().splitlines()
    assert len(lines) == 1


# ── Inbox routing ─────────────────────────────────────────────────────────────


def test_inbox_reason_nonnull_routes_to_00_inbox(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post(
        "/internal/notes/file",
        json=_filing_payload(folder="projects", inbox_reason="low_confidence"),
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["note_path"].startswith("00_inbox/")


def test_inbox_receipt_path_starts_with_00_inbox(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post(
        "/internal/notes/file",
        json=_filing_payload(folder="people", inbox_reason="classifier_selected_inbox"),
        headers=_HEADERS,
    )
    assert resp.json()["note_path"].startswith("00_inbox/")
