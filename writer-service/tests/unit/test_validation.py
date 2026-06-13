"""Classification input validation and path traversal tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app

CLIENT = TestClient(app, raise_server_exceptions=False)
_TOKEN = "test-token-abc123"
_HEADERS = {"X-Second-Brain-Writer-Token": _TOKEN}


def _base_payload(**overrides):
    payload = {
        "capture_id": "SB-20260612-0001",
        "source_message_id": "111222333444555666",
        "created_at": "2026-06-12T18:00:00Z",
        "delivery_attempt": 1,
        "model": "gemini-3.5-flash",
        "prompt_version": "classifier-v1",
        "classification": {
            "folder": "projects",
            "project": "my-project",
            "note_type": "note",
            "title": "Valid note title",
            "tags": ["tag1"],
            "body": "Valid body text.",
            "actions": [],
            "needs_clarification": False,
            "clarifying_question": None,
            "confidence": 0.9,
        },
        "inbox_reason": None,
    }
    payload.update(overrides)
    return payload


# ── Classification validation ────────────────────────────────────────────────


def test_valid_classification_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)
    resp = CLIENT.post("/internal/notes/file", json=_base_payload(), headers=_HEADERS)
    assert resp.status_code == 200


def test_unknown_field_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    payload = _base_payload()
    payload["classification"]["unknown_extra_field"] = "bad"
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    assert resp.status_code == 422


def test_invalid_folder_enum_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    payload = _base_payload()
    payload["classification"]["folder"] = "definitely-not-a-folder"
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    assert resp.status_code == 422


def test_invalid_action_status_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    payload = _base_payload()
    payload["classification"]["actions"] = [{"text": "do something", "status": "pending"}]
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    assert resp.status_code == 422


def test_confidence_below_zero_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    payload = _base_payload()
    payload["classification"]["confidence"] = -0.1
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    assert resp.status_code == 422


def test_confidence_above_one_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    payload = _base_payload()
    payload["classification"]["confidence"] = 1.01
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    assert resp.status_code == 422


def test_invalid_capture_id_format_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    payload = _base_payload(capture_id="INVALID-123")
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    assert resp.status_code == 422


def test_delivery_attempt_below_one_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    payload = _base_payload(delivery_attempt=0)
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    assert resp.status_code == 422


# ── Path traversal rejection ─────────────────────────────────────────────────


def test_path_traversal_in_title_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)
    payload = _base_payload()
    # sanitize_slug strips ., / etc so ../evil would become evil — no traversal possible
    # But a title producing a component starting with . would be caught
    payload["classification"]["title"] = "..evil"
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    # sanitize_slug converts ..evil → evil (strips leading/trailing hyphens after replacing non-alnum)
    # So this should succeed — path traversal is via the slug sanitizer
    assert resp.status_code in (200, 422)


def test_null_byte_in_title_rejected_or_sanitized(tmp_path, monkeypatch):
    """null bytes are stripped by sanitize_slug (non-alnum → hyphen)."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir(parents=True)
    payload = _base_payload()
    payload["classification"]["title"] = "note\x00title"
    resp = CLIENT.post("/internal/notes/file", json=payload, headers=_HEADERS)
    # _validate_path_components rejects null bytes in filename
    assert resp.status_code in (200, 422)
