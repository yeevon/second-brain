from __future__ import annotations

import argparse
import asyncio

from secondbrain.capture_models import CaptureStatusSnapshot
from secondbrain.capture_service import CaptureService
from secondbrain.config import Settings
from secondbrain.discord_capture import create_discord_client
from secondbrain.vault_writer import VaultWriter
from secondbrain.worker import CaptureQueue, run_capture_worker


def run_discord_listener() -> None:
    settings = Settings()
    queue = CaptureQueue(maxsize=settings.classifier_queue_maxsize)
    vault_writer = VaultWriter(settings.vault_path)
    capture_service = CaptureService.open(
        settings,
        notify_capture=queue.enqueue,
    )
    worker_started = False

    async def start_background_worker_once() -> None:
        nonlocal worker_started
        if worker_started:
            return

        reconcile_result = await capture_service.startup_reconcile(client)
        capture_ids = await capture_service.enqueue_unfinished_captures()
        worker_started = True
        asyncio.create_task(
            run_capture_worker(
                settings=settings,
                capture_service=capture_service,
                queue=queue,
                vault_writer=vault_writer,
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

    client = create_discord_client(
        capture_service.handle_gateway_message,
        start_background_worker_once,
    )
    capture_service.attach_receipt_client(client)
    print("starting Discord listener")
    print(f"  guild_id: {settings.discord_guild_id}")
    print(f"  capture_channel_id: {settings.discord_capture_channel_id}")
    print(f"  allowed_user_id: {settings.discord_allowed_user_id}")
    print(f"  ledger_path: {settings.ledger_path}")
    print(f"  vault_path: {settings.vault_path}")
    client.run(settings.discord_bot_token)


def format_status_report(settings: Settings, snapshot: CaptureStatusSnapshot) -> str:
    last_reconciled = snapshot.last_reconciled_discord_message_id or "none"
    last_successful_vault_write = snapshot.last_successful_vault_write or "none"
    return "\n".join(
        [
            "Second Brain status",
            f"ledger path: {settings.ledger_path}",
            f"vault path: {settings.vault_path}",
            f"total captures: {snapshot.total_captures}",
            f"captures filed: {snapshot.filed}",
            f"captures in inbox: {snapshot.inbox}",
            f"captures rejected as sensitive: {snapshot.rejected_sensitive}",
            f"captures failed: {snapshot.failed}",
            f"last reconciled Discord message ID: {last_reconciled}",
            f"last successful vault write: {last_successful_vault_write}",
        ]
    )


def run_status() -> None:
    settings = Settings()
    capture_service = CaptureService.open(settings)
    try:
        print(format_status_report(settings, capture_service.status_snapshot()))
    finally:
        capture_service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secondbrain")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="listen for Discord captures and print them")
    subparsers.add_parser("status", help="report local ledger and vault status")

    args = parser.parse_args(argv)
    if args.command == "run":
        run_discord_listener()
        return 0
    if args.command == "status":
        run_status()
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
