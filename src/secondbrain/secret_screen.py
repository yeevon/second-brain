from collections.abc import Callable
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class SecretScreenResult:
    is_sensitive: bool
    redacted_text: str
    flags: tuple[str, ...]


Replacement = str | Callable[[re.Match[str]], str]


_PATTERNS: tuple[tuple[str, re.Pattern[str], Replacement], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 _-]*PRIVATE KEY-----.*?-----END [A-Z0-9 _-]*PRIVATE KEY-----",
            re.IGNORECASE | re.DOTALL,
        ),
        "[REDACTED]",
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED]"),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"), "[REDACTED]"),
    (
        "discord_bot_token",
        re.compile(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b"),
        "[REDACTED]",
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
        "Bearer [REDACTED]",
    ),
    (
        "password_assignment",
        re.compile(r"\b(password)\s*=\s*([^\s]+)", re.IGNORECASE),
        lambda match: f"{match.group(1)}=[REDACTED]",
    ),
    (
        "secret_assignment",
        re.compile(r"\b(secret)\s*=\s*([^\s]+)", re.IGNORECASE),
        lambda match: f"{match.group(1)}=[REDACTED]",
    ),
    (
        "api_key_assignment",
        re.compile(r"\b(api[_-]?key)\s*=\s*([^\s]+)", re.IGNORECASE),
        lambda match: f"{match.group(1)}=[REDACTED]",
    ),
    ("ssn_like", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED]"),
)


def screen_text(raw_text: str) -> SecretScreenResult:
    flags = tuple(name for name, pattern, _replacement in _PATTERNS if pattern.search(raw_text))
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
    for _name, pattern, replacement in _PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
