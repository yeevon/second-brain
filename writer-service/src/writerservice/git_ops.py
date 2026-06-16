from __future__ import annotations

import subprocess
from pathlib import Path

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

# Timeout (seconds) for local Git operations (add, commit, status, rev-parse).
_LOCAL_TIMEOUT = 15
# Timeout (seconds) for network Git operations (fetch, push).
_NETWORK_TIMEOUT = 60


def check_index_lock(vault_path: Path) -> None:
    lock_file = vault_path / ".git" / "index.lock"
    if lock_file.exists():
        raise GitIndexLockedError(
            "Git index lock exists. A previous Git operation may have been interrupted. "
            "Verify no Git process is running, then delete the lock file manually."
        )


def check_working_tree_clean(vault_path: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=_LOCAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitWorkdirDirtyError(
            "git status timed out: vault may be unresponsive."
        )
    if result.returncode != 0:
        raise GitWorkdirDirtyError(
            "git status failed: vault may not be a valid Git repository. "
            "Inspect the vault and verify it is properly initialized."
        )
    if result.stdout.strip():
        raise GitWorkdirDirtyError(
            "Vault working tree is not clean. "
            "A previous write may have crashed after filesystem mutation. "
            "Inspect uncommitted changes before retrying."
        )


def git_fetch(vault_path: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=_NETWORK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitFetchError("git fetch timed out: network unreachable or SSH hung.")
    if result.returncode != 0:
        raise GitFetchError(
            f"git fetch failed with exit code {result.returncode}: {result.stderr.strip()}"
        )


def git_merge_ff_only(vault_path: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "merge", "--ff-only", "origin/main"],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=_LOCAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitMergeConflictError(
            "git merge --ff-only timed out."
        )
    if result.returncode != 0:
        raise GitMergeConflictError(
            "git merge --ff-only failed: local clone has diverged from origin/main. "
            "Operator inspection required."
        )


def git_add(vault_path: Path, *paths: str) -> None:
    try:
        result = subprocess.run(
            ["git", "add", "--", *paths],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=_LOCAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitAddError("git add timed out.")
    if result.returncode != 0:
        raise GitAddError(
            f"git add failed with exit code {result.returncode}"
        )


def git_commit(vault_path: Path, message: str) -> None:
    try:
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=_LOCAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitCommitError("git commit timed out.")
    if result.returncode != 0:
        raise GitCommitError(
            f"git commit failed with exit code {result.returncode}"
        )


def git_push(vault_path: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=_NETWORK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitPushError("git push timed out: network unreachable or SSH hung.")
    if result.returncode != 0:
        if "rejected" in result.stderr or "non-fast-forward" in result.stderr:
            raise GitPushRejectedError(
                "git push rejected: remote has advanced. Retry will fetch and re-attempt."
            )
        raise GitPushError(
            f"git push failed with exit code {result.returncode}: {result.stderr.strip()}"
        )


def git_rev_parse_head(vault_path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=_LOCAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitPushError("git rev-parse HEAD timed out.")
    if result.returncode != 0:
        raise GitPushError(
            f"git rev-parse HEAD failed with exit code {result.returncode}"
        )
    return result.stdout.strip()


def git_log_hash_for_path(vault_path: Path, note_path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", str(note_path.relative_to(vault_path))],
            cwd=vault_path,
            capture_output=True,
            text=True,
            timeout=_LOCAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()
