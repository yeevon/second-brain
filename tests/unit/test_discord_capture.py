from types import SimpleNamespace

import pytest

from secondbrain.discord_capture import (
    create_discord_client,
    extract_attachment_metadata,
    should_capture_message,
)


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
    attachments=None,
):
    guild = None if guild_id is None else SimpleNamespace(id=guild_id)
    return SimpleNamespace(
        guild=guild,
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=author_id, bot=author_bot),
        webhook_id=webhook_id,
        content=content,
        attachments=attachments or [],
    )


def test_should_capture_allowlisted_text_message():
    assert should_capture_message(make_message(), make_settings()) is True


def test_should_capture_text_message_with_attachment():
    message = make_message(attachments=[make_attachment()])

    assert should_capture_message(message, make_settings()) is True


def test_should_capture_attachment_only_message():
    message = make_message(content="", attachments=[make_attachment()])

    assert should_capture_message(message, make_settings()) is True


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


def test_extract_attachment_metadata_from_discord_message():
    message = make_message(
        attachments=[
            make_attachment(
                filename="bug.png",
                content_type="image/png",
                size=1234,
                url="https://cdn.discordapp.com/attachments/bug.png",
            )
        ]
    )

    assert extract_attachment_metadata(message) == [
        {
            "filename": "bug.png",
            "content_type": "image/png",
            "size": 1234,
            "url": "https://cdn.discordapp.com/attachments/bug.png",
        }
    ]


@pytest.mark.asyncio
async def test_client_hands_message_to_capture_handler():
    captured = []

    async def handle_capture(message):
        captured.append(message)

    client = create_discord_client(handle_capture)
    await client.on_message(make_message())

    assert len(captured) == 1


@pytest.mark.asyncio
async def test_client_forwards_filtering_to_capture_boundary():
    captured = []

    async def handle_capture(message):
        captured.append(message)

    message = make_message(author_id=999)
    client = create_discord_client(handle_capture)
    await client.on_message(message)

    assert captured == [message]


def make_attachment(
    *,
    filename="screenshot.png",
    content_type="image/png",
    size=42,
    url="https://cdn.discordapp.com/attachments/screenshot.png",
):
    return SimpleNamespace(
        filename=filename,
        content_type=content_type,
        size=size,
        url=url,
    )
