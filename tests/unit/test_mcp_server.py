"""Unit tests for SB-123: read-only MCP server tool implementations."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from secondbrain.mcp_server import (
    _clamp_limit,
    _do_list_open_tasks,
    _do_list_recent_notes,
    _do_read_note,
    _do_search_notes,
    _enforce_path,
    _RESULT_LIMIT_MAX,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_note(path: Path, frontmatter: str, body: str = "Note body.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Path-root enforcement
# ---------------------------------------------------------------------------


class TestEnforcePath:
    def test_normal_path_resolves(self, tmp_path):
        note = tmp_path / "notes" / "file.md"
        note.parent.mkdir()
        resolved = _enforce_path(tmp_path, "notes/file.md")
        assert resolved == note.resolve()

    def test_traversal_raises(self, tmp_path):
        with pytest.raises(ValueError, match="path traversal"):
            _enforce_path(tmp_path, "../outside.md")

    def test_double_dot_traversal_raises(self, tmp_path):
        with pytest.raises(ValueError, match="path traversal"):
            _enforce_path(tmp_path, "foo/../../etc/passwd")

    def test_absolute_path_within_vault_is_ok(self, tmp_path):
        inside = tmp_path / "inside.md"
        inside.touch()
        resolved = _enforce_path(tmp_path, "inside.md")
        assert resolved.is_relative_to(tmp_path.resolve())


# ---------------------------------------------------------------------------
# Result limit clamping
# ---------------------------------------------------------------------------


class TestClampLimit:
    def test_default_value(self):
        assert _clamp_limit(None) == 20

    def test_over_max_is_clamped(self):
        assert _clamp_limit(9999) == _RESULT_LIMIT_MAX

    def test_below_one_is_clamped_to_one(self):
        assert _clamp_limit(0) == 1
        assert _clamp_limit(-5) == 1

    def test_valid_value_passes_through(self):
        assert _clamp_limit(10) == 10

    def test_non_integer_uses_default(self):
        assert _clamp_limit("bad") == 20


# ---------------------------------------------------------------------------
# search_notes
# ---------------------------------------------------------------------------


class TestSearchNotes:
    def test_finds_matching_note(self, tmp_path):
        fm = 'capture_id: "SB-20250101-0001"\nproject: "my-project"\ntags:\n  - python'
        _write_note(tmp_path / "20_projects" / "note.md", fm, "This is about asyncio programming.")

        results = _do_search_notes(tmp_path, query="asyncio", folder=None, project=None, tags=None, limit=10)
        assert len(results) == 1
        assert "note.md" in results[0]["note_path"]

    def test_no_match_returns_empty(self, tmp_path):
        _write_note(tmp_path / "note.md", "", "Nothing relevant here.")
        results = _do_search_notes(tmp_path, query="xyzzy_not_found", folder=None, project=None, tags=None, limit=10)
        assert results == []

    def test_folder_filter_excludes_other_folders(self, tmp_path):
        _write_note(tmp_path / "20_projects" / "p.md", "", "target content")
        _write_note(tmp_path / "30_ideas" / "i.md", "", "target content")

        results = _do_search_notes(tmp_path, query="target", folder="20_projects", project=None, tags=None, limit=10)
        assert all("20_projects" in r["note_path"] for r in results)

    def test_project_filter(self, tmp_path):
        _write_note(tmp_path / "a.md", 'project: "proj-a"', "keyword")
        _write_note(tmp_path / "b.md", 'project: "proj-b"', "keyword")

        results = _do_search_notes(tmp_path, query="keyword", folder=None, project="proj-a", tags=None, limit=10)
        assert all(r["project"] == "proj-a" for r in results)

    def test_tag_filter(self, tmp_path):
        _write_note(tmp_path / "tagged.md", "tags:\n  - python\n  - async", "keyword")
        _write_note(tmp_path / "other.md", "tags:\n  - rust", "keyword")

        results = _do_search_notes(tmp_path, query="keyword", folder=None, project=None, tags=["python"], limit=10)
        assert len(results) == 1
        assert "tagged.md" in results[0]["note_path"]

    def test_limit_respected(self, tmp_path):
        for i in range(10):
            _write_note(tmp_path / f"note_{i}.md", "", "searchterm")

        results = _do_search_notes(tmp_path, query="searchterm", folder=None, project=None, tags=None, limit=3)
        assert len(results) <= 3

    def test_case_insensitive_search(self, tmp_path):
        _write_note(tmp_path / "note.md", "", "Contains AsyncIO stuff")
        results = _do_search_notes(tmp_path, query="asyncio", folder=None, project=None, tags=None, limit=10)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# read_note
# ---------------------------------------------------------------------------


class TestReadNote:
    def test_reads_existing_note(self, tmp_path):
        note = tmp_path / "folder" / "file.md"
        _write_note(note, "", "Hello world content.")

        content = _do_read_note(tmp_path, "folder/file.md")
        assert "Hello world content." in content

    def test_missing_note_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _do_read_note(tmp_path, "nonexistent.md")

    def test_path_traversal_raises(self, tmp_path):
        with pytest.raises(ValueError, match="path traversal"):
            _do_read_note(tmp_path, "../../../etc/passwd")

    def test_returns_full_content(self, tmp_path):
        note = tmp_path / "note.md"
        content = "---\ncapture_id: \"SB-x\"\n---\n\n# Title\n\nBody text.\n"
        note.write_text(content, encoding="utf-8")

        result = _do_read_note(tmp_path, "note.md")
        assert result == content


# ---------------------------------------------------------------------------
# list_recent_notes
# ---------------------------------------------------------------------------


class TestListRecentNotes:
    def test_finds_recently_modified_note(self, tmp_path):
        note = tmp_path / "recent.md"
        note.write_text("content", encoding="utf-8")
        # mtime is effectively now

        results = _do_list_recent_notes(tmp_path, days=7, folder=None, limit=10)
        assert any("recent.md" in r["note_path"] for r in results)

    def test_excludes_old_notes(self, tmp_path):
        note = tmp_path / "old.md"
        note.write_text("content", encoding="utf-8")
        # Set mtime to 30 days ago
        import os, time
        old_ts = (datetime.now(UTC) - timedelta(days=30)).timestamp()
        os.utime(note, (old_ts, old_ts))

        results = _do_list_recent_notes(tmp_path, days=7, folder=None, limit=10)
        assert not any("old.md" in r["note_path"] for r in results)

    def test_limit_respected(self, tmp_path):
        for i in range(10):
            (tmp_path / f"note_{i}.md").write_text("x", encoding="utf-8")

        results = _do_list_recent_notes(tmp_path, days=7, folder=None, limit=3)
        assert len(results) <= 3

    def test_folder_filter(self, tmp_path):
        (tmp_path / "20_projects").mkdir()
        (tmp_path / "30_ideas").mkdir()
        (tmp_path / "20_projects" / "p.md").write_text("x", encoding="utf-8")
        (tmp_path / "30_ideas" / "i.md").write_text("x", encoding="utf-8")

        results = _do_list_recent_notes(tmp_path, days=7, folder="20_projects", limit=10)
        assert all("20_projects" in r["note_path"] for r in results)

    def test_returns_modified_at_iso_string(self, tmp_path):
        note = tmp_path / "note.md"
        note.write_text("x", encoding="utf-8")

        results = _do_list_recent_notes(tmp_path, days=7, folder=None, limit=10)
        assert len(results) >= 1
        # Should be parseable as ISO datetime
        datetime.fromisoformat(results[0]["modified_at"])


# ---------------------------------------------------------------------------
# list_open_tasks
# ---------------------------------------------------------------------------


class TestListOpenTasks:
    def _make_note_with_actions(self, vault: Path, name: str, project: str | None, actions: list[tuple[str, str]]) -> None:
        fm_lines = ['capture_id: "SB-test-0001"']
        if project:
            fm_lines.append(f'project: "{project}"')
        fm_lines.append("actions:")
        for text, status in actions:
            fm_lines.append(f'  - text: "{text}"')
            fm_lines.append(f'    status: {status}')
        fm = "\n".join(fm_lines)
        _write_note(vault / name, fm, "Body text.")

    def test_finds_note_with_open_action(self, tmp_path):
        self._make_note_with_actions(tmp_path, "note.md", None, [("Do the thing", "open")])
        results = _do_list_open_tasks(tmp_path, project=None, limit=10)
        assert len(results) == 1
        assert results[0]["open_actions"] == ["Do the thing"]

    def test_excludes_done_actions(self, tmp_path):
        self._make_note_with_actions(tmp_path, "note.md", None, [("Finished task", "done")])
        results = _do_list_open_tasks(tmp_path, project=None, limit=10)
        assert results == []

    def test_mixed_actions_returns_only_open(self, tmp_path):
        self._make_note_with_actions(
            tmp_path, "note.md", None,
            [("Open task", "open"), ("Done task", "done")],
        )
        results = _do_list_open_tasks(tmp_path, project=None, limit=10)
        assert len(results) == 1
        assert results[0]["open_actions"] == ["Open task"]

    def test_project_filter(self, tmp_path):
        self._make_note_with_actions(tmp_path, "a.md", "project-a", [("Task A", "open")])
        self._make_note_with_actions(tmp_path, "b.md", "project-b", [("Task B", "open")])

        results = _do_list_open_tasks(tmp_path, project="project-a", limit=10)
        assert len(results) == 1
        assert results[0]["project"] == "project-a"

    def test_limit_respected(self, tmp_path):
        for i in range(10):
            self._make_note_with_actions(tmp_path, f"note_{i}.md", None, [("Task", "open")])

        results = _do_list_open_tasks(tmp_path, project=None, limit=3)
        assert len(results) <= 3

    def test_empty_vault_returns_empty(self, tmp_path):
        results = _do_list_open_tasks(tmp_path, project=None, limit=10)
        assert results == []

    def test_note_without_actions_excluded(self, tmp_path):
        _write_note(tmp_path / "no_actions.md", 'capture_id: "SB-x"\nactions:\n  []', "Body.")
        results = _do_list_open_tasks(tmp_path, project=None, limit=10)
        assert results == []
