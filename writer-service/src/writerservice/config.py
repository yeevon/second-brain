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


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
