from types import SimpleNamespace

from secondbrain.discord_capture import should_capture_message


def make_settings():
    return SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
    )


def make_message(**overrides):
    data = {
        "guild": SimpleNamespace(id=100),
        "channel": SimpleNamespace(id=200),
        "author": SimpleNamespace(id=300, bot=False),
        "webhook_id": None,
        "content": "capture this",
        "attachments": [],
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_allowlisted_message_passes():
    assert should_capture_message(make_message(), make_settings()) is True


def test_wrong_user_is_ignored():
    assert should_capture_message(
        make_message(author=SimpleNamespace(id=999, bot=False)), make_settings()
    ) is False


def test_wrong_guild_is_ignored():
    assert should_capture_message(make_message(guild=SimpleNamespace(id=999)), make_settings()) is False


def test_wrong_channel_is_ignored():
    assert should_capture_message(make_message(channel=SimpleNamespace(id=999)), make_settings()) is False


def test_bot_authored_message_is_ignored():
    assert should_capture_message(
        make_message(author=SimpleNamespace(id=300, bot=True)), make_settings()
    ) is False


def test_webhook_authored_message_is_ignored():
    assert should_capture_message(make_message(webhook_id=123), make_settings()) is False
