"""Unit tests for SB-122: vault pull script."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from secondbrain.vault_pull import VaultPullError, pull_vault


def _make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestPullVaultPathValidation:
    def test_missing_vault_path_raises(self, tmp_path):
        missing = tmp_path / "no_such_vault"
        with pytest.raises(VaultPullError) as exc_info:
            pull_vault(missing)
        assert "does not exist" in str(exc_info.value)
        assert exc_info.value.exit_code == 1

    def test_existing_path_proceeds(self, tmp_path):
        clean_status = _make_completed(returncode=0, stdout="")
        fetch_result = _make_completed(returncode=0)
        merge_result = _make_completed(returncode=0)

        with patch("subprocess.run", side_effect=[clean_status, clean_status, fetch_result, merge_result]):
            pull_vault(tmp_path)  # should not raise


class TestDirtyWorktree:
    def test_dirty_worktree_raises_visible_error(self, tmp_path):
        dirty_status = _make_completed(returncode=0, stdout="M some_file.md\n")

        with patch("subprocess.run", side_effect=[dirty_status, dirty_status]):
            with pytest.raises(VaultPullError) as exc_info:
                pull_vault(tmp_path)

        assert "dirty working tree" in str(exc_info.value)
        assert exc_info.value.exit_code == 2

    def test_dirty_worktree_includes_changed_files(self, tmp_path):
        dirty_status = _make_completed(returncode=0, stdout="M notes/thing.md\nA new_file.md\n")

        with patch("subprocess.run", side_effect=[dirty_status, dirty_status]):
            with pytest.raises(VaultPullError) as exc_info:
                pull_vault(tmp_path)

        assert "notes/thing.md" in str(exc_info.value) or "new_file.md" in str(exc_info.value)


class TestFetchFailure:
    def test_fetch_failure_raises_with_exit_code(self, tmp_path):
        clean_status = _make_completed(returncode=0, stdout="")
        fetch_fail = _make_completed(returncode=128, stderr="fatal: unable to reach remote")

        with patch("subprocess.run", side_effect=[clean_status, clean_status, fetch_fail]):
            with pytest.raises(VaultPullError) as exc_info:
                pull_vault(tmp_path)

        assert "fetch" in str(exc_info.value).lower()
        assert exc_info.value.exit_code == 128


class TestMergeFailure:
    def test_diverged_history_raises(self, tmp_path):
        clean_status = _make_completed(returncode=0, stdout="")
        fetch_ok = _make_completed(returncode=0)
        merge_fail = _make_completed(
            returncode=1, stderr="fatal: Not possible to fast-forward, aborting."
        )

        with patch("subprocess.run", side_effect=[clean_status, clean_status, fetch_ok, merge_fail]):
            with pytest.raises(VaultPullError) as exc_info:
                pull_vault(tmp_path)

        assert "merge" in str(exc_info.value).lower()
        assert exc_info.value.exit_code == 1

    def test_conflict_raises_visible_error(self, tmp_path):
        clean_status = _make_completed(returncode=0, stdout="")
        fetch_ok = _make_completed(returncode=0)
        merge_fail = _make_completed(
            returncode=1, stderr="CONFLICT (content): Merge conflict in notes/foo.md"
        )

        with patch("subprocess.run", side_effect=[clean_status, clean_status, fetch_ok, merge_fail]):
            with pytest.raises(VaultPullError) as exc_info:
                pull_vault(tmp_path)

        assert exc_info.value.exit_code == 1


class TestSuccessfulPull:
    def test_successful_pull_does_not_raise(self, tmp_path):
        clean_status = _make_completed(returncode=0, stdout="")
        fetch_ok = _make_completed(returncode=0)
        merge_ok = _make_completed(returncode=0, stdout="Updating abc1234..def5678")

        with patch("subprocess.run", side_effect=[clean_status, clean_status, fetch_ok, merge_ok]):
            pull_vault(tmp_path)  # no exception

    def test_already_up_to_date_does_not_raise(self, tmp_path):
        clean_status = _make_completed(returncode=0, stdout="")
        fetch_ok = _make_completed(returncode=0)
        merge_ok = _make_completed(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run", side_effect=[clean_status, clean_status, fetch_ok, merge_ok]):
            pull_vault(tmp_path)  # no exception


class TestGitCommandOrder:
    def test_fetch_before_merge(self, tmp_path):
        """Verify git commands are issued in correct order: status → fetch → merge."""
        call_order = []

        def record_call(cmd, **kwargs):
            call_order.append(cmd)
            return _make_completed(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=record_call):
            pull_vault(tmp_path)

        assert any("status" in " ".join(c) for c in call_order), "git status must be called"
        assert any("fetch" in " ".join(c) for c in call_order), "git fetch must be called"
        assert any("merge" in " ".join(c) for c in call_order), "git merge must be called"

        fetch_idx = next(i for i, c in enumerate(call_order) if "fetch" in " ".join(c))
        merge_idx = next(i for i, c in enumerate(call_order) if "merge" in " ".join(c))
        assert fetch_idx < merge_idx, "git fetch must happen before git merge"

    def test_ff_only_flag_in_merge_command(self, tmp_path):
        """Verify merge uses --ff-only to prevent diverged merges."""
        merge_cmds = []

        def record_call(cmd, **kwargs):
            if "merge" in cmd:
                merge_cmds.append(cmd)
            return _make_completed(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=record_call):
            pull_vault(tmp_path)

        assert len(merge_cmds) == 1
        assert "--ff-only" in merge_cmds[0]

    def test_merges_origin_main(self, tmp_path):
        """Verify merge targets origin/main specifically."""
        merge_cmds = []

        def record_call(cmd, **kwargs):
            if "merge" in cmd:
                merge_cmds.append(cmd)
            return _make_completed(returncode=0, stdout="")

        with patch("subprocess.run", side_effect=record_call):
            pull_vault(tmp_path)

        assert len(merge_cmds) == 1
        assert "origin/main" in merge_cmds[0]
