from __future__ import annotations

import argparse
import asyncio

from secondbrain.config import Settings
from secondbrain.discord_capture import create_discord_client, extract_attachment_metadata
from secondbrain.ledger import Ledger
from secondbrain.receipts import send_rejection_receipt, send_saved_receipt
from secondbrain.reconcile import LAST_RECONCILED_MESSAGE_ID, reconcile_discord_history
from secondbrain.secret_screen import screen_text
from secondbrain.vault_writer import VaultWriter
from secondbrain.worker import CaptureQueue, enqueue_unfinished_captures, run_capture_worker


def create_capture_handler(
    settings: Settings,
    ledger: Ledger,
    queue: CaptureQueue,
    *,
    enqueue_captures: bool = True,
    advance_reconcile_marker: bool = False,
):
    async def handle_capture(message) -> None:
        raw_text = message.content.strip() if message.content else ""
        secret_result = screen_text(raw_text)

        if secret_result.is_sensitive:
            result = ledger.insert_sensitive_rejection(
                discord_message_id=str(message.id),
                discord_channel_id=str(message.channel.id),
                discord_guild_id=str(message.guild.id),
                discord_author_id=str(message.author.id),
                redacted_text=secret_result.redacted_text,
                sensitivity_flags=secret_result.flags,
            )
            capture = result.capture
            if advance_reconcile_marker:
                ledger.set_system_state(LAST_RECONCILED_MESSAGE_ID, str(message.id))
            if not result.created:
                print(f"duplicate Discord capture ignored: {capture.capture_id}")
                return

            try:
                receipt_message_id = await send_rejection_receipt(
                    message,
                    capture,
                    flags=secret_result.flags,
                )
            except Exception as exc:
                print(f"{capture.capture_id} rejection receipt failed: {type(exc).__name__}: {exc}")
            else:
                ledger.set_receipt_message_id(capture.capture_id, receipt_message_id)
            print(f"rejected sensitive Discord capture: {capture.capture_id}")
            print(f"  flags: {list(secret_result.flags)}")
            return

        attachment_metadata = extract_attachment_metadata(message)
        result = ledger.insert_accepted_capture(
            discord_message_id=str(message.id),
            discord_channel_id=str(message.channel.id),
            discord_guild_id=str(message.guild.id),
            discord_author_id=str(message.author.id),
            raw_text=raw_text,
            has_attachments=bool(attachment_metadata),
            attachment_metadata=attachment_metadata,
        )
        capture = result.capture
        if advance_reconcile_marker:
            ledger.set_system_state(LAST_RECONCILED_MESSAGE_ID, str(message.id))
        if not result.created:
            print(f"duplicate Discord capture ignored: {capture.capture_id}")
            return

        receipt_message_id = None
        try:
            receipt_message_id = await send_saved_receipt(
                message,
                capture,
                has_attachments=bool(attachment_metadata),
            )
        except Exception as exc:
            print(f"{capture.capture_id} saved receipt failed: {type(exc).__name__}: {exc}")
        else:
            ledger.set_receipt_message_id(capture.capture_id, receipt_message_id)
        if enqueue_captures:
            await queue.enqueue(capture.capture_id)

        print(f"{capture.capture_id} received. Your note is saved. Processing...")
        print(f"  message_id: {message.id}")
        print(f"  receipt_message_id: {receipt_message_id}")
        print(f"  queued: {capture.capture_id}" if enqueue_captures else "  queued: deferred to startup recovery")
        if attachment_metadata:
            print("  attachment warning: attachment detected but not archived in the MVP")
            print(f"  attachments: {attachment_metadata}")

    return handle_capture


def run_discord_listener() -> None:
    settings = Settings()
    ledger = Ledger(settings.ledger_path)
    queue = CaptureQueue(maxsize=settings.classifier_queue_maxsize)
    vault_writer = VaultWriter(settings.vault_path)
    handle_capture = create_capture_handler(
        settings,
        ledger,
        queue,
        advance_reconcile_marker=True,
    )
    reconcile_capture = create_capture_handler(settings, ledger, queue, enqueue_captures=False)
    worker_started = False

    async def start_background_worker_once() -> None:
        nonlocal worker_started
        if worker_started:
            return

        reconcile_result = await reconcile_discord_history(
            client=client,
            settings=settings,
            ledger=ledger,
            handle_capture=reconcile_capture,
        )
        capture_ids = await enqueue_unfinished_captures(ledger, queue)
        worker_started = True
        asyncio.create_task(
            run_capture_worker(
                settings=settings,
                ledger=ledger,
                queue=queue,
                vault_writer=vault_writer,
                receipt_client=client,
            )
        )
        print("startup Discord history reconciliation complete")
        print(f"  messages seen: {reconcile_result.seen}")
        print(f"  captures handled: {reconcile_result.handled}")
        print(f"  ignored messages: {reconcile_result.ignored}")
        if reconcile_result.warning:
            print(f"  warning: {reconcile_result.warning}")
        print("background classifier worker started")
        print(f"  recovered captures queued: {len(capture_ids)}")

    client = create_discord_client(settings, handle_capture, start_background_worker_once)
    print("starting Discord listener")
    print(f"  guild_id: {settings.discord_guild_id}")
    print(f"  capture_channel_id: {settings.discord_capture_channel_id}")
    print(f"  allowed_user_id: {settings.discord_allowed_user_id}")
    print(f"  ledger_path: {settings.ledger_path}")
    print(f"  vault_path: {settings.vault_path}")
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
