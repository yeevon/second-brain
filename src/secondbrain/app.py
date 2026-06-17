from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import signal
import sys
import uuid

from secondbrain.api_server import InternalApiServer
from secondbrain.capture_api import create_capture_api
from secondbrain.capture_service import CaptureService
from secondbrain.config import Settings
from secondbrain.discord_capture import create_discord_client
from secondbrain.heartbeat import run_capture_service_heartbeat
from secondbrain.logging_config import configure_logging
from secondbrain.observability import log_metadata
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
        self.heartbeat_task: asyncio.Task | None = None

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
        self.heartbeat_task: asyncio.Task | None = None
        self.delivery_task: asyncio.Task | None = None

    async def start_once(self, client) -> ReconcileResult | None:
        async with self._startup_lock:
            if self._started:
                return None
            result = await self.capture_service.startup_reconcile(client)
            self._started = True
            return result


def ensure_delivery_dispatcher_task(
    *,
    startup,
    capture_service: CaptureService,
    settings,
) -> None:
    task = startup.delivery_task
    if task is not None and not task.done():
        return
    from secondbrain.delivery import run_delivery_dispatcher
    from secondbrain.n8n_delivery import N8nWebhookDeliveryClient
    downstream_client = N8nWebhookDeliveryClient(
        webhook_url=settings.n8n_intake_webhook_url,
        webhook_token=settings.n8n_intake_webhook_token,
        timeout_seconds=settings.delivery_webhook_timeout_seconds,
    )
    startup.delivery_task = asyncio.create_task(
        run_delivery_dispatcher(
            settings=settings,
            ledger=capture_service._ledger,
            downstream_client=downstream_client,
            receipt_edit_client=capture_service,
        )
    )


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


def ensure_heartbeat_task(
    *,
    startup,
    capture_service: CaptureService,
    instance_id: str,
    interval_seconds: int,
    reaper_liveness_threshold_s: int = 300,
    reconcile_liveness_threshold_s: int = 300,
) -> None:
    if startup.heartbeat_task is not None:
        return
    startup.heartbeat_task = asyncio.create_task(
        run_capture_service_heartbeat(
            ledger=capture_service,
            instance_id=instance_id,
            interval_seconds=interval_seconds,
            reaper_liveness_threshold_s=reaper_liveness_threshold_s,
            reconcile_liveness_threshold_s=reconcile_liveness_threshold_s,
        )
    )


def initialize_capture_service_lifecycle(
    *,
    startup,
    capture_service: CaptureService,
    heartbeat_interval_seconds: int,
    reaper_liveness_threshold_s: int = 300,
    reconcile_liveness_threshold_s: int = 300,
) -> str:
    instance_id = str(uuid.uuid4())
    capture_service.record_capture_service_start(
        instance_id=instance_id,
        now=datetime.now(UTC),
    )
    ensure_heartbeat_task(
        startup=startup,
        capture_service=capture_service,
        instance_id=instance_id,
        interval_seconds=heartbeat_interval_seconds,
        reaper_liveness_threshold_s=reaper_liveness_threshold_s,
        reconcile_liveness_threshold_s=reconcile_liveness_threshold_s,
    )
    return instance_id


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
    instance_id = initialize_capture_service_lifecycle(
        startup=startup,
        capture_service=capture_service,
        heartbeat_interval_seconds=settings.capture_service_heartbeat_interval_seconds,
        reaper_liveness_threshold_s=getattr(settings, "reaper_liveness_threshold_s", 300),
        reconcile_liveness_threshold_s=getattr(settings, "reconcile_liveness_threshold_s", 300),
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
            log_metadata(
                "startup_reconcile_complete",
                messages_seen=reconcile_result.seen,
                captures_handled=reconcile_result.handled,
                ignored_messages=reconcile_result.ignored,
                warning=reconcile_result.warning or None,
            )
            log_metadata(
                "background_worker_started",
                recovered_captures=len(capture_ids),
            )
        ensure_periodic_reconciliation_task(
            startup=startup,
            client=client,
            capture_service=capture_service,
        )
        if startup_result is not None:
            capture_service.record_capture_service_ready(
                instance_id=instance_id,
                now=datetime.now(UTC),
            )
            log_metadata(
                "periodic_reconcile_started",
                interval_seconds=settings.periodic_reconcile_interval_seconds,
            )

    client = create_discord_client(
        capture_service.handle_gateway_message,
        start_background_worker_once,
    )
    capture_service.attach_receipt_client(client)
    log_metadata(
        "capture_service_starting",
        mode="local-full",
        downstream_processing="enabled",
        guild_id=settings.discord_guild_id,
        capture_channel_id=settings.discord_capture_channel_id,
        allowed_user_id=settings.discord_allowed_user_id,
        ledger_path=str(settings.ledger_path),
        vault_path=str(settings.vault_path),
        api_host=settings.capture_api_host,
        api_port=settings.capture_api_port,
    )

    api_task = asyncio.create_task(api_server.serve())
    discord_task = asyncio.create_task(client.start(settings.discord_bot_token))
    await run_service_runtime(
        api_task=api_task,
        discord_task=discord_task,
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
        instance_id=instance_id,
    )


async def run_capture_only_runtime(settings: Settings) -> None:
    capture_service = CaptureService.open(settings, notify_capture=None)
    startup = CaptureOnlyStartup(capture_service=capture_service, settings=settings)
    instance_id = initialize_capture_service_lifecycle(
        startup=startup,
        capture_service=capture_service,
        heartbeat_interval_seconds=settings.capture_service_heartbeat_interval_seconds,
        reaper_liveness_threshold_s=getattr(settings, "reaper_liveness_threshold_s", 300),
        reconcile_liveness_threshold_s=getattr(settings, "reconcile_liveness_threshold_s", 300),
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

    async def reconcile_once() -> None:
        reconcile_result = await startup.start_once(client)
        if reconcile_result is not None:
            log_metadata(
                "startup_reconcile_complete",
                messages_seen=reconcile_result.seen,
                captures_handled=reconcile_result.handled,
                ignored_messages=reconcile_result.ignored,
                warning=reconcile_result.warning or None,
            )
            log_metadata("discord_listener_ready")
        ensure_periodic_reconciliation_task(
            startup=startup,
            client=client,
            capture_service=capture_service,
        )
        ensure_stale_lease_reaper_task(
            startup=startup,
            capture_service=capture_service,
        )
        if settings.downstream_delivery_enabled:
            ensure_delivery_dispatcher_task(
                startup=startup,
                capture_service=capture_service,
                settings=settings,
            )
        if reconcile_result is not None:
            capture_service.record_capture_service_ready(
                instance_id=instance_id,
                now=datetime.now(UTC),
            )
            log_metadata(
                "periodic_reconcile_started",
                interval_seconds=settings.periodic_reconcile_interval_seconds,
            )

    client = create_discord_client(
        capture_service.handle_gateway_message,
        reconcile_once,
    )
    capture_service.attach_receipt_client(client)
    ensure_stale_lease_reaper_task(
        startup=startup,
        capture_service=capture_service,
    )
    log_metadata(
        "capture_service_starting",
        mode="capture-only",
        downstream_processing="enabled" if settings.downstream_delivery_enabled else "disabled",
        guild_id=settings.discord_guild_id,
        capture_channel_id=settings.discord_capture_channel_id,
        allowed_user_id=settings.discord_allowed_user_id,
        ledger_path=str(settings.ledger_path),
        api_host=settings.capture_api_host,
        api_port=settings.capture_api_port,
    )

    api_task = asyncio.create_task(api_server.serve())
    discord_task = asyncio.create_task(client.start(settings.discord_bot_token))
    await run_service_runtime(
        api_task=api_task,
        discord_task=discord_task,
        api_server=api_server,
        client=client,
        startup=startup,
        capture_service=capture_service,
        instance_id=instance_id,
    )


async def run_service_runtime(
    *,
    api_task: asyncio.Task,
    discord_task: asyncio.Task,
    api_server,
    client,
    startup,
    capture_service: CaptureService,
    instance_id: str | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            continue
        installed_signals.append(sig)

    stop_waiter = asyncio.ensure_future(stop_event.wait())
    tasks = (api_task, discord_task)
    try:
        done, _pending = await asyncio.wait(
            {api_task, discord_task, stop_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task is not stop_waiter:
                task.result()
    finally:
        if not stop_waiter.done():
            stop_waiter.cancel()
        with suppress(asyncio.CancelledError):
            await stop_waiter

        async def _shutdown_step(name: str, coro) -> None:
            try:
                await coro
            except Exception as exc:
                log_metadata(
                    "shutdown_step_failed",
                    step=name,
                    error_type=type(exc).__name__,
                )

        await _shutdown_step("stop_api_server", api_server.stop())
        await _shutdown_step("close_discord_client", client.close())

        for attr in ("periodic_task", "worker_task", "reaper_task", "heartbeat_task", "delivery_task"):
            task = getattr(startup, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception) as exc:
                    if not isinstance(exc, asyncio.CancelledError):
                        log_metadata(
                            "shutdown_step_failed",
                            step=f"cancel_{attr}",
                            error_type=type(exc).__name__,
                        )

        if instance_id is not None:
            try:
                capture_service.record_capture_service_stop(
                    instance_id=instance_id,
                    now=datetime.now(UTC),
                )
            except Exception as exc:
                log_metadata(
                    "shutdown_step_failed",
                    step="record_capture_service_stop",
                    error_type=type(exc).__name__,
                )

        for task in tasks:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        try:
            capture_service.close()
        except Exception as exc:
            log_metadata(
                "shutdown_step_failed",
                step="close_capture_service",
                error_type=type(exc).__name__,
            )

        for sig in installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except Exception as exc:
                log_metadata(
                    "shutdown_step_failed",
                    step=f"remove_signal_handler_{sig}",
                    error_type=type(exc).__name__,
                )


def run_discord_listener() -> None:
    configure_logging()
    try:
        asyncio.run(run_service())
    except KeyboardInterrupt:
        log_metadata("shutdown_complete")


def run_status() -> int:
    from secondbrain.status import (
        StatusSettings,
        OperationalStatusUnavailable,
        read_operational_status,
        format_operational_status,
    )
    settings = StatusSettings.from_env()
    try:
        snapshot = read_operational_status(settings=settings)
    except OperationalStatusUnavailable as exc:
        print("Second Brain operational status unavailable")
        print(f"reason: {exc.safe_reason}")
        return 2
    print(format_operational_status(snapshot))
    if (
        snapshot.capture_service_health != "HEALTHY"
        or snapshot.stale_leases > 0
        or snapshot.captures_failed > 0
    ):
        return 1
    return 0


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


def run_preflight_command() -> int:
    from secondbrain.preflight import run_preflight, format_preflight_results
    checks = run_preflight()
    output, all_passed = format_preflight_results(checks)
    print(output)
    return 0 if all_passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secondbrain")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="listen for Discord captures and print them")
    subparsers.add_parser("status", help="report local ledger and vault status")
    subparsers.add_parser("preflight", help="validate configuration without starting the service")
    retry_parser = subparsers.add_parser("retry", help="queue a manual retry for a failed capture")
    retry_parser.add_argument("capture_id", help="the capture ID to retry (e.g. SB-20260607-0042)")

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            run_discord_listener()
            return 0
        if args.command == "status":
            return run_status()
        if args.command == "preflight":
            return run_preflight_command()
        if args.command == "retry":
            return 0 if run_manual_retry(args.capture_id) else 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2
