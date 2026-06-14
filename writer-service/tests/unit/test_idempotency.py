"""Idempotency tests for writer-service."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from writerservice.api_models import Classification
from writerservice.main import app
from writerservice.writer import VaultWriter

CLIENT = TestClient(app, raise_server_exceptions=False)
_TOKEN = "test-token-abc123"
_HEADERS = {"X-Second-Brain-Writer-Token": _TOKEN}
_CREATED_AT = datetime(2026, 6, 12, 18, 0, 0, tzinfo=UTC)


def _payload(capture_id: str = "SB-20260612-0001", **overrides):
    p = {
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
            "title": "Idempotency test note",
            "tags": ["test"],
            "body": "Test body.",
            "actions": [],
            "needs_clarification": False,
            "clarifying_question": None,
            "confidence": 0.9,
        },
        "inbox_reason": None,
    }
    p.update(overrides)
    return p


def test_same_capture_id_returns_same_path_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)

    resp1 = CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    assert resp1.status_code == 200
    path1 = resp1.json()["note_path"]

    content_before = (tmp_path / "vault" / path1).read_text()

    resp2 = CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    assert resp2.status_code == 200
    path2 = resp2.json()["note_path"]

    assert path1 == path2
    assert (tmp_path / "vault" / path2).read_text() == content_before


def test_idempotent_replay_returns_idempotent_true(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)

    CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    resp = CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["idempotent"] is True


def test_new_file_returns_idempotent_false(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)

    resp = CLIENT.post("/internal/notes/file", json=_payload("SB-20260612-0099"), headers=_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["idempotent"] is False
