"""Unit tests for git_ops subprocess wrappers."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from writerservice.git_errors import (
    GitAddError,
    GitCommitError,
    GitFetchError,
    GitIndexLockedError,
    GitMergeConflictError,
    GitPushError,
    GitPushRejectedError,
    GitWorkdirDirtyError,
)
from writerservice.git_ops import (
    check_index_lock,
    check_working_tree_clean,
    git_add,
    git_commit,
    git_fetch,
    git_log_hash_for_path,
    git_merge_ff_only,
    git_push,
    git_rev_parse_head,
)


# ── check_index_lock ──────────────────────────────────────────────────────────

def test_check_index_lock_raises_when_lock_exists(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "index.lock").touch()
    with pytest.raises(GitIndexLockedError):
        check_index_lock(tmp_path)


def test_check_index_lock_does_not_delete_lock(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    lock = git_dir / "index.lock"
    lock.touch()
    with pytest.raises(GitIndexLockedError):
        check_index_lock(tmp_path)
    assert lock.exists()


def test_check_index_lock_passes_when_no_lock(tmp_path):
    (tmp_path / ".git").mkdir()
    check_index_lock(tmp_path)  # must not raise


def test_git_index_locked_error_is_retryable():
    assert GitIndexLockedError.retryable is True
    assert GitIndexLockedError.http_status == 503


# ── check_working_tree_clean ──────────────────────────────────────────────────

def test_check_working_tree_clean_raises_on_dirty_tree(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "dirty.md").write_text("untracked")
    with pytest.raises(GitWorkdirDirtyError):
        check_working_tree_clean(tmp_path)


def test_check_working_tree_clean_does_not_modify_files(tmp_path):
    _init_git_repo(tmp_path)
    dirty = tmp_path / "dirty.md"
    dirty.write_text("untracked content")
    with pytest.raises(GitWorkdirDirtyError):
        check_working_tree_clean(tmp_path)
    assert dirty.read_text() == "untracked content"


def test_check_working_tree_clean_passes_on_clean_repo(tmp_path):
    _init_git_repo(tmp_path)
    check_working_tree_clean(tmp_path)  # must not raise


def test_git_worktree_dirty_error_is_not_retryable():
    assert GitWorkdirDirtyError.retryable is False
    assert GitWorkdirDirtyError.http_status == 503


# ── git_fetch ─────────────────────────────────────────────────────────────────

def test_git_fetch_raises_on_non_zero_exit(tmp_path):
    _init_git_repo(tmp_path)
    with pytest.raises(GitFetchError):
        git_fetch(tmp_path)  # no remote → non-zero exit


def test_git_fetch_error_is_retryable():
    assert GitFetchError.retryable is True


# ── git_merge_ff_only ─────────────────────────────────────────────────────────

def test_git_merge_ff_only_raises_on_no_remote(tmp_path):
    _init_git_repo(tmp_path)
    with pytest.raises(GitMergeConflictError):
        git_merge_ff_only(tmp_path)


def test_git_merge_conflict_error_not_retryable():
    assert GitMergeConflictError.retryable is False
    assert GitMergeConflictError.http_status == 409


def test_git_merge_ff_only_succeeds_on_up_to_date_clone(tmp_path):
    bare, clone = _init_bare_and_clone(tmp_path)
    git_fetch(clone)
    git_merge_ff_only(clone)  # must not raise (up-to-date)


# ── git_add ───────────────────────────────────────────────────────────────────

def test_git_add_stages_file(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "note.md").write_text("content")
    git_add(tmp_path, "note.md")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    )
    assert "A  note.md" in status.stdout


def test_git_add_raises_on_missing_file(tmp_path):
    _init_git_repo(tmp_path)
    with pytest.raises(GitAddError):
        git_add(tmp_path, "nonexistent.md")


# ── git_commit ────────────────────────────────────────────────────────────────

def test_git_commit_creates_commit_with_expected_message(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "note.md").write_text("content")
    git_add(tmp_path, "note.md")
    git_commit(tmp_path, "note: SB-20260612-0001 via writer-service")
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=tmp_path, capture_output=True, text=True
    )
    assert "SB-20260612-0001" in log.stdout


def test_git_commit_message_contains_capture_id(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "note.md").write_text("content")
    git_add(tmp_path, "note.md")
    git_commit(tmp_path, "note: SB-20260613-4242 via writer-service")
    log = subprocess.run(
        ["git", "log", "--format=%B", "-1"], cwd=tmp_path, capture_output=True, text=True
    )
    assert "SB-20260613-4242" in log.stdout


def test_git_commit_raises_on_empty_repo_with_no_staged_files(tmp_path):
    _init_git_repo(tmp_path)
    with pytest.raises(GitCommitError):
        git_commit(tmp_path, "note: SB-20260612-0001 via writer-service")


# ── git_push ──────────────────────────────────────────────────────────────────

def test_git_push_succeeds_on_bare_remote(tmp_path):
    bare, clone = _init_bare_and_clone(tmp_path)
    (clone / "note.md").write_text("content")
    git_add(clone, "note.md")
    git_commit(clone, "note: SB-20260612-0001 via writer-service")
    git_push(clone)  # must not raise


def test_git_push_raises_rejected_error_on_non_fast_forward(tmp_path):
    bare, clone1 = _init_bare_and_clone(tmp_path)
    clone2 = tmp_path / "clone2"
    subprocess.run(["git", "clone", str(bare), str(clone2)], check=True, capture_output=True)
    _configure_git_user(clone2)

    # clone1 pushes a commit
    (clone1 / "a.md").write_text("from clone1")
    git_add(clone1, "a.md")
    git_commit(clone1, "note: SB-20260612-0001 via writer-service")
    git_push(clone1)

    # clone2 also commits (diverged) and tries to push
    (clone2 / "b.md").write_text("from clone2")
    subprocess.run(["git", "add", "b.md"], cwd=clone2, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "note: SB-20260612-0002 via writer-service"],
        cwd=clone2, check=True, capture_output=True,
    )
    with pytest.raises(GitPushRejectedError):
        git_push(clone2)


def test_git_push_rejected_error_is_retryable():
    assert GitPushRejectedError.retryable is True
    assert GitPushRejectedError.http_status == 409


def test_git_push_raises_generic_error_on_no_remote(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "note.md").write_text("content")
    git_add(tmp_path, "note.md")
    git_commit(tmp_path, "note: SB-20260612-0001 via writer-service")
    with pytest.raises(GitPushError):
        git_push(tmp_path)


# ── git_rev_parse_head / git_log_hash_for_path ───────────────────────────────

def test_git_rev_parse_head_returns_hash(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "note.md").write_text("content")
    git_add(tmp_path, "note.md")
    git_commit(tmp_path, "note: SB-20260612-0001 via writer-service")
    h = git_rev_parse_head(tmp_path)
    assert len(h) == 40
    assert all(c in "0123456789abcdef" for c in h)


def test_git_log_hash_for_path_returns_hash(tmp_path):
    _init_git_repo(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("content")
    git_add(tmp_path, "note.md")
    git_commit(tmp_path, "note: SB-20260612-0001 via writer-service")
    h = git_log_hash_for_path(tmp_path, note)
    assert h is not None
    assert len(h) == 40


def test_git_log_hash_for_path_returns_none_for_untracked(tmp_path):
    _init_git_repo(tmp_path)
    note = tmp_path / "untracked.md"
    note.write_text("not committed")
    h = git_log_hash_for_path(tmp_path, note)
    assert h is None


# ── Error class attributes ────────────────────────────────────────────────────

def test_all_writer_errors_have_required_attributes():
    from writerservice.git_errors import (
        CaptureDuplicateError,
        GitFetchError,
        GitIndexLockedError,
        GitMergeConflictError,
        GitPushError,
        GitPushRejectedError,
        GitWorkdirDirtyError,
        PathTraversalError,
        WriterError,
    )
    for cls in [
        GitFetchError, GitMergeConflictError, GitIndexLockedError,
        GitWorkdirDirtyError, GitPushRejectedError, GitPushError,
        CaptureDuplicateError, PathTraversalError,
    ]:
        assert hasattr(cls, 'error_type'), f"{cls.__name__} missing error_type"
        assert hasattr(cls, 'http_status'), f"{cls.__name__} missing http_status"
        assert hasattr(cls, 'retryable'), f"{cls.__name__} missing retryable"
        assert issubclass(cls, WriterError)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    _configure_git_user(path)


def _configure_git_user(path: Path) -> None:
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)


def _init_bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
    _configure_git_user(clone)
    # Create initial commit so clone has a HEAD
    (clone / "README.md").write_text("vault")
    subprocess.run(["git", "add", "README.md"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial vault structure"],
        cwd=clone, check=True, capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=clone, check=True, capture_output=True)
    return bare, clone
