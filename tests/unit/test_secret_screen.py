import pytest

from secondbrain.secret_screen import screen_text


def test_clean_text_is_not_sensitive_and_is_preserved():
    result = screen_text("Review reconnect handling in the telemetry dashboard.")

    assert result.is_sensitive is False
    assert result.redacted_text == "Review reconnect handling in the telemetry dashboard."
    assert result.flags == ()


@pytest.mark.parametrize(
    ("raw_text", "expected_flag", "secret_fragment"),
    [
        (
            "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
            "private_key",
            "abc123",
        ),
        ("AWS key AKIAABCDEFGHIJKLMNOP", "aws_access_key", "AKIAABCDEFGHIJKLMNOP"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz123456", "github_token", "ghp_"),
        (
            "discord token abcdefghijklmnopqrstuvwx.abcdef.ABCDEFGHIJKLMNOPQRSTUVWXYZabc",
            "discord_bot_token",
            "abcdefghijklmnopqrstuvwx.abcdef",
        ),
        ("auth Bearer supersecrettokenvalue", "bearer_token", "supersecrettokenvalue"),
        ("password=hunter2", "password_assignment", "hunter2"),
        ("secret=topsecret", "secret_assignment", "topsecret"),
        ("api_key=abc123456789", "api_key_assignment", "abc123456789"),
        ("ssn 123-45-6789", "ssn_like", "123-45-6789"),
    ],
)
def test_secret_patterns_are_flagged_and_redacted(raw_text, expected_flag, secret_fragment):
    result = screen_text(raw_text)

    assert result.is_sensitive is True
    assert expected_flag in result.flags
    assert secret_fragment not in result.redacted_text
    assert "[REDACTED]" in result.redacted_text


def test_multiple_secret_flags_can_be_returned():
    result = screen_text("password=hunter2 api_key=abc123456789")

    assert result.is_sensitive is True
    assert "password_assignment" in result.flags
    assert "api_key_assignment" in result.flags
    assert "hunter2" not in result.redacted_text
    assert "abc123456789" not in result.redacted_text


def test_assignment_redaction_preserves_key_name_only():
    result = screen_text("password=hunter2 secret=topsecret api_key=abc123456789")

    assert result.redacted_text == (
        "password=[REDACTED] secret=[REDACTED] api_key=[REDACTED]"
    )
