from __future__ import annotations

import os


class Settings:
    def __init__(self) -> None:
        self.writer_service_token: str = os.environ["WRITER_SERVICE_TOKEN"]
        self.vault_path: str = os.environ.get("VAULT_PATH", "/opt/vault")
        self.audit_log_path: str = os.environ.get(
            "AUDIT_LOG_PATH", "/opt/vault/99_log/events.ndjson"
        )
        self.log_level: str = os.environ.get("LOG_LEVEL", "INFO")
        self.git_sync_enabled: bool = (
            os.environ.get("GIT_SYNC_ENABLED", "false").lower() == "true"
        )
        self.vault_remote: str = os.environ.get("VAULT_REMOTE", "")
        self.capture_service_url: str | None = os.environ.get("CAPTURE_SERVICE_URL")
        self.capture_service_internal_token: str | None = os.environ.get(
            "CAPTURE_SERVICE_INTERNAL_TOKEN"
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
