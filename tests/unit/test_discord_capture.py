from types import SimpleNamespace

import pytest

from secondbrain.discord_capture import create_discord_client, should_capture_message


def make_settings():
    return SimpleNamespace(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
    )


def make_message(
    *,
    guild_id=100,
    channel_id=200,
    author_id=300,
    author_bot=False,
    webhook_id=None,
    content="capture this",
):
    guild = None if guild_id is None else SimpleNamespace(id=guild_id)
    return SimpleNamespace(
        guild=guild,
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=author_id, bot=author_bot),
        webhook_id=webhook_id,
        content=content,
    )


def test_should_capture_allowlisted_text_message():
    assert should_capture_message(make_message(), make_settings()) is True


@pytest.mark.parametrize(
    "message",
    [
        make_message(guild_id=None),
        make_message(guild_id=999),
        make_message(channel_id=999),
        make_message(author_id=999),
        make_message(author_bot=True),
        make_message(webhook_id=123),
        make_message(content=""),
        make_message(content="   "),
    ],
)
def test_should_ignore_messages_that_fail_required_filters(message):
    assert should_capture_message(message, make_settings()) is False


@pytest.mark.asyncio
async def test_client_hands_filtered_message_to_capture_handler():
    settings = make_settings()
    captured = []

    async def handle_capture(message):
        captured.append(message)

    client = create_discord_client(settings, handle_capture)
    await client.on_message(make_message())

    assert len(captured) == 1


@pytest.mark.asyncio
async def test_client_does_not_hand_ignored_message_to_capture_handler():
    settings = make_settings()
    captured = []

    async def handle_capture(message):
        captured.append(message)

    client = create_discord_client(settings, handle_capture)
    await client.on_message(make_message(author_id=999))

    assert captured == []
