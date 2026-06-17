from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv


class PreflightCheck(NamedTuple):
    name: str
    passed: bool
    detail: str


def run_preflight() -> list[PreflightCheck]:
    """Run mode-aware configuration checks. Never starts Discord or makes network calls."""
    load_dotenv()
    checks: list[PreflightCheck] = []

    mode = (os.environ.get("CAPTURE_PROCESSING_MODE") or "").strip()
    checks.append(_check_present("CAPTURE_PROCESSING_MODE", mode))
    if mode not in {"local-full", "capture-only"}:
        checks.append(PreflightCheck(
            name="CAPTURE_PROCESSING_MODE value",
            passed=False,
            detail=f"must be 'local-full' or 'capture-only', got {mode!r}",
        ))
        return checks

    # Common checks (both modes)
    discord_token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    checks.append(_check_discord_token(discord_token))

    ledger_path_str = (os.environ.get("LEDGER_PATH") or "").strip()
    ledger_check, ledger_path = _check_ledger_path(ledger_path_str)
    checks.append(ledger_check)
    if ledger_path is not None:
        checks.append(_check_sqlite_openable(ledger_path))

    # Mode-specific checks
    if mode == "local-full":
        checks.extend(_local_full_checks())
    else:
        checks.extend(_capture_only_checks())

    return checks


def _check_present(name: str, value: str) -> PreflightCheck:
    passed = bool(value)
    return PreflightCheck(name=name, passed=passed, detail="set" if passed else "missing")


def _check_discord_token(token: str) -> PreflightCheck:
    if not token:
        return PreflightCheck("DISCORD_BOT_TOKEN", False, "missing")
    # Basic structural check: Discord bot tokens have at least two dots
    if token.count(".") < 2:
        return PreflightCheck("DISCORD_BOT_TOKEN", False, "set but does not look like a valid bot token")
    return PreflightCheck("DISCORD_BOT_TOKEN", True, "set")


def _check_ledger_path(path_str: str) -> tuple[PreflightCheck, Path | None]:
    if not path_str:
        return PreflightCheck("LEDGER_PATH", False, "missing"), None
    ledger_path = Path(path_str)
    parent = ledger_path.parent
    if not parent.exists():
        return PreflightCheck("LEDGER_PATH", False, f"{parent} directory does not exist"), None
    if not os.access(parent, os.W_OK):
        return PreflightCheck("LEDGER_PATH", False, f"{parent} directory is not writable"), None
    return PreflightCheck("LEDGER_PATH", True, f"{ledger_path} (directory writable)"), ledger_path


def _check_sqlite_openable(ledger_path: Path) -> PreflightCheck:
    try:
        conn = sqlite3.connect(str(ledger_path))
        conn.close()
        return PreflightCheck("SQLite open", True, str(ledger_path))
    except Exception as exc:
        return PreflightCheck("SQLite open", False, f"{type(exc).__name__}: cannot open database")


def _local_full_checks() -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []

    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    checks.append(_check_present("GEMINI_API_KEY", gemini_key))

    gemini_model = (os.environ.get("GEMINI_MODEL") or "").strip()
    if not gemini_model:
        checks.append(PreflightCheck("GEMINI_MODEL", False, "missing"))
    elif gemini_model.endswith("-latest") or gemini_model.endswith("-preview"):
        checks.append(PreflightCheck(
            "GEMINI_MODEL",
            False,
            f"{gemini_model} looks like a floating alias — use a pinned model string",
        ))
    else:
        checks.append(PreflightCheck("GEMINI_MODEL", True, f"{gemini_model} (pinned)"))

    vault_path_str = (os.environ.get("VAULT_PATH") or "").strip()
    if not vault_path_str:
        checks.append(PreflightCheck("VAULT_PATH", False, "missing"))
    else:
        vault_path = Path(vault_path_str)
        if vault_path.is_dir():
            checks.append(PreflightCheck("VAULT_PATH", True, f"{vault_path} (exists)"))
        else:
            checks.append(PreflightCheck("VAULT_PATH", False, f"{vault_path} does not exist or is not a directory"))

    return checks


def _capture_only_checks() -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []

    internal_token = (os.environ.get("CAPTURE_SERVICE_INTERNAL_TOKEN") or "").strip()
    checks.append(_check_present("CAPTURE_SERVICE_INTERNAL_TOKEN", internal_token))

    downstream_enabled = (os.environ.get("DOWNSTREAM_DELIVERY_ENABLED") or "").strip().lower() == "true"
    if downstream_enabled:
        webhook_url = (os.environ.get("N8N_INTAKE_WEBHOOK_URL") or "").strip()
        if not webhook_url:
            checks.append(PreflightCheck("N8N_INTAKE_WEBHOOK_URL", False, "missing (required when DOWNSTREAM_DELIVERY_ENABLED=true)"))
        elif not (webhook_url.startswith("http://") or webhook_url.startswith("https://")):
            checks.append(PreflightCheck("N8N_INTAKE_WEBHOOK_URL", False, "does not look like a valid URL"))
        else:
            checks.append(PreflightCheck("N8N_INTAKE_WEBHOOK_URL", True, "set"))

        webhook_token = (os.environ.get("N8N_INTAKE_WEBHOOK_TOKEN") or "").strip()
        checks.append(_check_present("N8N_INTAKE_WEBHOOK_TOKEN", webhook_token))

    return checks


def format_preflight_results(checks: list[PreflightCheck]) -> tuple[str, bool]:
    lines = []
    all_passed = True
    for check in checks:
        symbol = "✓" if check.passed else "✗"
        lines.append(f"  {symbol} {check.name:<35} — {check.detail}")
        if not check.passed:
            all_passed = False
    return "\n".join(lines), all_passed
