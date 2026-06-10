from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import sys

from secondbrain.api_server import InternalApiServer
from secondbrain.capture_models import CaptureStatusSnapshot
from secondbrain.capture_api import create_capture_api
from secondbrain.capture_service import CaptureService
from secondbrain.config import Settings
from secondbrain.discord_capture import create_discord_client
from secondbrain.reconcile import ReconcileResult
from secondbrain.vault_writer import VaultWriter
from secondbrain.worker import CaptureQueue, run_capture_worker


@dataclass(frozen=True)
class LocalWorkerStartupResult:
    reconcile_result: ReconcileResult
    worker_task: asyncio.Task
    capture_ids: list[str]


async def start_local_worker_and_enqueue_recovered(
    *,
    settings: Settings,
    capture_service: CaptureService,
    queue: CaptureQueue,
    vault_writer: VaultWriter,
):
    worker_task = asyncio.create_task(
        run_capture_worker(
            settings=settings,
            capture_service=capture_service,
            queue=queue,
            vault_writer=vault_writer,
        )
    )
    try:
        capture_ids = await capture_service.enqueue_unfinished_captures()
    except BaseException:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        raise
    return worker_task, capture_ids


class LocalWorkerStartup:
    def __init__(
        self,
        *,
        settings: Settings,
        capture_service: CaptureService,
        queue: CaptureQueue,
        vault_writer: VaultWriter,
    ) -> None:
        self.settings = settings
        self.capture_service = capture_service
        self.queue = queue
        self.vault_writer = vault_writer
        self._startup_lock = asyncio.Lock()
        self.worker_task: asyncio.Task | None = None
        self.periodic_task: asyncio.Task | None = None

    async def start_once(self, client) -> LocalWorkerStartupResult | None:
        async with self._startup_lock:
            if self.worker_task is not None and not self.worker_task.done():
                return None

            reconcile_result = await self.capture_service.startup_reconcile(client)
            self.worker_task, capture_ids = await start_local_worker_and_enqueue_recovered(
                settings=self.settings,
                capture_service=self.capture_service,
                queue=self.queue,
                vault_writer=self.vault_writer,
            )
            return LocalWorkerStartupResult(
                reconcile_result=reconcile_result,
                worker_task=self.worker_task,
                capture_ids=capture_ids,
            )


class CaptureOnlyStartup:
    def __init__(self, *, capture_service: CaptureService, settings=None) -> None:
        self.capture_service = capture_service
        self.settings = settings
        self._startup_lock = asyncio.Lock()
        self._started = False
        self.worker_task: asyncio.Task | None = None
        self.periodic_task: asyncio.Task | None = None
        self.reaper_task: asyncio.Task | None = None

    async def start_once(self, client) -> ReconcileResult | None:
        async with self._startup_lock:
            if self._started:
                return None
            result = await self.capture_service.startup_reconcile(client)
            self._started = True
            return result


def ensure_stale_lease_reaper_task(
    *,
    startup,
    capture_service: CaptureService,
) -> None:
    task = startup.reaper_task
    if task is not None and not task.done():
        return
    startup.reaper_task = asyncio.create_task(
        capture_service.run_stale_lease_reaper_loop()
    )


def ensure_periodic_reconciliation_task(
    *,
    startup,
    client,
    capture_service: CaptureService,
) -> None:
    task = startup.periodic_task
    if task is not None and not task.done():
        return
    startup.periodic_task = asyncio.create_task(
        capture_service.run_periodic_reconciliation_loop(client)
    )


async def run_service() -> None:
    settings = Settings()
    if settings.capture_processing_mode == "local-full":
        await run_local_full_runtime(settings)
        return
    if settings.capture_processing_mode == "capture-only":
        await run_capture_only_runtime(settings)
        return
    raise RuntimeError(f"unsupported capture processing mode: {settings.capture_processing_mode}")


async def run_local_full_runtime(settings: Settings) -> None:
    queue = CaptureQueue(maxsize=settings.classifier_queue_maxsize)
    vault_writer = VaultWriter(settings.vault_path)
    capture_service = CaptureService.open(
        settings,
        notify_capture=queue.enqueue,
    )
    startup = LocalWorkerStartup(
        settings=settings,
        capture_service=capture_service,
        queue=queue,
        vault_writer=vault_writer,
    )
    api = create_capture_api(
        capture_service=capture_service,
        internal_token=settings.capture_service_internal_token,
    )
    api_server = InternalApiServer(
        api,
        host=settings.capture_api_host,
        port=settings.capture_api_port,
    )

    async def start_background_worker_once() -> None:
        startup_result = await startup.start_once(client)
        if startup_result is not None:
            reconcile_result = startup_result.reconcile_result
            capture_ids = startup_result.capture_ids
            print("startup Discord history reconciliation complete")
            print(f"  messages seen: {reconcile_result.seen}")
            print(f"  captures handled: {reconcile_result.handled}")
            print(f"  ignored messages: {reconcile_result.ignored}")
            if reconcile_result.warning:
                print(f"  warning: {reconcile_result.warning}")
            print("background classifier worker started")
            print(f"  recovered captures queued: {len(capture_ids)}")
        ensure_periodic_reconciliation_task(
            startup=startup,
            client=client,
            capture_service=capture_service,
        )
        if startup_result is not None:
            print(f"  periodic reconciliation started (interval: {settings.periodic_reconcile_interval_seconds}s)")

    client = create_discord_client(
        capture_service.handle_gateway_message,
        start_background_worker_once,
    )
    capture_service.attach_receipt_client(client)
    print("capture-service runtime mode: local-full")
    print("downstream processing: enabled")
    print("starting Discord listener")
    print(f"  guild_id: {settings.discord_guild_id}")
    print(f"  capture_channel_id: {settings.discord_capture_channel_id}")
    print(f"  allowed_user_id: {settings.discord_allowed_user_id}")
    print(f"  ledger_path: {settings.ledger_path}")
    print(f"  vault_path: {settings.vault_path}")
    print(f"  internal API host: {settings.capture_api_host}")
    print(f"  internal API port: {settings.capture_api_port}")
    print("  internal API authentication: configured")
    print(f"capture-service API started on internal container port {settings.capture_api_port}")

    api_task = asyncio.create_task(api_server.serve())
    discord_task = asyncio.create_task(client.start(settings.discord_bot_token))
    await run_service_runtime(
        api_task=api_task,
        discord_task=discord_task,
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
    )


async def run_capture_only_runtime(settings: Settings) -> None:
    capture_service = CaptureService.open(settings, notify_capture=None)
    startup = CaptureOnlyStartup(capture_service=capture_service, settings=settings)
    api = create_capture_api(
        capture_service=capture_service,
        internal_token=settings.capture_service_internal_token,
    )
    api_server = InternalApiServer(
        api,
        host=settings.capture_api_host,
        port=settings.capture_api_port,
    )

    async def reconcile_once() -> None:
        reconcile_result = await startup.start_once(client)
        if reconcile_result is not None:
            print("startup Discord history reconciliation complete")
            print(f"  messages seen: {reconcile_result.seen}")
            print(f"  captures handled: {reconcile_result.handled}")
            print(f"  ignored messages: {reconcile_result.ignored}")
            if reconcile_result.warning:
                print(f"  warning: {reconcile_result.warning}")
            print("Discord listener ready")
        ensure_periodic_reconciliation_task(
            startup=startup,
            client=client,
            capture_service=capture_service,
        )
        ensure_stale_lease_reaper_task(
            startup=startup,
            capture_service=capture_service,
        )
        if reconcile_result is not None:
            print(f"  periodic reconciliation started (interval: {settings.periodic_reconcile_interval_seconds}s)")

    client = create_discord_client(
        capture_service.handle_gateway_message,
        reconcile_once,
    )
    capture_service.attach_receipt_client(client)
    ensure_stale_lease_reaper_task(
        startup=startup,
        capture_service=capture_service,
    )
    print("capture-service runtime mode: capture-only")
    print("downstream processing: disabled")
    print("starting Discord listener")
    print(f"  guild_id: {settings.discord_guild_id}")
    print(f"  capture_channel_id: {settings.discord_capture_channel_id}")
    print(f"  allowed_user_id: {settings.discord_allowed_user_id}")
    print(f"  ledger_path: {settings.ledger_path}")
    print(f"  internal API host: {settings.capture_api_host}")
    print(f"  internal API port: {settings.capture_api_port}")
    print("  internal API authentication: configured")
    print(f"capture-service API started on internal container port {settings.capture_api_port}")

    api_task = asyncio.create_task(api_server.serve())
    discord_task = asyncio.create_task(client.start(settings.discord_bot_token))
    await run_service_runtime(
        api_task=api_task,
        discord_task=discord_task,
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
    )


async def run_service_runtime(
    *,
    api_task: asyncio.Task,
    discord_task: asyncio.Task,
    api_server,
    client,
    startup,
    capture_service: CaptureService,
) -> None:
    tasks = (api_task, discord_task)
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        await api_server.stop()
        await client.close()

        for attr in ("periodic_task", "worker_task", "reaper_task"):
            task = getattr(startup, attr, None)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        for task in tasks:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        capture_service.close()


def run_discord_listener() -> None:
    try:
        asyncio.run(run_service())
    except KeyboardInterrupt:
        print("shutdown complete")


def format_status_report(
    settings: Settings,
    snapshot: CaptureStatusSnapshot,
    periodic_snapshot: dict | None = None,
    delivery_snapshot: dict | None = None,
) -> str:
    last_reconciled = snapshot.last_reconciled_discord_message_id or "none"
    last_successful_vault_write = snapshot.last_successful_vault_write or "none"
    lines = [
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
    if periodic_snapshot is not None:
        p = periodic_snapshot
        interval = getattr(settings, "periodic_reconcile_interval_seconds", "?")
        limit = getattr(settings, "periodic_reconcile_limit", "?")
        lines += [
            f"periodic reconciliation interval: {interval} seconds",
            f"periodic reconciliation scan limit: {limit}",
            f"periodic reconciliation runs: {p.get('periodic_reconcile_runs_total') or '0'}",
            f"periodic reconciliation recovered total: {p.get('periodic_reconcile_recovered_total') or '0'}",
            f"periodic reconciliation duplicate total: {p.get('periodic_reconcile_duplicates_total') or '0'}",
            f"periodic reconciliation failures: {p.get('periodic_reconcile_failures_total') or '0'}",
            f"periodic reconciliation limit exceeded count: {p.get('periodic_reconcile_limit_exceeded_total') or '0'}",
            f"periodic reconciliation last run: {p.get('periodic_reconcile_last_run_at') or 'never'}",
            f"periodic reconciliation last success: {p.get('periodic_reconcile_last_success_at') or 'never'}",
            f"periodic reconciliation last recovered count: {p.get('periodic_reconcile_last_recovered_count') or '0'}",
            f"periodic reconciliation last warning: {p.get('periodic_reconcile_last_warning') or 'none'}",
            f"periodic reconciliation most recent error type: {p.get('periodic_reconcile_last_error_type') or 'none'}",
        ]
    if delivery_snapshot is not None:
        d = delivery_snapshot
        interval = getattr(settings, "stale_lease_reaper_interval_seconds", "?")
        batch = getattr(settings, "stale_lease_reaper_batch_size", "?")
        lines += [
            f"stale lease reaper interval: {interval} seconds",
            f"stale lease reaper batch size: {batch}",
            f"active processing leases: {d.get('active_processing_leases', 0)}",
            f"expired processing leases: {d.get('expired_leases', 0)}",
            f"delivery pending forward: {d.get('pending_forward', 0)}",
            f"delivery forwarding: {d.get('forwarding', 0)}",
            f"delivery forwarded awaiting callback: {d.get('forwarded', 0)}",
            f"delivery classifying: {d.get('classifying', 0)}",
            f"delivery waiting for retry: {d.get('retry_wait', 0)}",
            f"delivery failed after retry cap: {d.get('failed', 0)}",
            f"total downstream delivery attempts: {d.get('total_delivery_attempts', 0)}",
            f"current accumulated retry attempts across captures: {d.get('total_retry_attempts', 0)}",
            f"next scheduled retry: {d.get('next_attempt_at') or 'none'}",
        ]
    return "\n".join(lines)


def run_status() -> None:
    settings = Settings()
    capture_service = CaptureService.open(settings)
    try:
        periodic_snapshot = capture_service.periodic_reconcile_snapshot()
        delivery_snapshot = capture_service.delivery_snapshot()
        print(format_status_report(
            settings,
            capture_service.status_snapshot(),
            periodic_snapshot,
            delivery_snapshot,
        ))
    finally:
        capture_service.close()


def run_manual_retry(capture_id: str) -> bool:
    from secondbrain.capture_service import CaptureNotFoundError
    settings = Settings()
    capture_service = CaptureService.open(settings)
    try:
        changed = capture_service.manual_retry_capture(capture_id=capture_id)
        if changed:
            print(f"manual retry queued: {capture_id}")
            return True
        else:
            print(f"manual retry rejected: capture is not in terminal FAILED state", file=sys.stderr)
            return False
    except CaptureNotFoundError:
        print(f"manual retry rejected: capture not found", file=sys.stderr)
        return False
    finally:
        capture_service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secondbrain")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="listen for Discord captures and print them")
    subparsers.add_parser("status", help="report local ledger and vault status")
    retry_parser = subparsers.add_parser("retry", help="queue a manual retry for a failed capture")
    retry_parser.add_argument("capture_id", help="the capture ID to retry (e.g. SB-20260607-0042)")

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            run_discord_listener()
            return 0
        if args.command == "status":
            run_status()
            return 0
        if args.command == "retry":
            return 0 if run_manual_retry(args.capture_id) else 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2
