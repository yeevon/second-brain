from pathlib import Path
import tomllib

import pytest

from secondbrain.config import Settings


REQUIRED_ENV = {
    "CAPTURE_PROCESSING_MODE": "local-full",
    "DISCORD_BOT_TOKEN": "discord-token",
    "DISCORD_GUILD_ID": "100",
    "DISCORD_CAPTURE_CHANNEL_ID": "200",
    "DISCORD_ALLOWED_USER_ID": "300",
    "GEMINI_API_KEY": "gemini-key",
    "GEMINI_MODEL": "gemini-test",
    "CLASSIFICATION_CONFIDENCE_THRESHOLD": "0.75",
    "CLASSIFIER_WORKER_COUNT": "1",
    "CLASSIFIER_QUEUE_MAXSIZE": "100",
    "VAULT_PATH": "/tmp/second-brain-test-vault",
    "LEDGER_PATH": ".runtime/ledger.sqlite3",
    "STARTUP_RECONCILE_LIMIT": "100",
    "CAPTURE_SERVICE_INTERNAL_TOKEN": "x" * 32,
    "CAPTURE_API_HOST": "127.0.0.1",
    "CAPTURE_API_PORT": "8000",
}


def test_settings_reports_named_missing_configuration(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    monkeypatch.delenv("CAPTURE_SERVICE_INTERNAL_TOKEN")

    with pytest.raises(RuntimeError) as exc_info:
        Settings()

    assert str(exc_info.value) == (
        "Missing required configuration: DISCORD_BOT_TOKEN, CAPTURE_SERVICE_INTERNAL_TOKEN"
    )


def test_settings_rejects_short_internal_token(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("CAPTURE_SERVICE_INTERNAL_TOKEN", "too-short")

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        Settings()


def test_project_exposes_secondbrain_console_script():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert pyproject["project"]["scripts"]["secondbrain"] == "secondbrain.app:main"


def _set_required_env(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
