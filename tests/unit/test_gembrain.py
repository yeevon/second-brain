"""Unit tests for SB-124: gembrain CLI."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from secondbrain.gembrain import cmd_ask, cmd_recent, cmd_status, cmd_tasks


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ---------------------------------------------------------------------------
# gembrain status
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def test_status_no_vault_path_degrades_gracefully(self, capsys):
        with patch.dict("os.environ", {}, clear=True):
            exit_code = cmd_status()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["vault_configured"] is False
        assert data["vault_path"] is None
        assert exit_code == 0  # status degrades, does not error

    def test_status_vault_missing_reports_not_exists(self, tmp_path, capsys):
        missing = tmp_path / "no_vault"
        with patch.dict("os.environ", {"VAULT_PATH": str(missing)}):
            exit_code = cmd_status()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["vault_exists"] is False

    def test_status_clean_and_synced_exits_0(self, tmp_path, capsys):
        def fake_git(cmd, **kwargs):
            if "log" in cmd and "origin/main" not in cmd:
                return _completed(stdout="abc1234 Add feature\n")
            if "log" in cmd and "origin/main" in cmd:
                return _completed(stdout="abc1234 Add feature\n")
            if "rev-list" in cmd:
                return _completed(stdout="0\n")
            if "status" in cmd:
                return _completed(stdout="")
            return _completed()

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("subprocess.run", side_effect=fake_git):
                exit_code = cmd_status()

        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["up_to_date"] is True
        assert data["dirty_worktree"] is False
        assert exit_code == 0

    def test_status_dirty_vault_exits_1(self, tmp_path, capsys):
        def fake_git(cmd, **kwargs):
            if "status" in cmd:
                return _completed(stdout="M notes/foo.md\n")
            if "rev-list" in cmd:
                return _completed(stdout="0\n")
            return _completed(stdout="abc1234 head\n")

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("subprocess.run", side_effect=fake_git):
                exit_code = cmd_status()

        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["dirty_worktree"] is True
        assert exit_code == 1

    def test_status_local_behind_remote_exits_1(self, tmp_path, capsys):
        def fake_git(cmd, **kwargs):
            if "rev-list" in cmd:
                return _completed(stdout="3\n")
            if "status" in cmd:
                return _completed(stdout="")
            return _completed(stdout="abc1234 head\n")

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("subprocess.run", side_effect=fake_git):
                exit_code = cmd_status()

        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["commits_behind"] == 3
        assert data["up_to_date"] is False
        assert exit_code == 1

    def test_status_local_equals_remote_exits_0(self, tmp_path, capsys):
        def fake_git(cmd, **kwargs):
            if "rev-list" in cmd:
                return _completed(stdout="0\n")
            if "status" in cmd:
                return _completed(stdout="")
            return _completed(stdout="abc1234 head\n")

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("subprocess.run", side_effect=fake_git):
                exit_code = cmd_status()

        data = json.loads(capsys.readouterr().out)
        assert data["up_to_date"] is True
        assert exit_code == 0

    def test_status_includes_pending_conflict_flag(self, tmp_path, capsys):
        # Create .git/MERGE_HEAD to simulate a conflict
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").write_text("deadbeef")

        def fake_git(cmd, **kwargs):
            if "status" in cmd:
                return _completed(stdout="")
            if "rev-list" in cmd:
                return _completed(stdout="0\n")
            return _completed(stdout="abc1234 head\n")

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("subprocess.run", side_effect=fake_git):
                exit_code = cmd_status()

        data = json.loads(capsys.readouterr().out)
        assert data["pending_conflict"] is True
        assert exit_code == 1


# ---------------------------------------------------------------------------
# gembrain recent
# ---------------------------------------------------------------------------


class TestCmdRecent:
    def test_recent_no_vault_path_exits_1(self, capsys):
        with patch.dict("os.environ", {}, clear=True):
            exit_code = cmd_recent(days=7, folder=None, limit=20)
        assert exit_code == 1
        assert "VAULT_PATH" in capsys.readouterr().err

    def test_recent_runs_preflight_before_query(self, tmp_path):
        preflight_called = []

        def fake_pull(vault_path):
            preflight_called.append(vault_path)

        fake_results = [{"note_path": "inbox/note.md", "modified_at": "2026-06-15T10:00:00+00:00"}]

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault", side_effect=fake_pull):
                with patch("secondbrain.mcp_server._do_list_recent_notes", return_value=fake_results):
                    exit_code = cmd_recent(days=7, folder=None, limit=20)

        assert exit_code == 0
        assert len(preflight_called) == 1

    def test_recent_preflight_failure_aborts_before_query(self, tmp_path, capsys):
        from secondbrain.vault_pull import VaultPullError

        query_called = []

        def fail_pull(vault_path):
            raise VaultPullError("dirty working tree", exit_code=2)

        def fake_query(*args, **kwargs):
            query_called.append(True)
            return []

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault", side_effect=fail_pull):
                with patch("secondbrain.mcp_server._do_list_recent_notes", side_effect=fake_query):
                    exit_code = cmd_recent(days=7, folder=None, limit=20)

        assert exit_code == 2
        assert len(query_called) == 0
        assert "preflight" in capsys.readouterr().err.lower()

    def test_recent_calls_list_recent_notes_with_correct_args(self, tmp_path, capsys):
        captured_kwargs: list[dict] = []

        def fake_query(vault_path, *, days, folder, limit):
            captured_kwargs.append({"days": days, "folder": folder, "limit": limit})
            return []

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("secondbrain.mcp_server._do_list_recent_notes", side_effect=fake_query):
                    cmd_recent(days=14, folder="20_projects", limit=5)

        assert len(captured_kwargs) == 1
        assert captured_kwargs[0] == {"days": 14, "folder": "20_projects", "limit": 5}

    def test_recent_formats_results_to_stdout(self, tmp_path, capsys):
        results = [
            {"note_path": "inbox/a.md", "modified_at": "2026-06-15T10:00:00+00:00"},
            {"note_path": "inbox/b.md", "modified_at": "2026-06-14T09:00:00+00:00"},
        ]

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("secondbrain.mcp_server._do_list_recent_notes", return_value=results):
                    exit_code = cmd_recent(days=7, folder=None, limit=20)

        out = capsys.readouterr().out
        assert "inbox/a.md" in out
        assert "inbox/b.md" in out
        assert exit_code == 0

    def test_recent_empty_result_prints_message(self, tmp_path, capsys):
        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("secondbrain.mcp_server._do_list_recent_notes", return_value=[]):
                    exit_code = cmd_recent(days=7, folder=None, limit=20)

        out = capsys.readouterr().out
        assert "No notes" in out
        assert exit_code == 0


# ---------------------------------------------------------------------------
# gembrain tasks
# ---------------------------------------------------------------------------


class TestCmdTasks:
    def test_tasks_no_vault_path_exits_1(self, capsys):
        with patch.dict("os.environ", {}, clear=True):
            exit_code = cmd_tasks(project=None, limit=20)
        assert exit_code == 1
        assert "VAULT_PATH" in capsys.readouterr().err

    def test_tasks_runs_preflight_before_query(self, tmp_path):
        preflight_called = []

        def fake_pull(vault_path):
            preflight_called.append(vault_path)

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault", side_effect=fake_pull):
                with patch("secondbrain.digest.scan_open_task_list", return_value=[]):
                    cmd_tasks(project=None, limit=20)

        assert len(preflight_called) == 1

    def test_tasks_project_filter_passed_through(self, tmp_path):
        captured: list[dict] = []

        def fake_scan(vault_path, *, project, limit):
            captured.append({"project": project, "limit": limit})
            return []

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("secondbrain.digest.scan_open_task_list", side_effect=fake_scan):
                    cmd_tasks(project="second-brain", limit=10)

        assert len(captured) == 1
        assert captured[0]["project"] == "second-brain"
        assert captured[0]["limit"] == 10

    def test_tasks_formats_output_grouped_by_note(self, tmp_path, capsys):
        results = [
            {
                "note_path": "20_projects/sb/2026-06-15--SB-001--task.md",
                "project": "second-brain",
                "capture_id": "SB-20260615-0001",
                "open_actions": ["Review the MCP server", "Write unit tests"],
            }
        ]

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("secondbrain.digest.scan_open_task_list", return_value=results):
                    exit_code = cmd_tasks(project=None, limit=20)

        out = capsys.readouterr().out
        assert "Review the MCP server" in out
        assert "Write unit tests" in out
        assert exit_code == 0

    def test_tasks_preflight_failure_aborts_before_scan(self, tmp_path, capsys):
        from secondbrain.vault_pull import VaultPullError

        scan_called = []

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch(
                "secondbrain.vault_pull.pull_vault",
                side_effect=VaultPullError("fetch failed", exit_code=128),
            ):
                with patch(
                    "secondbrain.digest.scan_open_task_list",
                    side_effect=lambda *a, **kw: scan_called.append(True) or [],
                ):
                    exit_code = cmd_tasks(project=None, limit=20)

        assert exit_code == 128
        assert len(scan_called) == 0


# ---------------------------------------------------------------------------
# gembrain ask
# ---------------------------------------------------------------------------


class TestCmdAsk:
    def test_ask_no_vault_path_exits_1(self, capsys):
        with patch.dict("os.environ", {}, clear=True):
            exit_code = cmd_ask("What are my open tasks?")
        assert exit_code == 1
        assert "VAULT_PATH" in capsys.readouterr().err

    def test_ask_gemini_not_found_exits_with_error(self, tmp_path, capsys):
        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("shutil.which", return_value=None):
                    exit_code = cmd_ask("What tasks are open?")

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "gemini" in err.lower()
        assert "not found" in err.lower()

    def test_ask_preflight_failure_aborts_before_gemini(self, tmp_path, capsys):
        from secondbrain.vault_pull import VaultPullError

        gemini_called = []

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch(
                "secondbrain.vault_pull.pull_vault",
                side_effect=VaultPullError("dirty", exit_code=2),
            ):
                with patch("shutil.which", return_value="/usr/bin/gemini"):
                    with patch(
                        "subprocess.run",
                        side_effect=lambda *a, **kw: gemini_called.append(True) or _completed(),
                    ):
                        exit_code = cmd_ask("What tasks are open?")

        assert exit_code == 2
        assert len(gemini_called) == 0

    def test_ask_constructs_correct_gemini_command(self, tmp_path):
        gemini_calls: list = []

        def fake_run(cmd, **kwargs):
            gemini_calls.append(cmd)
            return _completed(returncode=0)

        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path), "LEDGER_PATH": "/tmp/ledger.db"}):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("shutil.which", return_value="/usr/bin/gemini"):
                    with patch("subprocess.run", side_effect=fake_run):
                        exit_code = cmd_ask("What are my open tasks?")

        assert exit_code == 0
        assert len(gemini_calls) == 1
        cmd = gemini_calls[0]
        assert cmd[0] == "/usr/bin/gemini"
        assert "--mcp-server" in cmd
        assert "brain-mcp" in cmd
        assert "What are my open tasks?" in cmd

    def test_ask_forwards_vault_and_ledger_path_to_env(self, tmp_path):
        captured_envs: list[dict] = []

        def fake_run(cmd, env=None, **kwargs):
            if env is not None:
                captured_envs.append(dict(env))
            return _completed()

        with patch.dict(
            "os.environ",
            {"VAULT_PATH": str(tmp_path), "LEDGER_PATH": "/data/ledger.db"},
        ):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("shutil.which", return_value="/usr/bin/gemini"):
                    with patch("subprocess.run", side_effect=fake_run):
                        cmd_ask("Summarize recent notes")

        assert len(captured_envs) == 1
        assert captured_envs[0]["VAULT_PATH"] == str(tmp_path)
        assert captured_envs[0]["LEDGER_PATH"] == "/data/ledger.db"

    def test_ask_propagates_gemini_exit_code(self, tmp_path):
        with patch.dict("os.environ", {"VAULT_PATH": str(tmp_path)}):
            with patch("secondbrain.vault_pull.pull_vault"):
                with patch("shutil.which", return_value="/usr/bin/gemini"):
                    with patch("subprocess.run", return_value=_completed(returncode=42)):
                        exit_code = cmd_ask("What is my focus today?")

        assert exit_code == 42
