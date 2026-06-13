"""Token authentication tests for writer-service."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app

CLIENT = TestClient(app, raise_server_exceptions=False)

_VALID_PAYLOAD = {
    "capture_id": "SB-20260612-0001",
    "source_message_id": "111222333444555666",
    "created_at": "2026-06-12T18:00:00Z",
    "delivery_attempt": 1,
    "model": "gemini-3.5-flash",
    "prompt_version": "classifier-v1",
    "classification": {
        "folder": "projects",
        "project": "test-project",
        "note_type": "note",
        "title": "Auth test note",
        "tags": ["test"],
        "body": "Test body.",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.9,
    },
    "inbox_reason": None,
}


def test_missing_token_returns_401(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    resp = CLIENT.post("/internal/notes/file", json=_VALID_PAYLOAD)
    assert resp.status_code == 401


def test_wrong_token_returns_401(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    resp = CLIENT.post(
        "/internal/notes/file",
        json=_VALID_PAYLOAD,
        headers={"X-Second-Brain-Writer-Token": "wrong-token"},
    )
    assert resp.status_code == 401


def test_correct_token_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)
    resp = CLIENT.post(
        "/internal/notes/file",
        json=_VALID_PAYLOAD,
        headers={"X-Second-Brain-Writer-Token": "test-token-abc123"},
    )
    assert resp.status_code == 200


def test_health_requires_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)
    resp = CLIENT.get("/health")
    assert resp.status_code == 200
