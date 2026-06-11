import os
from dotenv import load_dotenv
from pathlib import Path
from typing import Literal

load_dotenv()


CaptureProcessingMode = Literal["local-full", "capture-only"]
SUPPORTED_CAPTURE_PROCESSING_MODES = {"local-full", "capture-only"}


class Settings:
    def __init__(self) -> None:
        # Discord
        self.discord_bot_token          = os.getenv("DISCORD_BOT_TOKEN")
        self.discord_guild_id           = os.getenv("DISCORD_GUILD_ID")
        self.discord_capture_channel_id = os.getenv("DISCORD_CAPTURE_CHANNEL_ID")
        self.discord_allowed_user_id    = os.getenv("DISCORD_ALLOWED_USER_ID")

        # Gemini
        self.gemini_api_key                      = os.getenv("GEMINI_API_KEY")
        self.gemini_model                        = os.getenv("GEMINI_MODEL")
        self.classification_confidence_threshold = os.getenv("CLASSIFICATION_CONFIDENCE_THRESHOLD")
        self.classifier_worker_count             = os.getenv("CLASSIFIER_WORKER_COUNT")
        self.classifier_queue_maxsize            = os.getenv("CLASSIFIER_QUEUE_MAXSIZE")

        # Github vault sync
        # self.github_token       = os.getenv("GITHUB_TOKEN")
        # self.github_vault_repo  = os.getenv("GITHUB_VAULT_REPO")
        # self.github_vault_branch = os.getenv("GITHUB_VAULT_BRANCH")

        # n8n
        # self.n8n_webhook_url = os.getenv("N8N_WEBHOOK_URL")

        # Vault
        self.vault_path              = os.getenv("VAULT_PATH")
        self.ledger_path             = os.getenv("LEDGER_PATH")
        self.startup_reconcile_limit = os.getenv("STARTUP_RECONCILE_LIMIT")

        # Internal capture API
        self.capture_service_internal_token = os.getenv("CAPTURE_SERVICE_INTERNAL_TOKEN")
        self.capture_api_host               = os.getenv("CAPTURE_API_HOST")
        self.capture_api_port               = os.getenv("CAPTURE_API_PORT")

        # Runtime
        self.capture_processing_mode = os.getenv("CAPTURE_PROCESSING_MODE")

        # SQLite runtime
        self.sqlite_busy_timeout_ms = _parse_int_env("SQLITE_BUSY_TIMEOUT_MS", "1000")
        self.sqlite_busy_retry_attempts = _parse_int_env("SQLITE_BUSY_RETRY_ATTEMPTS", "5")
        self.sqlite_busy_retry_base_delay_ms = _parse_int_env("SQLITE_BUSY_RETRY_BASE_DELAY_MS", "25")
        self.sqlite_job_queue_maxsize = _parse_int_env("SQLITE_JOB_QUEUE_MAXSIZE", "10000")

        # Prompt version
        # self.prompt_version = os.getenv("PROMPT_VERSION")

        required = {
            "CAPTURE_PROCESSING_MODE": self.capture_processing_mode,
            "DISCORD_BOT_TOKEN": self.discord_bot_token,
            "DISCORD_GUILD_ID": self.discord_guild_id,
            "DISCORD_CAPTURE_CHANNEL_ID": self.discord_capture_channel_id,
            "DISCORD_ALLOWED_USER_ID": self.discord_allowed_user_id,
            "LEDGER_PATH": self.ledger_path,
            "STARTUP_RECONCILE_LIMIT": self.startup_reconcile_limit,
            "CAPTURE_SERVICE_INTERNAL_TOKEN": self.capture_service_internal_token,
            "CAPTURE_API_HOST": self.capture_api_host,
            "CAPTURE_API_PORT": self.capture_api_port,
        }
        if self.capture_processing_mode == "local-full":
            required.update(
                {
                    "GEMINI_API_KEY": self.gemini_api_key,
                    "GEMINI_MODEL": self.gemini_model,
                    "CLASSIFICATION_CONFIDENCE_THRESHOLD": self.classification_confidence_threshold,
                    "CLASSIFIER_WORKER_COUNT": self.classifier_worker_count,
                    "CLASSIFIER_QUEUE_MAXSIZE": self.classifier_queue_maxsize,
                    "VAULT_PATH": self.vault_path,
                }
            )
        missing = [name for name, value in required.items() if value is None or not value.strip()]
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")

        if self.capture_processing_mode not in SUPPORTED_CAPTURE_PROCESSING_MODES:
            raise RuntimeError(f"Unsupported capture processing mode: {self.capture_processing_mode}")
        
        
        self.discord_guild_id           = int(self.discord_guild_id)
        self.discord_capture_channel_id = int(self.discord_capture_channel_id)
        self.discord_allowed_user_id    = int(self.discord_allowed_user_id)
        self.startup_reconcile_limit          = int(self.startup_reconcile_limit)
        self.periodic_reconcile_interval_seconds = _parse_int_env("PERIODIC_RECONCILE_INTERVAL_SECONDS", "60")
        self.periodic_reconcile_limit            = _parse_int_env("PERIODIC_RECONCILE_LIMIT", "100")
        self.capture_api_port           = int(self.capture_api_port)

        # Delivery dispatcher and reaper
        # DELIVERY_RETRY_MAX_ATTEMPTS is the canonical name; DELIVERY_MAX_ATTEMPTS is a legacy alias
        self.delivery_retry_max_attempts         = _parse_int_env("DELIVERY_RETRY_MAX_ATTEMPTS",
                                                       os.getenv("DELIVERY_MAX_ATTEMPTS", "5"))
        self.delivery_retry_base_delay_seconds   = _parse_int_env("DELIVERY_RETRY_BASE_DELAY_SECONDS", "10")
        self.delivery_retry_max_delay_seconds    = _parse_int_env("DELIVERY_RETRY_MAX_DELAY_SECONDS", "300")
        self.delivery_forward_lease_seconds      = _parse_int_env("DELIVERY_FORWARD_LEASE_SECONDS", "60")
        self.delivery_processing_lease_seconds   = _parse_int_env("DELIVERY_PROCESSING_LEASE_SECONDS", "300")
        self.delivery_dispatch_interval_seconds  = _parse_int_env("DELIVERY_DISPATCH_INTERVAL_SECONDS", "2")
        self.delivery_dispatch_batch_size        = _parse_int_env("DELIVERY_DISPATCH_BATCH_SIZE", "25")
        self.stale_lease_reaper_interval_seconds = _parse_int_env("STALE_LEASE_REAPER_INTERVAL_SECONDS", "30")
        self.stale_lease_reaper_batch_size       = _parse_int_env("STALE_LEASE_REAPER_BATCH_SIZE", "100")

        # Heartbeat
        self.capture_service_heartbeat_interval_seconds  = _parse_int_env("CAPTURE_SERVICE_HEARTBEAT_INTERVAL_SECONDS", "15")
        self.capture_service_health_stale_after_seconds  = _parse_int_env("CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS", "60")

        # Status command
        self.status_timezone = os.getenv("STATUS_TIMEZONE", "UTC")

        self.ledger_path = Path(self.ledger_path)

        if self.capture_processing_mode == "local-full":
            self.classifier_worker_count = int(self.classifier_worker_count)
            self.classifier_queue_maxsize = int(self.classifier_queue_maxsize)
            self.classification_confidence_threshold = float(self.classification_confidence_threshold)
            self.vault_path = Path(self.vault_path)

            if not self.vault_path.is_absolute():
                raise RuntimeError("Vault path should be absolute path to your vault")
        else:
            self.classifier_worker_count = None
            self.classifier_queue_maxsize = None
            self.classification_confidence_threshold = None
            self.vault_path = None

        if not self.capture_service_internal_token.strip():
            raise RuntimeError("Capture service internal token is required")

        if len(self.capture_service_internal_token.strip()) < 32:
            raise RuntimeError("Capture service internal token must be at least 32 characters")

        if not (1 <= self.capture_api_port <= 65535):
            raise RuntimeError("Capture API port must be between 1 and 65535")

        if self.periodic_reconcile_interval_seconds < 1:
            raise RuntimeError("PERIODIC_RECONCILE_INTERVAL_SECONDS must be >= 1")
        if self.periodic_reconcile_limit < 1:
            raise RuntimeError("PERIODIC_RECONCILE_LIMIT must be >= 1")

        if self.delivery_retry_max_attempts < 1:
            raise RuntimeError("DELIVERY_RETRY_MAX_ATTEMPTS must be >= 1")
        if self.delivery_retry_base_delay_seconds < 1:
            raise RuntimeError("DELIVERY_RETRY_BASE_DELAY_SECONDS must be >= 1")
        if self.delivery_retry_max_delay_seconds < self.delivery_retry_base_delay_seconds:
            raise RuntimeError("DELIVERY_RETRY_MAX_DELAY_SECONDS must be >= DELIVERY_RETRY_BASE_DELAY_SECONDS")
        if self.delivery_forward_lease_seconds < 1:
            raise RuntimeError("DELIVERY_FORWARD_LEASE_SECONDS must be >= 1")
        if self.delivery_processing_lease_seconds < self.delivery_forward_lease_seconds:
            raise RuntimeError("DELIVERY_PROCESSING_LEASE_SECONDS must be >= DELIVERY_FORWARD_LEASE_SECONDS")
        if self.delivery_dispatch_interval_seconds < 1:
            raise RuntimeError("DELIVERY_DISPATCH_INTERVAL_SECONDS must be >= 1")
        if self.delivery_dispatch_batch_size < 1:
            raise RuntimeError("DELIVERY_DISPATCH_BATCH_SIZE must be >= 1")
        if self.stale_lease_reaper_interval_seconds < 1:
            raise RuntimeError("STALE_LEASE_REAPER_INTERVAL_SECONDS must be >= 1")
        if self.stale_lease_reaper_batch_size < 1:
            raise RuntimeError("STALE_LEASE_REAPER_BATCH_SIZE must be >= 1")

        if self.capture_service_heartbeat_interval_seconds < 1:
            raise RuntimeError("CAPTURE_SERVICE_HEARTBEAT_INTERVAL_SECONDS must be >= 1")
        if self.capture_service_health_stale_after_seconds <= self.capture_service_heartbeat_interval_seconds:
            raise RuntimeError(
                "CAPTURE_SERVICE_HEALTH_STALE_AFTER_SECONDS must be > CAPTURE_SERVICE_HEARTBEAT_INTERVAL_SECONDS"
            )

        if self.sqlite_busy_timeout_ms < 0:
            raise RuntimeError("SQLITE_BUSY_TIMEOUT_MS must be >= 0")
        if self.sqlite_busy_retry_attempts < 1:
            raise RuntimeError("SQLITE_BUSY_RETRY_ATTEMPTS must be >= 1")
        if self.sqlite_busy_retry_base_delay_ms < 0:
            raise RuntimeError("SQLITE_BUSY_RETRY_BASE_DELAY_MS must be >= 0")
        if self.sqlite_job_queue_maxsize < 1:
            raise RuntimeError("SQLITE_JOB_QUEUE_MAXSIZE must be >= 1")


def _parse_int_env(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{name} must be an integer, got: {raw!r}") from exc
