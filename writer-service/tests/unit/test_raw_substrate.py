"""Unit tests for the raw vault substrate (SB-141 / TD-03)."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from writerservice.api_models import AttachmentMetadata, Classification
from writerservice.writer import (
    RawHashMismatchError,
    VaultWriter,
    build_raw_body,
    compute_raw_sha256,
    parse_raw_file,
    raw_capture_path,
    render_raw_markdown,
    write_or_verify_raw_capture,
)

_CREATED_AT = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
_CAPTURE_ID = "SB-20260621-0003"


def _make_classification(**overrides):
    data = {
        "folder": "projects",
        "project": "second-brain",
        "note_type": "note",
        "title": "Test note",
        "tags": ["test"],
        "body": "Body content.",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.9,
    }
    data.update(overrides)
    return Classification.model_validate(data)


# ── build_raw_body ─────────────────────────────────────────────────────────────


def test_build_raw_body_text_only():
    result = build_raw_body("hello world", [])
    assert result == "hello world"


def test_build_raw_body_preserves_whitespace_and_newlines():
    text = "  line one\n\ttab line\ncode\n  trailing  "
    result = build_raw_body(text, [])
    assert result == text


def test_build_raw_body_text_plus_attachments():
    attachments = [AttachmentMetadata(filename="photo.png", content_type="image/png")]
    result = build_raw_body("some text", attachments)
    assert result.startswith("some text\n\n## Attachments\n\n")
    assert '- filename: "photo.png", content_type: "image/png"' in result


def test_build_raw_body_attachment_only():
    attachments = [AttachmentMetadata(filename="archive.zip", content_type=None)]
    result = build_raw_body("", attachments)
    assert result.startswith("## Attachments\n\n")
    assert '- filename: "archive.zip", content_type: null' in result
    assert "## Attachments" in result


def test_build_raw_body_multiple_attachments():
    attachments = [
        AttachmentMetadata(filename="a.png", content_type="image/png"),
        AttachmentMetadata(filename="b.pdf", content_type="application/pdf"),
    ]
    result = build_raw_body("caption", attachments)
    assert '- filename: "a.png"' in result
    assert '- filename: "b.pdf"' in result


def test_build_raw_body_empty_text_no_attachments():
    result = build_raw_body("", [])
    assert result == ""


# ── raw_capture_path ──────────────────────────────────────────────────────────


def test_raw_capture_path_format():
    path = raw_capture_path("SB-20260621-0003", _CREATED_AT)
    assert path == "00_raw/2026/06/SB-20260621-0003.md"


def test_raw_capture_path_uses_utc_month():
    dt = datetime(2026, 1, 5, 23, 0, 0, tzinfo=UTC)
    path = raw_capture_path("SB-20260105-0001", dt)
    assert path == "00_raw/2026/01/SB-20260105-0001.md"


def test_raw_capture_path_ignores_classification():
    # Same capture_id and created_at must always produce same path regardless of inputs
    path1 = raw_capture_path(_CAPTURE_ID, _CREATED_AT)
    path2 = raw_capture_path(_CAPTURE_ID, _CREATED_AT)
    assert path1 == path2


# ── compute_raw_sha256 ────────────────────────────────────────────────────────


def test_compute_raw_sha256_matches_manual():
    body = "hello"
    expected = hashlib.sha256(b"hello").hexdigest()
    assert compute_raw_sha256(body) == expected


def test_compute_raw_sha256_no_trimming():
    body_with_spaces = "  hello  \n"
    expected = hashlib.sha256(body_with_spaces.encode("utf-8")).hexdigest()
    assert compute_raw_sha256(body_with_spaces) == expected


def test_compute_raw_sha256_empty_string():
    expected = hashlib.sha256(b"").hexdigest()
    assert compute_raw_sha256("") == expected


# ── parse_raw_file ────────────────────────────────────────────────────────────


def test_parse_raw_file_round_trips(tmp_path):
    body = "my raw text\nwith newlines"
    h = compute_raw_sha256(body)
    content = render_raw_markdown(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        raw_body=body,
        raw_hash=h,
    )
    f = tmp_path / "raw.md"
    f.write_text(content, encoding="utf-8")
    parsed_body, parsed_hash = parse_raw_file(f)
    assert parsed_body == body
    assert parsed_hash == h
    assert compute_raw_sha256(parsed_body) == h


# ── write_or_verify_raw_capture ───────────────────────────────────────────────


def test_write_or_verify_creates_new_file(tmp_path):
    raw_abs = tmp_path / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    body = "original text"
    h = compute_raw_sha256(body)
    created = write_or_verify_raw_capture(
        raw_abs=raw_abs,
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        raw_body=body,
        raw_hash=h,
    )
    assert created is True
    assert raw_abs.exists()


def test_write_or_verify_idempotent_same_hash(tmp_path):
    raw_abs = tmp_path / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    body = "original text"
    h = compute_raw_sha256(body)
    write_or_verify_raw_capture(
        raw_abs=raw_abs, capture_id=_CAPTURE_ID, source_message_id="111",
        created_at=_CREATED_AT, raw_body=body, raw_hash=h,
    )
    created = write_or_verify_raw_capture(
        raw_abs=raw_abs, capture_id=_CAPTURE_ID, source_message_id="111",
        created_at=_CREATED_AT, raw_body=body, raw_hash=h,
    )
    assert created is False


def test_write_or_verify_raises_on_hash_mismatch(tmp_path):
    raw_abs = tmp_path / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    body1 = "original text"
    h1 = compute_raw_sha256(body1)
    write_or_verify_raw_capture(
        raw_abs=raw_abs, capture_id=_CAPTURE_ID, source_message_id="111",
        created_at=_CREATED_AT, raw_body=body1, raw_hash=h1,
    )
    body2 = "different text"
    h2 = compute_raw_sha256(body2)
    with pytest.raises(RawHashMismatchError):
        write_or_verify_raw_capture(
            raw_abs=raw_abs, capture_id=_CAPTURE_ID, source_message_id="111",
            created_at=_CREATED_AT, raw_body=body2, raw_hash=h2,
        )


def test_write_or_verify_does_not_overwrite_on_mismatch(tmp_path):
    raw_abs = tmp_path / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    body1 = "original text"
    h1 = compute_raw_sha256(body1)
    write_or_verify_raw_capture(
        raw_abs=raw_abs, capture_id=_CAPTURE_ID, source_message_id="111",
        created_at=_CREATED_AT, raw_body=body1, raw_hash=h1,
    )
    original_content = raw_abs.read_text(encoding="utf-8")
    body2 = "different text"
    h2 = compute_raw_sha256(body2)
    with pytest.raises(RawHashMismatchError):
        write_or_verify_raw_capture(
            raw_abs=raw_abs, capture_id=_CAPTURE_ID, source_message_id="111",
            created_at=_CREATED_AT, raw_body=body2, raw_hash=h2,
        )
    assert raw_abs.read_text(encoding="utf-8") == original_content


# ── VaultWriter raw substrate integration ────────────────────────────────────


def test_raw_file_created_on_write_note(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="original text",
    )
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    assert raw_abs.exists()


def test_raw_file_path_is_deterministic(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    result = writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="original text",
    )
    assert result.raw_capture_path == f"00_raw/2026/06/{_CAPTURE_ID}.md"


def test_raw_file_body_matches_raw_text(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    text = "exact original\nwith newlines\n  and spaces  "
    writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text=text,
    )
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    parsed_body, _ = parse_raw_file(raw_abs)
    assert parsed_body == text


def test_raw_sha256_in_raw_frontmatter_matches_body(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    text = "some raw content"
    writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text=text,
    )
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    body, stored_hash = parse_raw_file(raw_abs)
    assert stored_hash == compute_raw_sha256(body)


def test_sanitized_note_frontmatter_includes_raw_linkage(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    result = writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="raw text",
    )
    content = result.absolute_path.read_text(encoding="utf-8")
    assert "raw_capture_path:" in content
    assert "raw_sha256:" in content
    assert "derived_from_capture_id:" in content
    assert _CAPTURE_ID in content


def test_sanitized_note_raw_sha256_matches_raw_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    result = writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="some text",
    )
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    _, raw_hash = parse_raw_file(raw_abs)
    note_content = result.absolute_path.read_text(encoding="utf-8")
    assert raw_hash in note_content


def test_write_result_includes_raw_capture_path_and_sha256(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    result = writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="text",
    )
    assert result.raw_capture_path == f"00_raw/2026/06/{_CAPTURE_ID}.md"
    assert len(result.raw_sha256) == 64  # SHA-256 hex digest


def test_idempotent_replay_does_not_create_second_raw_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    kwargs = dict(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="original text",
    )
    writer.write_note(**kwargs)
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    content_before = raw_abs.read_text(encoding="utf-8")

    writer.write_note(**{**kwargs, "delivery_attempt": 2})
    assert raw_abs.read_text(encoding="utf-8") == content_before


def test_raw_hash_mismatch_raises_and_does_not_write_sanitized_note(tmp_path):
    # Simulate: raw file written on first attempt, but sanitized note was never written
    # (e.g. sanitized note write failed on first attempt). Retry comes in with different text.
    vault = tmp_path / "vault"
    vault.mkdir()

    # Write the raw file manually with "original text"
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    body = "original text"
    h = compute_raw_sha256(body)
    created = write_or_verify_raw_capture(
        raw_abs=raw_abs,
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        raw_body=body,
        raw_hash=h,
    )
    assert created is True

    # Now try to file with different raw_text — no sanitized note exists yet
    writer = VaultWriter(vault)
    with pytest.raises(RawHashMismatchError):
        writer.write_note(
            capture_id=_CAPTURE_ID,
            source_message_id="111",
            created_at=_CREATED_AT,
            classification=_make_classification(),
            model="gemini-flash",
            prompt_version="v1",
            delivery_attempt=2,
            inbox_reason=None,
            raw_text="DIFFERENT text",
        )

    # No sanitized note should have been written
    sanitized = [p for p in vault.rglob("*.md") if "00_raw" not in str(p)]
    assert len(sanitized) == 0


def test_text_plus_attachments_raw_body_has_attachments_section(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    attachments = [AttachmentMetadata(filename="photo.png", content_type="image/png")]
    writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="caption text",
        attachments=attachments,
    )
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    content = raw_abs.read_text(encoding="utf-8")
    assert "## Attachments" in content
    assert "photo.png" in content


def test_attachment_only_raw_file_has_no_binary_content(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    attachments = [AttachmentMetadata(filename="doc.pdf", content_type="application/pdf")]
    writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="",
        attachments=attachments,
    )
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    assert raw_abs.exists()
    content = raw_abs.read_text(encoding="utf-8")
    assert "## Attachments" in content
    assert "doc.pdf" in content


def test_sensitive_content_written_verbatim_to_raw(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = VaultWriter(vault)
    sensitive_text = "password: s3cr3t\ntoken: abc123"
    writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text=sensitive_text,
    )
    raw_abs = vault / "00_raw" / "2026" / "06" / f"{_CAPTURE_ID}.md"
    body, _ = parse_raw_file(raw_abs)
    assert body == sensitive_text


def test_raw_file_written_before_sanitized_note(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()

    write_order: list[str] = []

    # The raw file is created atomically via os.rename; track that.
    import os as _os
    original_rename = _os.rename

    def tracked_rename(src, dst, **kwargs):
        dst_str = str(dst)
        if "00_raw" in dst_str:
            write_order.append("raw")
        return original_rename(src, dst, **kwargs)

    original_write_text = Path.write_text

    def tracked_write_text(self, *args, **kwargs):
        rel = str(self.relative_to(vault)) if str(self).startswith(str(vault)) else str(self)
        if rel.endswith(".md") and "99_log" not in rel and "00_raw" not in rel:
            write_order.append("sanitized")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(_os, "rename", tracked_rename)
    monkeypatch.setattr(Path, "write_text", tracked_write_text)

    writer = VaultWriter(vault)
    writer.write_note(
        capture_id=_CAPTURE_ID,
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-flash",
        prompt_version="v1",
        delivery_attempt=1,
        inbox_reason=None,
        raw_text="some text",
    )
    assert "raw" in write_order
    assert "sanitized" in write_order
    assert write_order.index("raw") < write_order.index("sanitized")
