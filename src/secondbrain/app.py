from __future__ import annotations

import argparse

from secondbrain.config import Settings
from secondbrain.discord_capture import create_discord_client, extract_attachment_metadata
from secondbrain.ledger import Ledger
from secondbrain.secret_screen import screen_text
from secondbrain.worker import CaptureQueue


def create_capture_handler(settings: Settings, ledger: Ledger, queue: CaptureQueue):
    async def handle_capture(message) -> None:
        raw_text = message.content.strip() if message.content else ""
        secret_result = screen_text(raw_text)

        if secret_result.is_sensitive:
            capture = ledger.insert_sensitive_rejection(
                discord_message_id=str(message.id),
                discord_channel_id=str(message.channel.id),
                discord_guild_id=str(message.guild.id),
                discord_author_id=str(message.author.id),
                redacted_text=secret_result.redacted_text,
                sensitivity_flags=secret_result.flags,
            )
            print(f"rejected sensitive Discord capture: {capture.capture_id}")
            print(f"  flags: {list(secret_result.flags)}")
            return

        attachment_metadata = extract_attachment_metadata(message)
        capture = ledger.insert_accepted_capture(
            discord_message_id=str(message.id),
            discord_channel_id=str(message.channel.id),
            discord_guild_id=str(message.guild.id),
            discord_author_id=str(message.author.id),
            raw_text=raw_text,
            has_attachments=bool(attachment_metadata),
            attachment_metadata=attachment_metadata,
        )

        receipt_message_id = f"terminal-receipt-{capture.capture_id}"
        ledger.set_receipt_message_id(capture.capture_id, receipt_message_id)
        await queue.enqueue(capture.capture_id)

        print(f"{capture.capture_id} received. Your note is saved. Processing...")
        print(f"  message_id: {message.id}")
        print(f"  receipt_message_id: {receipt_message_id}")
        print(f"  queued: {capture.capture_id}")
        if attachment_metadata:
            print("  attachment warning: attachment detected but not archived in the MVP")
            print(f"  attachments: {attachment_metadata}")

    return handle_capture


def run_discord_listener() -> None:
    settings = Settings()
    ledger = Ledger(settings.ledger_path)
    queue = CaptureQueue(maxsize=settings.classifier_queue_maxsize)
    handle_capture = create_capture_handler(settings, ledger, queue)
    client = create_discord_client(settings, handle_capture)
    print("starting Discord listener")
    print(f"  guild_id: {settings.discord_guild_id}")
    print(f"  capture_channel_id: {settings.discord_capture_channel_id}")
    print(f"  allowed_user_id: {settings.discord_allowed_user_id}")
    print(f"  ledger_path: {settings.ledger_path}")
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
