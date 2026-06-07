from datetime import UTC, datetime
import json

import pytest

from secondbrain.models import Classification
from secondbrain.vault_writer import VaultWriter, render_markdown, sanitize_slug, yaml_scalar


def make_classification(**overrides):
    data = {
        "folder": "projects",
        "project": "halo",
        "note_type": "task",
        "title": "Review WebSocket reconnect handling",
        "tags": ["telemetry", "websocket"],
        "body": "Review reconnect handling in the HALO telemetry dashboard.",
        "actions": [{"text": "Review WebSocket reconnect handling", "status": "open"}],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.91,
    }
    data.update(overrides)
    return Classification.model_validate(data)


def test_write_project_note_creates_markdown_and_audit_event(tmp_path):
    vault = tmp_path / "vault"
    writer = VaultWriter(vault)
    created_at = datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC)

    result = writer.write_note(
        capture_id="SB-20260607-0001",
        source_message_id="1513233540316266517",
        created_at=created_at,
        classification=make_classification(),
        model="gemini-mock",
    )

    assert result.created is True
    assert result.note_path == (
        "20_projects/halo/"
        "2026-06-07--SB-20260607-0001--review-websocket-reconnect-handling.md"
    )
    assert result.absolute_path.exists()

    markdown = result.absolute_path.read_text(encoding="utf-8")
    assert "capture_id: SB-20260607-0001" in markdown
    assert 'source_message_id: "1513233540316266517"' in markdown
    assert "area: projects" in markdown
    assert "project: halo" in markdown
    assert "prompt_version: classifier-v1" in markdown
    assert "# Review WebSocket reconnect handling" in markdown
    assert "- [ ] Review WebSocket reconnect handling" in markdown

    audit_line = (vault / "99_log" / "events.ndjson").read_text(encoding="utf-8").strip()
    audit = json.loads(audit_line)
    assert audit["capture_id"] == "SB-20260607-0001"
    assert audit["event"] == "FILED"
    assert audit["path"] == result.note_path


def test_write_inbox_note_uses_inbox_folder(tmp_path):
    writer = VaultWriter(tmp_path / "vault")

    result = writer.write_note(
        capture_id="SB-20260607-0001",
        source_message_id="1513233540316266517",
        created_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
        classification=make_classification(
            folder="inbox",
            project=None,
            title="Unclassified capture",
            tags=["inbox"],
        ),
        model="gemini-mock",
    )

    assert result.note_path == "00_inbox/2026-06-07--SB-20260607-0001--unclassified-capture.md"


@pytest.mark.parametrize(
    ("folder", "project", "expected_prefix"),
    [
        ("inbox", None, "00_inbox/"),
        ("people", None, "10_people/"),
        ("projects", "HALO Ops", "20_projects/halo-ops/"),
        ("ideas", None, "30_ideas/"),
        ("learning", None, "40_learning/"),
        ("admin", None, "50_admin/"),
    ],
)
def test_write_note_uses_mvp_folder_mapping(tmp_path, folder, project, expected_prefix):
    writer = VaultWriter(tmp_path / "vault")

    result = writer.write_note(
        capture_id="SB-20260607-0001",
        source_message_id="1513233540316266517",
        created_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
        classification=make_classification(folder=folder, project=project),
        model="gemini-mock",
    )

    assert result.note_path.startswith(expected_prefix)


def test_existing_capture_id_returns_existing_note_without_duplicate(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    classification = make_classification()

    first = writer.write_note(
        capture_id="SB-20260607-0001",
        source_message_id="1513233540316266517",
        created_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
        classification=classification,
        model="gemini-mock",
    )
    second = writer.write_note(
        capture_id="SB-20260607-0001",
        source_message_id="1513233540316266517",
        created_at=datetime(2026, 6, 7, 12, 30, 0, tzinfo=UTC),
        classification=make_classification(title="Different retry title"),
        model="gemini-mock",
    )

    assert second.created is False
    assert second.note_path == first.note_path
    notes = list((tmp_path / "vault").rglob("*.md"))
    assert notes == [first.absolute_path]


def test_refuses_to_overwrite_unrelated_note_at_generated_path(tmp_path):
    vault = tmp_path / "vault"
    writer = VaultWriter(vault)
    unrelated_path = (
        vault
        / "20_projects"
        / "halo"
        / "2026-06-07--SB-20260607-0001--review-websocket-reconnect-handling.md"
    )
    unrelated_path.parent.mkdir(parents=True)
    unrelated_path.write_text("capture_id: SB-20260607-DIFFERENT\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite unrelated note"):
        writer.write_note(
            capture_id="SB-20260607-0001",
            source_message_id="1513233540316266517",
            created_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
            classification=make_classification(),
            model="gemini-mock",
        )


def test_rejects_folder_outside_allowlist(tmp_path):
    writer = VaultWriter(tmp_path / "vault")
    classification = make_classification()
    classification.folder = "not-real"

    with pytest.raises(ValueError, match="unsupported folder: not-real"):
        writer.write_note(
            capture_id="SB-20260607-0001",
            source_message_id="1513233540316266517",
            created_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
            classification=classification,
            model="gemini-mock",
        )


def test_refuses_relative_vault_path():
    with pytest.raises(ValueError, match="vault_path must be absolute"):
        VaultWriter("relative-vault")


def test_render_markdown_omits_actions_section_when_no_actions():
    markdown = render_markdown(
        capture_id="SB-20260607-0001",
        source_message_id="1513233540316266517",
        created_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
        classification=make_classification(actions=[]),
        model="gemini-mock",
    )

    assert "actions:\n  []" in markdown
    assert "## Actions" not in markdown


def test_render_markdown_quotes_frontmatter_values_when_needed():
    markdown = render_markdown(
        capture_id="SB-20260607-0001",
        source_message_id="1513233540316266517",
        created_at=datetime(2026, 6, 7, 12, 29, 24, tzinfo=UTC),
        classification=make_classification(
            note_type="quick note",
            tags=["needs quote", "plain"],
            actions=[{"text": "Review: reconnect handling", "status": "open"}],
        ),
        model="gemini mock",
    )

    assert 'source_message_id: "1513233540316266517"' in markdown
    assert "note_type: quick note" in markdown
    assert "  - needs quote" in markdown
    assert '  - text: "Review: reconnect handling"' in markdown


def test_sanitize_slug_blocks_path_separators_and_empty_values():
    assert sanitize_slug("../HALO reconnect!!") == "halo-reconnect"
    assert sanitize_slug("   ") == "untitled"


def test_yaml_scalar_quotes_unsafe_values():
    assert yaml_scalar("plain value") == "plain value"
    assert yaml_scalar("Review: reconnect") == '"Review: reconnect"'
