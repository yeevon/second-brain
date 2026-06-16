"""Markdown generation tests for writer-service."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from writerservice.api_models import Classification
from writerservice.writer import VaultWriter, render_markdown


_CREATED_AT = datetime(2026, 6, 12, 18, 0, 0, tzinfo=UTC)

GOLDEN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "golden_note.md"


def _make_classification(**overrides):
    data = {
        "folder": "projects",
        "project": "second-brain",
        "note_type": "implementation-note",
        "title": "SB-114 writer service",
        "tags": ["writer", "second-brain"],
        "body": "Normalized note body.",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.92,
    }
    data.update(overrides)
    return Classification.model_validate(data)


def test_golden_fixture_matches_byte_for_byte():
    result = render_markdown(
        capture_id="SB-20260612-0001",
        source_message_id="123456789012345678",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
    )
    expected = GOLDEN_FIXTURE.read_text(encoding="utf-8")
    assert result == expected


def test_filename_embeds_capture_id(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    result = writer.write_note(
        capture_id="SB-20260612-0001",
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
        delivery_attempt=1,
        inbox_reason=None,
    )
    assert "SB-20260612-0001" in result.note_path


def test_filename_is_deterministic(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    result1 = writer.write_note(
        capture_id="SB-20260612-0001",
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
        delivery_attempt=1,
        inbox_reason=None,
    )
    expected_path = (
        f"20_projects/second-brain/2026-06-12--SB-20260612-0001--sb-114-writer-service.md"
    )
    assert result1.note_path == expected_path


def test_frontmatter_contains_all_mvp_fields(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    result = writer.write_note(
        capture_id="SB-20260612-0001",
        source_message_id="123456789012345678",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
        delivery_attempt=1,
        inbox_reason=None,
    )
    content = result.absolute_path.read_text(encoding="utf-8")
    assert 'capture_id: "SB-20260612-0001"' in content
    assert 'source_message_id: "123456789012345678"' in content
    assert "created_at:" in content
    assert 'area: "projects"' in content
    assert 'project: "second-brain"' in content
    assert 'note_type: "implementation-note"' in content
    assert "tags:" in content
    assert "actions:" in content
    assert "lifecycle_status: active" in content
    assert 'model: "gemini-3.5-flash"' in content
    assert 'prompt_version: "classifier-v1"' in content
    assert "schema_version: 1" in content


def test_frontmatter_area_equals_logical_folder_value(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    result = writer.write_note(
        capture_id="SB-20260612-0002",
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(folder="learning", project=None),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
        delivery_attempt=1,
        inbox_reason=None,
    )
    content = result.absolute_path.read_text(encoding="utf-8")
    assert 'area: "learning"' in content


def test_frontmatter_field_order_is_deterministic(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    result = writer.write_note(
        capture_id="SB-20260612-0001",
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
        delivery_attempt=1,
        inbox_reason=None,
    )
    content = result.absolute_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    field_names = [
        ln.split(":")[0].lstrip(" -").strip()
        for ln in lines
        if ln and not ln.startswith("---") and ":" in ln and not ln.startswith(" ")
    ]
    expected_order = [
        "capture_id", "source_message_id", "created_at", "area", "project",
        "note_type", "tags", "actions", "lifecycle_status", "model",
        "prompt_version", "schema_version",
    ]
    # Check all expected fields appear in the correct relative order
    positions = {}
    for i, fn in enumerate(field_names):
        if fn in expected_order:
            positions[fn] = i
    ordered_positions = [positions[f] for f in expected_order if f in positions]
    assert ordered_positions == sorted(ordered_positions)


def test_projects_subfolder_created_from_project_slug(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    result = writer.write_note(
        capture_id="SB-20260612-0001",
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(project="My Project"),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
        delivery_attempt=1,
        inbox_reason=None,
    )
    assert result.note_path.startswith("20_projects/my-project/")


def test_inbox_reason_nonnull_overrides_folder(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    result = writer.write_note(
        capture_id="SB-20260612-0001",
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(folder="projects"),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
        delivery_attempt=1,
        inbox_reason="low_confidence",
    )
    assert result.note_path.startswith("00_inbox/")


def test_body_appended_verbatim(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    result = writer.write_note(
        capture_id="SB-20260612-0001",
        source_message_id="111",
        created_at=_CREATED_AT,
        classification=_make_classification(body="Exact body text here."),
        model="gemini-3.5-flash",
        prompt_version="classifier-v1",
        delivery_attempt=1,
        inbox_reason=None,
    )
    content = result.absolute_path.read_text(encoding="utf-8")
    assert "Exact body text here." in content
