"""SB-122: Pull-only Obsidian vault sync wrapper.

Performs git fetch + ff-only merge, fails visibly on dirty tree or conflict.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class VaultPullError(Exception):
    """Raised when vault pull fails for a specific reason."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def pull_vault(vault_path: Path) -> None:
    """Execute pull-only sync for the Obsidian vault.

    Steps:
      1. Verify vault path exists.
      2. Check for dirty worktree — fail if dirty.
      3. git fetch origin.
      4. git merge --ff-only origin/main — fail on diverged history or conflict.

    Raises:
        VaultPullError: with a descriptive message and appropriate exit code.
    """
    if not vault_path.exists():
        raise VaultPullError(
            f"vault path does not exist: {vault_path}",
            exit_code=1,
        )

    _run_git(["git", "status", "--porcelain"], vault_path, step="check worktree")
    # _run_git already raises on non-zero; check output for dirty state
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=vault_path,
        capture_output=True,
        text=True,
    )
    if status_result.stdout.strip():
        raise VaultPullError(
            f"dirty working tree — refusing to pull:\n{status_result.stdout.rstrip()}",
            exit_code=2,
        )

    _run_git(["git", "fetch", "origin"], vault_path, step="fetch")
    _run_git(["git", "merge", "--ff-only", "origin/main"], vault_path, step="merge")


def _run_git(cmd: list[str], cwd: Path, *, step: str) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise VaultPullError(
            f"git {step} failed (exit {result.returncode}):\n{detail}",
            exit_code=result.returncode,
        )
    return result


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv()

    vault_path_str = os.getenv("VAULT_PATH", "").strip()
    if not vault_path_str:
        print("ERROR: VAULT_PATH is required", file=sys.stderr)
        return 1

    vault_path = Path(vault_path_str)
    try:
        pull_vault(vault_path)
        print(f"Vault synced successfully: {vault_path}")
        return 0
    except VaultPullError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
