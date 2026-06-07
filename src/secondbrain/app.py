from __future__ import annotations

import argparse

from secondbrain.config import Settings
from secondbrain.discord_capture import create_discord_client, extract_attachment_metadata


async def print_capture(message) -> None:
    print("accepted Discord capture")
    print(f"  message_id: {message.id}")
    print(f"  author_id: {message.author.id}")
    print(f"  channel_id: {message.channel.id}")
    print(f"  content: {message.content.strip() if message.content else ''}")

    attachment_metadata = extract_attachment_metadata(message)
    if attachment_metadata:
        print(f"  attachments: {attachment_metadata}")


def run_discord_listener() -> None:
    settings = Settings()
    client = create_discord_client(settings, print_capture)
    print("starting Discord listener")
    print(f"  guild_id: {settings.discord_guild_id}")
    print(f"  capture_channel_id: {settings.discord_capture_channel_id}")
    print(f"  allowed_user_id: {settings.discord_allowed_user_id}")
    client.run(settings.discord_bot_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secondbrain")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="listen for Discord captures and print them")

    args = parser.parse_args(argv)
    if args.command == "run":
        run_discord_listener()
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
