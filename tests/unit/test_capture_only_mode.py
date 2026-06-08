from types import SimpleNamespace

import pytest

from secondbrain.config import Settings
from secondbrain.receipts import ATTACHMENT_WARNING, format_saved_receipt


BASE_ENV = {
    "CAPTURE_PROCESSING_MODE": "capture-only",
    "DISCORD_BOT_TOKEN": "discord-token",
    "DISCORD_GUILD_ID": "100",
    "DISCORD_CAPTURE_CHANNEL_ID": "200",
    "DISCORD_ALLOWED_USER_ID": "300",
    "LEDGER_PATH": ".runtime/ledger.sqlite3",
    "STARTUP_RECONCILE_LIMIT": "100",
    "CAPTURE_SERVICE_INTERNAL_TOKEN": "x" * 32,
    "CAPTURE_API_HOST": "127.0.0.1",
    "CAPTURE_API_PORT": "8000",
}


LOCAL_FULL_ENV = {
    **BASE_ENV,
    "CAPTURE_PROCESSING_MODE": "local-full",
    "GEMINI_API_KEY": "gemini-key",
    "GEMINI_MODEL": "gemini-test",
    "CLASSIFICATION_CONFIDENCE_THRESHOLD": "0.75",
    "CLASSIFIER_WORKER_COUNT": "1",
    "CLASSIFIER_QUEUE_MAXSIZE": "100",
    "VAULT_PATH": "/tmp/second-brain-test-vault",
}


def test_capture_only_mode_does_not_require_gemini_api_key(monkeypatch):
    _set_env(monkeypatch, BASE_ENV)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    settings = Settings()

    assert settings.capture_processing_mode == "capture-only"
    assert settings.gemini_api_key is None
    assert settings.gemini_model is None


def test_capture_only_mode_does_not_require_vault_path(monkeypatch):
    _set_env(monkeypatch, BASE_ENV)
    monkeypatch.delenv("VAULT_PATH", raising=False)

    settings = Settings()

    assert settings.capture_processing_mode == "capture-only"
    assert settings.vault_path is None


def test_local_full_mode_requires_gemini_api_key(monkeypatch):
    _set_env(monkeypatch, LOCAL_FULL_ENV)
    monkeypatch.delenv("GEMINI_API_KEY")

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        Settings()


def test_local_full_mode_requires_vault_path(monkeypatch):
    _set_env(monkeypatch, LOCAL_FULL_ENV)
    monkeypatch.delenv("VAULT_PATH")

    with pytest.raises(RuntimeError, match="VAULT_PATH"):
        Settings()


def test_unknown_processing_mode_is_rejected(monkeypatch):
    _set_env(monkeypatch, BASE_ENV)
    monkeypatch.setenv("CAPTURE_PROCESSING_MODE", "sometimes-local")

    with pytest.raises(RuntimeError, match="Unsupported capture processing mode: sometimes-local"):
        Settings()


def test_capture_only_receipt_does_not_claim_processing():
    capture = SimpleNamespace(capture_id="SB-20260608-0001")

    content = format_saved_receipt(
        capture,
        has_attachments=False,
        downstream_processing_enabled=False,
    )

    assert content == (
        "⏳ SB-20260608-0001 received.\n"
        "Your note is safely captured.\n"
        "Downstream filing is not enabled yet."
    )


def test_local_full_receipt_preserves_processing_message():
    capture = SimpleNamespace(capture_id="SB-20260608-0001")

    content = format_saved_receipt(
        capture,
        has_attachments=False,
        downstream_processing_enabled=True,
    )

    assert content == "⏳ SB-20260608-0001 received.\nYour note is saved. Processing…"


def test_capture_only_receipt_preserves_attachment_warning():
    capture = SimpleNamespace(capture_id="SB-20260608-0001")

    content = format_saved_receipt(
        capture,
        has_attachments=True,
        downstream_processing_enabled=False,
    )

    assert content.endswith(ATTACHMENT_WARNING)


def _set_env(monkeypatch, env):
    for key in set(BASE_ENV) | set(LOCAL_FULL_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
