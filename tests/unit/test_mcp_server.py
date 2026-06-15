"""Unit tests for SB-123: read-only MCP server tool implementations."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from secondbrain.mcp_server import (
    _clamp_limit,
    _do_get_sync_status,
    _do_list_open_tasks,
    _do_list_recent_notes,
    _do_read_note,
    _do_search_notes,
    _enforce_path,
    _ledger_path,
    _vault_preflight,
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

    def test_non_markdown_file_rejected(self, tmp_path):
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="only markdown"):
            _do_read_note(tmp_path, "config.json")

    def test_git_config_rejected(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]", encoding="utf-8")
        with pytest.raises(ValueError, match="hidden"):
            _do_read_note(tmp_path, ".git/config")

    def test_hidden_file_at_root_rejected(self, tmp_path):
        (tmp_path / ".writer.lock").write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="hidden"):
            _do_read_note(tmp_path, ".writer.lock")

    def test_hidden_directory_component_rejected(self, tmp_path):
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "note.md").write_text("content", encoding="utf-8")
        with pytest.raises(ValueError, match="hidden"):
            _do_read_note(tmp_path, ".hidden/note.md")


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

    def test_quoted_status_open_is_recognised(self, tmp_path):
        """writer-service renders status: "open" (json.dumps); must be counted."""
        fm = (
            'capture_id: "SB-20250101-0001"\n'
            'actions:\n'
            '  - text: "Quoted task"\n'
            '    status: "open"\n'
        )
        _write_note(tmp_path / "quoted.md", fm)
        results = _do_list_open_tasks(tmp_path, project=None, limit=10)
        assert len(results) == 1
        assert results[0]["open_actions"] == ["Quoted task"]

    def test_unquoted_status_open_is_recognised(self, tmp_path):
        """Hand-authored notes with unquoted status: open must also be counted."""
        fm = (
            'capture_id: "SB-20250101-0002"\n'
            'actions:\n'
            '  - text: "Unquoted task"\n'
            '    status: open\n'
        )
        _write_note(tmp_path / "unquoted.md", fm)
        results = _do_list_open_tasks(tmp_path, project=None, limit=10)
        assert len(results) == 1
        assert results[0]["open_actions"] == ["Unquoted task"]


# ---------------------------------------------------------------------------
# ledger_path optional
# ---------------------------------------------------------------------------


class TestLedgerPath:
    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("LEDGER_PATH", raising=False)
        assert _ledger_path() is None

    def test_returns_path_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
        assert _ledger_path() == tmp_path / "ledger.sqlite3"

    def test_empty_string_returns_none(self, monkeypatch):
        monkeypatch.setenv("LEDGER_PATH", "   ")
        assert _ledger_path() is None


# ---------------------------------------------------------------------------
# get_sync_status
# ---------------------------------------------------------------------------


class TestGetSyncStatus:
    def test_no_ledger_path_returns_partial_result(self, tmp_path):
        result = _do_get_sync_status(None, tmp_path)
        assert result["ledger_path"] is None
        assert result["ledger_exists"] is False
        assert result["vault_path"] == str(tmp_path)

    def test_missing_ledger_file_returns_not_exists(self, tmp_path):
        result = _do_get_sync_status(tmp_path / "no-ledger.sqlite3", None)
        assert result["ledger_exists"] is False
        assert result["vault_path"] is None

    def test_vault_git_status_included_when_vault_is_clean_repo(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        (tmp_path / "note.md").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        result = _do_get_sync_status(None, tmp_path)
        assert "vault_dirty" in result
        assert result["vault_dirty"] is False
        assert result["vault_head_commit"] is not None

    def test_both_none_returns_minimal_result(self):
        result = _do_get_sync_status(None, None)
        assert result["ledger_path"] is None
        assert result["vault_path"] is None
        assert result["ledger_exists"] is False


# ---------------------------------------------------------------------------
# vault_preflight
# ---------------------------------------------------------------------------


class TestVaultPreflight:
    def test_none_vault_path_returns_error(self):
        err = _vault_preflight(None)
        assert err is not None
        assert "not configured" in err

    def test_nonexistent_path_returns_error(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        err = _vault_preflight(missing)
        assert err is not None
        assert "does not exist" in err

    def test_clean_git_repo_returns_none(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        (tmp_path / "note.md").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        err = _vault_preflight(tmp_path)
        assert err is None

    def test_dirty_git_repo_returns_warning(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        (tmp_path / "note.md").write_text("initial")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        # Make a tracked file dirty
        (tmp_path / "note.md").write_text("modified")
        err = _vault_preflight(tmp_path)
        assert err is not None
        assert "stale" in err or "uncommitted" in err

    def test_untracked_obsidian_files_do_not_trigger_warning(self, tmp_path):
        """Obsidian writes untracked workspace/cache files; preflight must not block on them."""
        import subprocess
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        (tmp_path / "note.md").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        # Simulate Obsidian writing untracked files
        obsidian = tmp_path / ".obsidian"
        obsidian.mkdir()
        (obsidian / "workspace.json").write_text("{}")
        (tmp_path / ".trash").mkdir()
        (tmp_path / ".trash" / "old-note.md").write_text("deleted")
        err = _vault_preflight(tmp_path)
        assert err is None
