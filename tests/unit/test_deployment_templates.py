"""Verify that env-template key names stay in sync with Settings attributes."""
import re
from pathlib import Path

import pytest

from secondbrain.config import Settings


_LOCAL_FULL_ENV = {
    "CAPTURE_PROCESSING_MODE": "local-full",
    "DISCORD_BOT_TOKEN": "discord-token",
    "DISCORD_GUILD_ID": "100",
    "DISCORD_CAPTURE_CHANNEL_ID": "200",
    "DISCORD_ALLOWED_USER_ID": "300",
    "LEDGER_PATH": ".runtime/ledger.sqlite3",
    "STARTUP_RECONCILE_LIMIT": "100",
    "CAPTURE_SERVICE_INTERNAL_TOKEN": "x" * 32,
    "CAPTURE_API_HOST": "127.0.0.1",
    "CAPTURE_API_PORT": "8000",
    "GEMINI_API_KEY": "gemini-key",
    "GEMINI_MODEL": "gemini-test",
    "CLASSIFICATION_CONFIDENCE_THRESHOLD": "0.75",
    "CLASSIFIER_WORKER_COUNT": "1",
    "CLASSIFIER_QUEUE_MAXSIZE": "100",
    "VAULT_PATH": "/tmp/vault",
}

_CAPTURE_ONLY_ENV = {
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


def _parse_template_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match:
            keys.append(match.group(1))
    return keys


def _make_settings(monkeypatch, base_env: dict) -> Settings:
    for key in set(_LOCAL_FULL_ENV) | set(_CAPTURE_ONLY_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in base_env.items():
        monkeypatch.setenv(key, value)
    return Settings()


_COMPOSE_ONLY_KEYS = {
    # Local Docker Compose bind-mount and init variables, not consumed by Settings
    "LOCAL_VAULT_PATH",
    "LOCAL_UID",
    "LOCAL_GID",
    "GIT_SYNC_ENABLED",
    "VAULT_DEPLOY_KEY_FILE",
    "GITHUB_KNOWN_HOSTS_FILE",
}


@pytest.mark.parametrize("template,base_env", [
    (".env.example", _LOCAL_FULL_ENV),
    ("deploy/capture-service.env.example", _CAPTURE_ONLY_ENV),
])
def test_template_keys_map_to_settings_attributes(monkeypatch, template, base_env):
    path = Path(template)
    keys = _parse_template_keys(path)
    settings = _make_settings(monkeypatch, base_env)

    unknown_keys = [
        k for k in keys
        if k not in _COMPOSE_ONLY_KEYS and not hasattr(settings, k.lower())
    ]

    assert unknown_keys == [], (
        f"{template} contains key(s) with no matching Settings attribute: "
        + ", ".join(unknown_keys)
    )
