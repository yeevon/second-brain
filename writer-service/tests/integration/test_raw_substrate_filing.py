"""Integration tests for raw vault substrate via the /internal/notes/file endpoint."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from writerservice.main import app
from writerservice.writer import compute_raw_sha256, parse_raw_file

CLIENT = TestClient(app, raise_server_exceptions=False)
_TOKEN = "test-token-abc123"
_HEADERS = {"X-Second-Brain-Writer-Token": _TOKEN}


def _payload(
    capture_id: str = "SB-20260621-0003",
    raw_text: str = "original raw text",
    attachments: list | None = None,
    **overrides,
):
    p = {
        "capture_id": capture_id,
        "source_message_id": "111222333444555666",
        "created_at": "2026-06-21T12:00:00Z",
        "delivery_attempt": 1,
        "model": "gemini-flash",
        "prompt_version": "v1",
        "classification": {
            "folder": "projects",
            "project": "second-brain",
            "note_type": "note",
            "title": "Raw substrate test",
            "tags": ["test"],
            "body": "Body content.",
            "actions": [],
            "needs_clarification": False,
            "clarifying_question": None,
            "confidence": 0.9,
        },
        "inbox_reason": None,
        "raw_text": raw_text,
        "attachments": attachments or [],
    }
    p.update(overrides)
    return p


# ── Response shape ─────────────────────────────────────────────────────────────


def test_response_includes_raw_capture_path(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["raw_capture_path"] == "00_raw/2026/06/SB-20260621-0003.md"


def test_response_includes_raw_sha256(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post("/internal/notes/file", json=_payload(raw_text="hello"), headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "raw_sha256" in body
    assert len(body["raw_sha256"]) == 64


def test_response_raw_sha256_matches_raw_text(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    raw_text = "the original message"
    resp = CLIENT.post("/internal/notes/file", json=_payload(raw_text=raw_text), headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    expected_hash = compute_raw_sha256(raw_text)
    assert body["raw_sha256"] == expected_hash


# ── Raw file on disk ──────────────────────────────────────────────────────────


def test_raw_file_exists_after_filing(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)

    raw_file = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    assert raw_file.exists()


def test_raw_file_path_uses_created_at_not_classification(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)

    assert (vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md").exists()


def test_raw_file_body_matches_raw_text_exactly(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    raw_text = "  exact original\n\twith tabs  \n"
    CLIENT.post("/internal/notes/file", json=_payload(raw_text=raw_text), headers=_HEADERS)

    raw_file = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    body, _ = parse_raw_file(raw_file)
    assert body == raw_text


def test_raw_sha256_in_raw_frontmatter_correct(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    raw_text = "some content"
    CLIENT.post("/internal/notes/file", json=_payload(raw_text=raw_text), headers=_HEADERS)

    raw_file = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    body, stored_hash = parse_raw_file(raw_file)
    assert stored_hash == compute_raw_sha256(body)


# ── Sanitized note frontmatter ─────────────────────────────────────────────────


def test_sanitized_note_has_raw_capture_path_frontmatter(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    note_file = vault / resp.json()["note_path"]
    content = note_file.read_text(encoding="utf-8")
    assert "raw_capture_path:" in content
    assert "00_raw/2026/06/SB-20260621-0003.md" in content


def test_sanitized_note_has_raw_sha256_frontmatter(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post("/internal/notes/file", json=_payload(raw_text="hello"), headers=_HEADERS)
    note_file = vault / resp.json()["note_path"]
    content = note_file.read_text(encoding="utf-8")
    expected_hash = compute_raw_sha256("hello")
    assert expected_hash in content


def test_sanitized_note_has_derived_from_capture_id(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)
    note_file = vault / resp.json()["note_path"]
    content = note_file.read_text(encoding="utf-8")
    assert "derived_from_capture_id:" in content
    assert "SB-20260621-0003" in content


def test_sanitized_note_raw_sha256_matches_raw_file(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    resp = CLIENT.post("/internal/notes/file", json=_payload(raw_text="check hash"), headers=_HEADERS)
    raw_file = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    _, raw_hash = parse_raw_file(raw_file)

    note_file = vault / resp.json()["note_path"]
    note_content = note_file.read_text(encoding="utf-8")
    assert raw_hash in note_content


# ── Idempotency ───────────────────────────────────────────────────────────────


def test_idempotent_replay_does_not_create_second_raw_file(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    p = _payload(raw_text="original")
    CLIENT.post("/internal/notes/file", json=p, headers=_HEADERS)
    raw_file = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    content_before = raw_file.read_text(encoding="utf-8")

    p2 = _payload(raw_text="original", delivery_attempt=2)
    resp2 = CLIENT.post("/internal/notes/file", json=p2, headers=_HEADERS)
    assert resp2.status_code == 200
    assert raw_file.read_text(encoding="utf-8") == content_before


def test_hash_mismatch_returns_409(tmp_path, monkeypatch):
    # Simulate: raw file exists (first attempt wrote it) but sanitized note does not.
    # Retry comes in with different raw_text → should fail with 409.
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    from writerservice.writer import compute_raw_sha256, write_or_verify_raw_capture
    from datetime import UTC, datetime

    raw_abs = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    body = "original"
    h = compute_raw_sha256(body)
    write_or_verify_raw_capture(
        raw_abs=raw_abs,
        capture_id="SB-20260621-0003",
        source_message_id="111222333444555666",
        created_at=datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC),
        raw_body=body,
        raw_hash=h,
    )

    resp2 = CLIENT.post(
        "/internal/notes/file",
        json=_payload(raw_text="DIFFERENT content", delivery_attempt=2),
        headers=_HEADERS,
    )
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["error_type"] == "raw_hash_mismatch"


def test_hash_mismatch_does_not_overwrite_raw_file(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_payload(raw_text="original"), headers=_HEADERS)
    raw_file = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    content_before = raw_file.read_text(encoding="utf-8")

    CLIENT.post(
        "/internal/notes/file",
        json=_payload(raw_text="DIFFERENT content", delivery_attempt=2),
        headers=_HEADERS,
    )
    assert raw_file.read_text(encoding="utf-8") == content_before


def test_hash_mismatch_returns_409_when_sanitized_note_already_exists(tmp_path, monkeypatch):
    # Sanitized note AND raw file both exist (first delivery succeeded).
    # A replay arrives with different raw_text — must fail with 409.
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_payload(raw_text="original"), headers=_HEADERS)

    resp2 = CLIENT.post(
        "/internal/notes/file",
        json=_payload(raw_text="DIFFERENT content", delivery_attempt=2),
        headers=_HEADERS,
    )
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["error_type"] == "raw_hash_mismatch"


# ── Audit log raw linkage ─────────────────────────────────────────────────────


def test_audit_event_includes_raw_capture_path(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_payload(), headers=_HEADERS)

    audit = vault / "99_log" / "events.ndjson"
    event = json.loads(audit.read_text().strip())
    assert event["raw_capture_path"] == "00_raw/2026/06/SB-20260621-0003.md"


def test_audit_event_includes_raw_sha256(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    CLIENT.post("/internal/notes/file", json=_payload(raw_text="audit hash check"), headers=_HEADERS)

    audit = vault / "99_log" / "events.ndjson"
    event = json.loads(audit.read_text().strip())
    assert event["raw_sha256"] == compute_raw_sha256("audit hash check")


# ── Attachments ───────────────────────────────────────────────────────────────


def test_text_plus_attachments_raw_file_has_attachments_section(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    attachments = [{"filename": "photo.png", "content_type": "image/png"}]
    CLIENT.post(
        "/internal/notes/file",
        json=_payload(raw_text="caption", attachments=attachments),
        headers=_HEADERS,
    )
    raw_file = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    content = raw_file.read_text(encoding="utf-8")
    assert "## Attachments" in content
    assert "photo.png" in content


def test_attachment_only_creates_raw_metadata_artifact(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_PATH", str(vault))

    attachments = [{"filename": "doc.pdf", "content_type": "application/pdf"}]
    resp = CLIENT.post(
        "/internal/notes/file",
        json=_payload(raw_text="", attachments=attachments),
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    raw_file = vault / "00_raw" / "2026" / "06" / "SB-20260621-0003.md"
    assert raw_file.exists()
    content = raw_file.read_text(encoding="utf-8")
    assert "## Attachments" in content
    assert "doc.pdf" in content
