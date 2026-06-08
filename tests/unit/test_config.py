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


# ---------------------------------------------------------------------------
# SQLite runtime settings
# ---------------------------------------------------------------------------

def test_sqlite_runtime_settings_use_safe_defaults(monkeypatch):
    _set_required_env(monkeypatch)
    # Do not set any SQLITE_* vars so defaults apply
    for key in (
        "SQLITE_BUSY_TIMEOUT_MS",
        "SQLITE_BUSY_RETRY_ATTEMPTS",
        "SQLITE_BUSY_RETRY_BASE_DELAY_MS",
        "SQLITE_JOB_QUEUE_MAXSIZE",
    ):
        monkeypatch.delenv(key, raising=False)

    s = Settings()
    assert s.sqlite_busy_timeout_ms == 1000
    assert s.sqlite_busy_retry_attempts == 5
    assert s.sqlite_busy_retry_base_delay_ms == 25
    assert s.sqlite_job_queue_maxsize == 10000


def test_sqlite_runtime_settings_accept_valid_overrides(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "500")
    monkeypatch.setenv("SQLITE_BUSY_RETRY_ATTEMPTS", "10")
    monkeypatch.setenv("SQLITE_BUSY_RETRY_BASE_DELAY_MS", "50")
    monkeypatch.setenv("SQLITE_JOB_QUEUE_MAXSIZE", "5000")

    s = Settings()
    assert s.sqlite_busy_timeout_ms == 500
    assert s.sqlite_busy_retry_attempts == 10
    assert s.sqlite_busy_retry_base_delay_ms == 50
    assert s.sqlite_job_queue_maxsize == 5000


def test_sqlite_busy_timeout_rejects_negative_value(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "-1")

    with pytest.raises(RuntimeError, match="SQLITE_BUSY_TIMEOUT_MS"):
        Settings()


def test_sqlite_retry_attempts_rejects_zero(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SQLITE_BUSY_RETRY_ATTEMPTS", "0")

    with pytest.raises(RuntimeError, match="SQLITE_BUSY_RETRY_ATTEMPTS"):
        Settings()


def test_sqlite_retry_delay_rejects_negative_value(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SQLITE_BUSY_RETRY_BASE_DELAY_MS", "-5")

    with pytest.raises(RuntimeError, match="SQLITE_BUSY_RETRY_BASE_DELAY_MS"):
        Settings()


def test_sqlite_queue_maxsize_rejects_zero(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SQLITE_JOB_QUEUE_MAXSIZE", "0")

    with pytest.raises(RuntimeError, match="SQLITE_JOB_QUEUE_MAXSIZE"):
        Settings()


def test_sqlite_runtime_settings_reject_non_numeric_values_cleanly(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "not-a-number")

    with pytest.raises(RuntimeError, match="SQLITE_BUSY_TIMEOUT_MS"):
        Settings()


def test_periodic_reconcile_settings_use_safe_defaults(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("PERIODIC_RECONCILE_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("PERIODIC_RECONCILE_LIMIT", raising=False)

    s = Settings()
    assert s.periodic_reconcile_interval_seconds == 60
    assert s.periodic_reconcile_limit == 100


def test_periodic_reconcile_settings_accept_valid_overrides(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PERIODIC_RECONCILE_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("PERIODIC_RECONCILE_LIMIT", "50")

    s = Settings()
    assert s.periodic_reconcile_interval_seconds == 120
    assert s.periodic_reconcile_limit == 50


def test_periodic_reconcile_interval_rejects_zero(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PERIODIC_RECONCILE_INTERVAL_SECONDS", "0")

    with pytest.raises(RuntimeError, match="PERIODIC_RECONCILE_INTERVAL_SECONDS"):
        Settings()


def test_periodic_reconcile_limit_rejects_zero(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PERIODIC_RECONCILE_LIMIT", "0")

    with pytest.raises(RuntimeError, match="PERIODIC_RECONCILE_LIMIT"):
        Settings()


def test_periodic_reconcile_settings_reject_non_numeric_values_cleanly(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PERIODIC_RECONCILE_INTERVAL_SECONDS", "not-a-number")

    with pytest.raises(RuntimeError, match="PERIODIC_RECONCILE_INTERVAL_SECONDS"):
        Settings()
