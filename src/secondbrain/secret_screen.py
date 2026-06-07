from dataclasses import dataclass
import re


@dataclass(frozen=True)
class SecretScreenResult:
    is_sensitive: bool
    redacted_text: str
    flags: tuple[str, ...]


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 _-]*PRIVATE KEY-----.*?-----END [A-Z0-9 _-]*PRIVATE KEY-----",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    (
        "discord_bot_token",
        re.compile(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b"),
    ),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE)),
    (
        "password_assignment",
        re.compile(r"\bpassword\s*=\s*([^\s]+)", re.IGNORECASE),
    ),
    (
        "secret_assignment",
        re.compile(r"\bsecret\s*=\s*([^\s]+)", re.IGNORECASE),
    ),
    (
        "api_key_assignment",
        re.compile(r"\bapi[_-]?key\s*=\s*([^\s]+)", re.IGNORECASE),
    ),
    ("ssn_like", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
)


def screen_text(raw_text: str) -> SecretScreenResult:
    flags = tuple(name for name, pattern in _PATTERNS if pattern.search(raw_text))
    if not flags:
        return SecretScreenResult(
            is_sensitive=False,
            redacted_text=raw_text,
            flags=(),
        )

    return SecretScreenResult(
        is_sensitive=True,
        redacted_text=redact_text(raw_text),
        flags=flags,
    )


def redact_text(raw_text: str) -> str:
    redacted = raw_text
    for _name, pattern in _PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
