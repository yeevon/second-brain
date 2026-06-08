import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()


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

        # Prompt version
        # self.prompt_version = os.getenv("PROMPT_VERSION")

        required = {
            "DISCORD_BOT_TOKEN": self.discord_bot_token,
            "DISCORD_GUILD_ID": self.discord_guild_id,
            "DISCORD_CAPTURE_CHANNEL_ID": self.discord_capture_channel_id,
            "DISCORD_ALLOWED_USER_ID": self.discord_allowed_user_id,
            "GEMINI_API_KEY": self.gemini_api_key,
            "GEMINI_MODEL": self.gemini_model,
            "CLASSIFICATION_CONFIDENCE_THRESHOLD": self.classification_confidence_threshold,
            "CLASSIFIER_WORKER_COUNT": self.classifier_worker_count,
            "CLASSIFIER_QUEUE_MAXSIZE": self.classifier_queue_maxsize,
            "VAULT_PATH": self.vault_path,
            "LEDGER_PATH": self.ledger_path,
            "STARTUP_RECONCILE_LIMIT": self.startup_reconcile_limit,
            "CAPTURE_SERVICE_INTERNAL_TOKEN": self.capture_service_internal_token,
            "CAPTURE_API_HOST": self.capture_api_host,
            "CAPTURE_API_PORT": self.capture_api_port,
        }
        missing = [name for name, value in required.items() if value is None or not value.strip()]
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")
        
        
        self.discord_guild_id           = int(self.discord_guild_id)
        self.discord_capture_channel_id = int(self.discord_capture_channel_id)
        self.discord_allowed_user_id    = int(self.discord_allowed_user_id)
        self.classifier_worker_count    = int(self.classifier_worker_count)
        self.classifier_queue_maxsize   = int(self.classifier_queue_maxsize)
        self.startup_reconcile_limit    = int(self.startup_reconcile_limit)
        self.capture_api_port           = int(self.capture_api_port)
        
        self.classification_confidence_threshold = float(self.classification_confidence_threshold)

        self.vault_path = Path(self.vault_path)
        self.ledger_path = Path(self.ledger_path)

        if not self.vault_path.is_absolute():
            raise RuntimeError("Vault path should be absolute path to your vault")

        if not self.capture_service_internal_token.strip():
            raise RuntimeError("Capture service internal token is required")

        if len(self.capture_service_internal_token.strip()) < 32:
            raise RuntimeError("Capture service internal token must be at least 32 characters")

        if not (1 <= self.capture_api_port <= 65535):
            raise RuntimeError("Capture API port must be between 1 and 65535")
