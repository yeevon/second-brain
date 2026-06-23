from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from secondbrain.capture_models import (
    CLASSIFYING,
    FAILED,
    FILED,
    FORWARDED,
    INBOX,
    NOT_APPLICABLE,
    PENDING_FORWARD,
    RECEIVED,
    REJECTED_SENSITIVE,
    TERMINAL_STATUSES,
    CaptureRecord,
    CaptureStatusSnapshot,
    DeliveryMutationResult,
    RetryDisposition,
    TransitionResult,
    WorkflowErrorOutcome,
)
from secondbrain.discord_capture import extract_attachment_metadata, should_capture_message
from secondbrain.ledger import UNSET, Ledger
from secondbrain.observability import log_metadata
from secondbrain.receipts import (
    ReceiptDeliveryResult,
    deliver_final_receipt,
    format_downstream_filed_receipt,
    format_filed_receipt,
    format_inbox_receipt,
    format_vault_failure_receipt,
    send_rejection_receipt,
    send_saved_receipt,
)
from secondbrain.reconcile import (
    LAST_RECONCILED_MESSAGE_ID,
    CaptureDisposition,
    ReconcileResult,
    reconcile_discord_history,
)
from secondbrain.secret_screen import screen_text


NotifyCapture = Callable[[str], Awaitable[None]]


class CaptureNotFoundError(Exception):
    pass


class InvalidCaptureTransitionError(Exception):
    pass


class ConflictingReplayError(Exception):
    pass


class ReceiptDeliveryError(Exception):
    pass


class CaptureService:
    def __init__(
        self,
        *,
        settings: Any,
        ledger: Ledger,
        notify_capture: NotifyCapture | None = None,
        receipt_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self._ledger = ledger
        self._notify_capture = notify_capture
        self._receipt_client = receipt_client
        # local-full mode processes notes locally; downstream delivery is not applicable
        mode = getattr(settings, "capture_processing_mode", None)
        self._initial_delivery_status = NOT_APPLICABLE if mode == "local-full" else PENDING_FORWARD
        writer_url = getattr(settings, "writer_service_url", None)
        writer_token = getattr(settings, "writer_service_token", None)
        self._writer_client: WriterServiceClient | None = (
            WriterServiceClient(url=writer_url, token=writer_token)
            if writer_url and writer_token
            else None
        )

    @classmethod
    def open(
        cls,
        settings: Any,
        *,
        notify_capture: NotifyCapture | None = None,
        receipt_client: Any | None = None,
    ) -> "CaptureService":
        return cls(
            settings=settings,
            ledger=Ledger(settings.ledger_path, settings),
            notify_capture=notify_capture,
            receipt_client=receipt_client,
        )

    def attach_receipt_client(self, client: Any) -> None:
        self._receipt_client = client

    async def handle_gateway_message(self, message) -> None:
        if not should_capture_message(message, self.settings):
            return
        content = (message.content or "").strip()
        approve_m = _APPROVE_VUP_RE.match(content)
        if approve_m:
            await self._handle_proposal_approve(approve_m.group(1), message)
            return
        reject_m = _REJECT_VUP_RE.match(content)
        if reject_m:
            await self._handle_proposal_reject(reject_m.group(1), message)
            return
        if _FIX_REPLY_RE.match(content) or _FIX_EXPLICIT_RE.match(content):
            await self.handle_gateway_correction(message)
            return
        await self._capture_if_allowed(message, notify_downstream=True)

    async def startup_reconcile(self, client: Any) -> ReconcileResult:
        result = await reconcile_discord_history(
            client=client,
            settings=self.settings,
            ledger=self._ledger,
            handle_capture=self.make_capture_handler(notify_downstream=False),
            mode="startup",
            scan_limit=self.settings.startup_reconcile_limit,
        )
        self._ledger.record_successful_reconciliation(mode="startup", now=datetime.now(UTC))
        return result

    def record_capture_service_start(self, *, instance_id: str, now: datetime) -> None:
        self._ledger.record_capture_service_start(instance_id=instance_id, now=now)
        log_metadata("capture_service_starting", instance_id=instance_id)

    def record_capture_service_ready(self, *, instance_id: str, now: datetime) -> bool:
        updated = self._ledger.record_capture_service_ready(instance_id=instance_id, now=now)
        if updated:
            log_metadata("capture_service_ready", instance_id=instance_id)
        else:
            log_metadata("capture_service_ready_ignored", instance_id=instance_id, reason="superseded_instance")
        return updated

    def record_capture_service_heartbeat(self, *, instance_id: str, now: datetime) -> bool:
        return self._ledger.record_capture_service_heartbeat(instance_id=instance_id, now=now)

    def get_system_state(self, key: str) -> str | None:
        return self._ledger.get_system_state(key)

    def set_system_state(self, key: str, value: str) -> None:
        self._ledger.set_system_state(key, value)

    def record_capture_service_stop(self, *, instance_id: str, now: datetime) -> bool:
        updated = self._ledger.record_capture_service_stop(instance_id=instance_id, now=now)
        if updated:
            log_metadata("capture_service_stopped", instance_id=instance_id)
        else:
            log_metadata("capture_service_stop_ignored", instance_id=instance_id, reason="superseded_instance")
        return updated

    def make_capture_handler(self, *, notify_downstream: bool):
        async def handle(message):
            return await self._capture_if_allowed(message, notify_downstream=notify_downstream)
        return handle

    async def run_periodic_reconciliation_loop(self, client) -> None:
        from secondbrain.reconcile import run_periodic_reconciliation
        await run_periodic_reconciliation(
            client=client,
            settings=self.settings,
            ledger=self._ledger,
            handle_capture=self.make_capture_handler(notify_downstream=True),
        )

    async def enqueue_unfinished_captures(self) -> list[str]:
        if self._initial_delivery_status == NOT_APPLICABLE:
            self._ledger.normalize_delivery_for_local_full()
        capture_ids = self.unfinished_capture_ids()
        if self._notify_capture is not None:
            for capture_id in capture_ids:
                await self._notify_capture(capture_id)
        return capture_ids

    def unfinished_capture_ids(self) -> list[str]:
        self._ledger.reset_classifying_to_received()
        return self._ledger.enqueueable_capture_ids()

    def claim_for_processing(self, capture_id: str) -> CaptureRecord | None:
        try:
            transition = self.mark_classifying(capture_id)
        except (CaptureNotFoundError, InvalidCaptureTransitionError, ConflictingReplayError):
            return None
        if not transition.changed:
            return None

        capture = self._ledger.get_capture(capture_id)
        log_metadata(
            "capture_classifying",
            capture_id=capture.capture_id,
            discord_message_id=capture.discord_message_id,
            status_transition=f"{RECEIVED}->{CLASSIFYING}",
        )
        return capture

    async def complete_filed(
        self,
        *,
        capture_id: str,
        classification,
        note_path: str,
    ) -> None:
        capture = self._ledger.get_capture(capture_id)
        transition = self.mark_filed(
            capture_id=capture_id,
            note_path=note_path,
            classification=classification,
        )
        updated = self._ledger.get_capture(capture_id)
        if transition.changed:
            log_metadata(
                "capture_filed",
                capture_id=updated.capture_id,
                discord_message_id=updated.discord_message_id,
                status_transition=f"{CLASSIFYING}->{FILED}",
                classification_confidence=classification.confidence,
                derived_note_path=note_path,
            )
            try:
                await self.edit_receipt(
                    capture_id=updated.capture_id,
                    content=format_filed_receipt(
                        capture_id=updated.capture_id,
                        note_path=note_path,
                        classification=classification,
                        has_attachments=capture.has_attachments,
                    ),
                )
            except ReceiptDeliveryError as exc:
                self._ledger.set_receipt_sync_status(
                    updated.capture_id,
                    status="failed",
                    last_attempt_at=datetime.now(UTC).isoformat(),
                    error_type=type(exc).__name__,
                )

    async def complete_inbox(
        self,
        *,
        capture_id: str,
        classification,
        note_path: str,
        reason: str | None,
    ) -> None:
        capture = self._ledger.get_capture(capture_id)
        transition = self.mark_inbox(
            capture_id=capture_id,
            note_path=note_path,
            classification=classification,
            reason=reason,
        )
        updated = self._ledger.get_capture(capture_id)
        if transition.changed:
            log_metadata(
                "capture_inbox",
                capture_id=updated.capture_id,
                discord_message_id=updated.discord_message_id,
                status_transition=f"{CLASSIFYING}->{INBOX}",
                classification_confidence=classification.confidence,
                derived_note_path=note_path,
                inbox_reason_type=_safe_inbox_reason_type(reason),
                error_type=_safe_inbox_error_type(reason),
            )
            try:
                await self.edit_receipt(
                    capture_id=updated.capture_id,
                    content=format_inbox_receipt(
                        capture_id=updated.capture_id,
                        note_path=note_path,
                        reason=reason,
                        has_attachments=capture.has_attachments,
                    ),
                )
            except ReceiptDeliveryError as exc:
                self._ledger.set_receipt_sync_status(
                    updated.capture_id,
                    status="failed",
                    last_attempt_at=datetime.now(UTC).isoformat(),
                    error_type=type(exc).__name__,
                )

    async def complete_failed(
        self,
        *,
        capture_id: str,
        reason: str,
        classification=None,
    ) -> None:
        capture = self._ledger.get_capture(capture_id)
        transition = self.mark_failed(
            capture_id=capture_id,
            reason=reason,
            classification=classification,
        )
        updated = self._ledger.get_capture(capture_id)
        if transition.changed:
            log_metadata(
                "capture_failed",
                capture_id=updated.capture_id,
                discord_message_id=updated.discord_message_id,
                status_transition=f"{capture.status}->{FAILED}",
                classification_confidence=getattr(classification, "confidence", None),
                error_type=_safe_failure_error_type(reason),
            )
            try:
                await self.edit_receipt(
                    capture_id=updated.capture_id,
                    content=format_vault_failure_receipt(
                        capture_id,
                        has_attachments=capture.has_attachments,
                    ),
                )
            except ReceiptDeliveryError as exc:
                self._ledger.set_receipt_sync_status(
                    updated.capture_id,
                    status="failed",
                    last_attempt_at=datetime.now(UTC).isoformat(),
                    error_type=type(exc).__name__,
                )

    def get_capture(self, capture_id: str) -> CaptureRecord:
        try:
            return self._ledger.get_capture(capture_id)
        except KeyError as exc:
            raise CaptureNotFoundError("capture not found") from exc

    def daily_digest_snapshot(self, *, since, now) -> dict:
        return self._ledger.daily_digest_snapshot(since=since, now=now)

    def weekly_digest_snapshot(self, *, since, now) -> dict:
        return self._ledger.weekly_digest_snapshot(since=since, now=now)

    # ------------------------------------------------------------------
    # SB-136: Vault update proposal delegation
    # ------------------------------------------------------------------

    def create_proposal(self, **kwargs):
        return self._ledger.create_proposal(**kwargs)

    def get_proposal(self, proposal_id: str):
        return self._ledger.get_proposal(proposal_id)

    def list_proposals(self, *, status=None, limit=50):
        return self._ledger.list_proposals(status=status, limit=limit)

    def update_proposal(self, proposal_id: str, **kwargs):
        return self._ledger.update_proposal(proposal_id, **kwargs)

    def assert_healthy(self) -> None:
        self._ledger.ping()

    def mark_forwarded(self, capture_id: str) -> TransitionResult:
        return self._transition_capture(
            capture_id,
            from_statuses={RECEIVED},
            to_status=FORWARDED,
            event_type="CAPTURE_FORWARDED",
            event_payload={"status": FORWARDED},
        )

    def mark_classifying(self, capture_id: str) -> TransitionResult:
        return self._transition_capture(
            capture_id,
            from_statuses={RECEIVED, FORWARDED},
            to_status=CLASSIFYING,
            event_type="CAPTURE_CLASSIFYING",
            event_payload={"status": CLASSIFYING},
        )

    def mark_filed(self, *, capture_id: str, note_path: str, classification) -> TransitionResult:
        classification_json = classification.model_dump(mode="json")
        return self._transition_capture(
            capture_id,
            from_statuses={CLASSIFYING},
            to_status=FILED,
            classification_json=classification_json,
            derived_note_path=note_path,
            last_error=None,
            event_type="CAPTURE_FILED",
            event_payload={"path": note_path},
            replay_payload={
                "classification_json": classification_json,
                "derived_note_path": note_path,
                "last_error": None,
            },
        )

    def mark_inbox(
        self,
        *,
        capture_id: str,
        note_path: str,
        classification,
        reason: str | None,
    ) -> TransitionResult:
        classification_json = classification.model_dump(mode="json")
        event_payload = {"path": note_path}
        if reason is not None:
            event_payload["reason"] = reason
        return self._transition_capture(
            capture_id,
            from_statuses={CLASSIFYING},
            to_status=INBOX,
            classification_json=classification_json,
            derived_note_path=note_path,
            last_error=reason,
            event_type="CAPTURE_INBOX",
            event_payload=event_payload,
            replay_payload={
                "classification_json": classification_json,
                "derived_note_path": note_path,
                "last_error": reason,
            },
        )

    def mark_failed(self, *, capture_id: str, reason: str, classification=None) -> TransitionResult:
        classification_json = None
        if classification is not None:
            classification_json = classification.model_dump(mode="json")
        return self._transition_capture(
            capture_id,
            from_statuses={RECEIVED, FORWARDED, CLASSIFYING},
            to_status=FAILED,
            classification_json=classification_json,
            last_error=reason,
            event_type="CAPTURE_FAILED",
            event_payload={"reason": reason},
            replay_payload={
                "classification_json": classification_json,
                "last_error": reason,
            },
        )

    def retry(self, capture_id: str) -> TransitionResult:
        return self._transition_capture(
            capture_id,
            from_statuses={FAILED},
            to_status=RECEIVED,
            classification_json=None,
            derived_note_path=None,
            last_error=None,
            event_type="CAPTURE_RETRIED",
            event_payload={"status": RECEIVED},
        )

    async def edit_receipt(self, *, capture_id: str, content: str) -> ReceiptDeliveryResult:
        capture = self.get_capture(capture_id)
        delivery = await self._deliver_final_receipt(capture, content)
        if not delivery.delivered:
            raise ReceiptDeliveryError("receipt delivery failed")
        self._ledger.set_receipt_sync_status(capture_id, status="clean", error_type=None)
        return ReceiptDeliveryResult(
            delivered=delivery.delivered,
            replaced=delivery.replaced,
            receipt_message_id=delivery.receipt_message_id,
        )

    def captures_by_status(self, status: str) -> list[CaptureRecord]:
        return self._ledger.captures_by_status(status)

    def status_counts(self) -> dict[str, int]:
        return self._ledger.status_counts()

    def total_captures(self) -> int:
        return self._ledger.total_captures()

    def last_reconciled_message_id(self) -> str | None:
        return self._ledger.get_system_state(LAST_RECONCILED_MESSAGE_ID)

    def periodic_reconcile_snapshot(self) -> dict[str, str | None]:
        return self._ledger.periodic_reconcile_snapshot()

    def delivery_snapshot(self) -> dict:
        return self._ledger.delivery_snapshot()

    # ------------------------------------------------------------------
    # Attempt-aware downstream delivery callbacks
    # Lease durations are calculated from trusted configuration rather
    # than accepting arbitrary timestamps from the downstream caller.
    # ------------------------------------------------------------------

    def acknowledge_delivery_forwarded(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
    ) -> DeliveryMutationResult:
        self.get_capture(capture_id)
        lease_until = datetime.now(UTC) + timedelta(
            seconds=self.settings.delivery_processing_lease_seconds
        )
        return self._ledger.mark_forwarded(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            lease_until=lease_until,
        )

    def acknowledge_delivery_classifying(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
    ) -> DeliveryMutationResult:
        self.get_capture(capture_id)
        lease_until = datetime.now(UTC) + timedelta(
            seconds=self.settings.delivery_processing_lease_seconds
        )
        return self._ledger.mark_classifying_delivery(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            lease_until=lease_until,
        )

    def renew_delivery_lease(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
    ) -> DeliveryMutationResult:
        self.get_capture(capture_id)
        lease_until = datetime.now(UTC) + timedelta(
            seconds=self.settings.delivery_processing_lease_seconds
        )
        return self._ledger.renew_delivery_lease(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            lease_until=lease_until,
        )

    async def acknowledge_delivery_filed(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        derived_note_path: str,
        git_commit_hash: str | None = None,
        classification_json: dict | None = None,
    ) -> DeliveryMutationResult:
        capture = self.get_capture(capture_id)
        result = self._ledger.mark_filed(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            derived_note_path=derived_note_path,
            git_commit_hash=git_commit_hash,
            classification_json=classification_json,
        )
        if result.outcome in {"changed", "idempotent_replay"}:
            self._ledger.set_system_states({
                "last_vault_write_at": datetime.now(UTC).isoformat(),
                "last_vault_write_capture_id": capture_id,
            })
            try:
                await self.edit_receipt(
                    capture_id=capture_id,
                    content=format_downstream_filed_receipt(
                        capture_id=capture_id,
                        note_path=derived_note_path,
                        has_attachments=capture.has_attachments,
                    ),
                )
            except ReceiptDeliveryError as exc:
                log_metadata(
                    "downstream_filed_receipt_failed",
                    capture_id=capture_id,
                )
                self._ledger.set_receipt_sync_status(
                    capture_id,
                    status="failed",
                    last_attempt_at=datetime.now(UTC).isoformat(),
                    error_type=type(exc).__name__,
                )
        return result

    async def acknowledge_delivery_inbox(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        derived_note_path: str,
        git_commit_hash: str | None = None,
        reason_type: str = "",
        classification_json: dict | None = None,
    ) -> DeliveryMutationResult:
        capture = self.get_capture(capture_id)
        result = self._ledger.mark_inbox(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            derived_note_path=derived_note_path,
            git_commit_hash=git_commit_hash,
            reason_type=reason_type,
            classification_json=classification_json,
        )
        if result.outcome in {"changed", "idempotent_replay"}:
            self._ledger.set_system_states({
                "last_vault_write_at": datetime.now(UTC).isoformat(),
                "last_vault_write_capture_id": capture_id,
            })
            try:
                await self.edit_receipt(
                    capture_id=capture_id,
                    content=format_inbox_receipt(
                        capture_id=capture_id,
                        note_path=derived_note_path,
                        reason=reason_type or None,
                        has_attachments=capture.has_attachments,
                    ),
                )
            except ReceiptDeliveryError as exc:
                log_metadata(
                    "downstream_inbox_receipt_failed",
                    capture_id=capture_id,
                )
                self._ledger.set_receipt_sync_status(
                    capture_id,
                    status="failed",
                    last_attempt_at=datetime.now(UTC).isoformat(),
                    error_type=type(exc).__name__,
                )
        return result

    async def schedule_delivery_retry(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        error_type: str,
        reason_type: str = "webhook_failure",
    ) -> RetryDisposition:
        from secondbrain.delivery import _RETRY_RECEIPT, _FAILED_RECEIPT, _edit_receipt_best_effort
        disposition = self._ledger.schedule_retry(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            now=datetime.now(UTC),
            error_type=error_type,
            reason_type=reason_type,
            max_attempts=self.settings.delivery_retry_max_attempts,
            base_delay_seconds=self.settings.delivery_retry_base_delay_seconds,
            max_delay_seconds=self.settings.delivery_retry_max_delay_seconds,
        )
        if disposition.failed_terminally:
            ok = await _edit_receipt_best_effort(
                self,
                capture_id=capture_id,
                content=_FAILED_RECEIPT.format(capture_id=capture_id),
            )
            if not ok:
                self._ledger.set_receipt_sync_status(
                    capture_id,
                    status="failed",
                    last_attempt_at=datetime.now(UTC).isoformat(),
                    error_type="ReceiptDeliveryError",
                )
        elif disposition.retry_scheduled:
            await _edit_receipt_best_effort(
                self,
                capture_id=capture_id,
                content=_RETRY_RECEIPT.format(capture_id=capture_id),
            )
        return disposition

    async def acknowledge_delivery_failed(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        reason_type: str = "",
    ) -> DeliveryMutationResult:
        from secondbrain.delivery import _FAILED_RECEIPT, _edit_receipt_best_effort
        self.get_capture(capture_id)
        result = self._ledger.mark_delivery_failed_terminally(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            reason=reason_type,
        )
        if result.changed:
            ok = await _edit_receipt_best_effort(
                self,
                capture_id=capture_id,
                content=_FAILED_RECEIPT.format(capture_id=capture_id),
            )
            if not ok:
                self._ledger.set_receipt_sync_status(
                    capture_id,
                    status="failed",
                    last_attempt_at=datetime.now(UTC).isoformat(),
                    error_type="ReceiptDeliveryError",
                )
        return result

    async def report_workflow_error(
        self,
        *,
        capture_id: str,
        delivery_attempt: int,
        disposition: str,
        error_type: str,
        reason_type: str,
        workflow_id: str,
        workflow_name: str,
        execution_id: str | None,
        stage: str,
    ) -> WorkflowErrorOutcome:
        from secondbrain.delivery import _RETRY_RECEIPT, _FAILED_RECEIPT, _edit_receipt_best_effort
        outcome = self._ledger.report_workflow_error(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            disposition=disposition,
            error_type=error_type,
            reason_type=reason_type,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            execution_id=execution_id,
            stage=stage,
            max_attempts=self.settings.delivery_retry_max_attempts,
            base_delay_seconds=self.settings.delivery_retry_base_delay_seconds,
            max_delay_seconds=self.settings.delivery_retry_max_delay_seconds,
        )
        if outcome.outcome == "retry_scheduled":
            await _edit_receipt_best_effort(
                self,
                capture_id=capture_id,
                content=_RETRY_RECEIPT.format(capture_id=capture_id),
            )
        elif outcome.outcome == "terminal_failure":
            ok = await _edit_receipt_best_effort(
                self,
                capture_id=capture_id,
                content=_FAILED_RECEIPT.format(capture_id=capture_id),
            )
            if not ok:
                self._ledger.set_receipt_sync_status(
                    capture_id,
                    status="failed",
                    last_attempt_at=datetime.now(UTC).isoformat(),
                    error_type="ReceiptDeliveryError",
                )
        return outcome

    # ------------------------------------------------------------------
    # SB-117: Clarification handling
    # ------------------------------------------------------------------

    async def record_clarification(self, *, capture_id: str, question: str) -> bool:
        capture = self.get_capture(capture_id)
        recorded = self._ledger.record_clarification(capture_id=capture_id, question=question)
        if recorded:
            log_metadata(
                "clarification_sent",
                capture_id=capture_id,
                discord_message_id=capture.discord_message_id,
            )
            try:
                await self.edit_receipt(
                    capture_id=capture_id,
                    content=_format_clarification_receipt(capture_id=capture_id, question=question),
                )
            except ReceiptDeliveryError:
                pass
        return recorded

    async def resolve_clarification(self, capture_id: str) -> bool:
        resolved = self._ledger.resolve_clarification(capture_id)
        if resolved:
            log_metadata("clarification_resolved", capture_id=capture_id)
        return resolved

    def captures_needing_clarification(self) -> list[CaptureRecord]:
        return self._ledger.captures_needing_clarification()

    def count_needs_clarification(self) -> int:
        return self._ledger.count_needs_clarification()

    def manual_retry_capture(self, *, capture_id: str) -> bool:
        from datetime import UTC, datetime
        try:
            self.get_capture(capture_id)
        except CaptureNotFoundError:
            log_metadata("manual_retry_rejected", capture_id=capture_id, reason="capture_not_found")
            raise
        changed = self._ledger.manual_retry_capture(
            capture_id=capture_id,
            now=datetime.now(UTC),
        )
        if changed:
            log_metadata("manual_retry_requested", capture_id=capture_id)
        else:
            log_metadata("manual_retry_rejected", capture_id=capture_id, reason="invalid_state")
        return changed

    # ------------------------------------------------------------------
    # SB-118: Correction handling
    # ------------------------------------------------------------------

    async def apply_correction(
        self,
        *,
        capture_id: str,
        new_folder: str,
        correction_reason: str,
        new_project: str | None = None,
    ) -> dict | None:
        capture = self.get_capture(capture_id)
        if capture.derived_note_path is None:
            return None

        writer_result = await self._call_writer_move(
            capture_id=capture_id,
            new_folder=new_folder,
            new_project=new_project,
            correction_reason=correction_reason,
        )
        if writer_result is None:
            return None

        move_outcome = "no_op" if writer_result.get("result") == "NO_OP" else "moved"
        correction_id = self._ledger.record_correction(
            capture_id=capture_id,
            old_note_path=writer_result["old_note_path"],
            new_note_path=writer_result["new_note_path"],
            git_commit_hash=writer_result.get("git_commit_hash"),
            correction_reason=correction_reason,
            move_outcome=move_outcome,
        )
        self._ledger.set_system_states({
            "last_vault_write_at": datetime.now(UTC).isoformat(),
            "last_vault_write_capture_id": capture_id,
        })
        log_metadata(
            "correction_applied",
            capture_id=capture_id,
            correction_id=correction_id,
            old_note_path=writer_result["old_note_path"],
            new_note_path=writer_result["new_note_path"],
            move_outcome=move_outcome,
        )

        try:
            await self.edit_receipt(
                capture_id=capture_id,
                content=_format_correction_receipt(
                    capture_id=capture_id,
                    new_note_path=writer_result["new_note_path"],
                ),
            )
        except ReceiptDeliveryError:
            pass

        return {
            "correction_id": correction_id,
            "old_note_path": writer_result["old_note_path"],
            "new_note_path": writer_result["new_note_path"],
            "git_commit_hash": writer_result.get("git_commit_hash"),
            "move_outcome": move_outcome,
        }

    async def _call_writer_move(
        self,
        *,
        capture_id: str,
        new_folder: str,
        new_project: str | None,
        correction_reason: str,
    ) -> dict | None:
        if self._writer_client is None:
            return None
        try:
            return await self._writer_client.move_note(
                capture_id=capture_id,
                new_folder=new_folder,
                new_project=new_project,
                correction_reason=correction_reason,
            )
        except Exception as exc:
            log_metadata(
                "correction_writer_move_failed",
                capture_id=capture_id,
                error_type=type(exc).__name__,
            )
            return None

    async def handle_gateway_correction(self, message) -> bool:
        """Parse and apply a fix: correction command from a Discord message.

        Returns True if a correction was dispatched, False if the message is
        not a valid correction command.
        """
        content = (message.content or "").strip()
        capture_id_or_sentinel, new_folder, reason = _parse_fix_command(content, message)
        if capture_id_or_sentinel is None:
            # Bare unthreaded fix: — rejected per spec
            try:
                await message.reply(
                    "⚠️ I could not identify which capture to update.\n"
                    "Reply directly to a filing receipt with:\n"
                    "`fix: <correction>`\n\n"
                    "Or include the capture ID:\n"
                    "`fix SB-YYYYMMDD-NNNN: <correction>`"
                )
            except Exception as exc:
                log_metadata(
                    "correction_rejection_reply_failed",
                    error_type=type(exc).__name__,
                )

            log_metadata("correction_rejected_unthreaded", reason="no_reply_thread_and_no_explicit_id")
            return False

        # Resolve receipt sentinel to actual capture_id
        if capture_id_or_sentinel.startswith("__receipt__"):
            receipt_message_id = capture_id_or_sentinel[len("__receipt__"):]
            record = self._ledger.get_capture_by_receipt_message_id(receipt_message_id)
            if record is None:
                log_metadata("correction_receipt_not_found", receipt_message_id=receipt_message_id)
                return False
            capture_id = record.capture_id
        else:
            capture_id = capture_id_or_sentinel

        try:
            capture = self.get_capture(capture_id)
        except CaptureNotFoundError:
            log_metadata("correction_capture_not_found", capture_id=capture_id)
            return False

        needs_clarification_resolve = capture.clarification_status == "NEEDS_CLARIFICATION"

        result = await self.apply_correction(
            capture_id=capture_id,
            new_folder=new_folder,
            correction_reason=reason,
        )
        if result is None:
            return False

        if needs_clarification_resolve:
            await self.resolve_clarification(capture_id)

        return True

    # ------------------------------------------------------------------
    # SB-138: Vault update proposal Discord approval surface
    # ------------------------------------------------------------------

    async def post_proposal_approval_message(self, proposal, channel_id: int) -> str | None:
        """Post an approval request message to Discord; return the message ID."""
        if self._receipt_client is None:
            return None
        try:
            channel = self._receipt_client.get_channel(channel_id)
            if channel is None:
                channel = await self._receipt_client.fetch_channel(channel_id)
            text = _format_proposal_approval_message(proposal)
            msg = await channel.send(text)
            return str(msg.id)
        except Exception as exc:
            log_metadata(
                "proposal_approval_message_failed",
                proposal_id=proposal.proposal_id,
                error_type=type(exc).__name__,
            )
            return None

    async def _edit_proposal_message(self, proposal, content: str) -> None:
        """Edit the approval-request Discord message to show final outcome."""
        if self._receipt_client is None or not proposal.approval_message_id:
            return
        try:
            channel_id = int(getattr(self.settings, "discord_capture_channel_id", 0))
            channel = self._receipt_client.get_channel(channel_id)
            if channel is None:
                channel = await self._receipt_client.fetch_channel(channel_id)
            msg = await channel.fetch_message(int(proposal.approval_message_id))
            await msg.edit(content=content)
        except Exception as exc:
            log_metadata(
                "proposal_message_edit_failed",
                proposal_id=proposal.proposal_id,
                error_type=type(exc).__name__,
            )

    async def _handle_proposal_approve(self, proposal_id: str, message) -> None:
        """Handle `approve VUP-...` from Discord: apply the proposal."""
        try:
            proposal = self._ledger.get_proposal(proposal_id)
        except KeyError:
            await _safe_reply(message, f"❌ Proposal `{proposal_id}` not found.")
            return

        from secondbrain.capture_models import (
            PROPOSAL_PENDING, PROPOSAL_APPROVED, PROPOSAL_APPLYING,
            PROPOSAL_APPLIED, PROPOSAL_FAILED, TERMINAL_PROPOSAL_STATUSES,
        )
        if proposal.status in TERMINAL_PROPOSAL_STATUSES:
            await _safe_reply(
                message,
                f"⚠️ Proposal `{proposal_id}` is already closed (status: {proposal.status}).",
            )
            return

        reviewer = str(message.author) if message.author else "discord"
        from datetime import UTC
        now = datetime.now(UTC)

        # Transition: PENDING → APPROVED → APPLYING
        self._ledger.update_proposal(
            proposal_id,
            status=PROPOSAL_APPROVED,
            reviewed_by=reviewer,
            reviewed_at=now,
        )
        self._ledger.update_proposal(proposal_id, status=PROPOSAL_APPLYING)

        # Reload for apply
        proposal = self._ledger.get_proposal(proposal_id)

        # Call writer-service apply
        if self._writer_client is None:
            self._ledger.update_proposal(
                proposal_id,
                status=PROPOSAL_FAILED,
                last_error="writer_service_not_configured",
            )
            await _safe_reply(message, f"❌ Apply failed for `{proposal_id}`: writer-service not configured.")
            return

        try:
            result = await self._writer_client.apply_proposal(proposal_id=proposal_id)
            self._ledger.update_proposal(
                proposal_id,
                status=PROPOSAL_APPLIED,
                applied_at=datetime.now(UTC),
                git_commit_hash=result.get("commit_hash"),
            )
            commit = result.get("commit_hash", "")[:8] if result.get("commit_hash") else "no commit"
            outcome_text = (
                f"✅ Applied — `{proposal_id}`\n"
                f"Operation: `{proposal.operation}`\n"
                f"File: `{result.get('changed_path', proposal.target_note_path)}`\n"
                f"Commit: `{commit}`"
            )
            await _safe_reply(message, outcome_text)
        except Exception as exc:
            error_type = type(exc).__name__
            self._ledger.update_proposal(
                proposal_id,
                status=PROPOSAL_FAILED,
                last_error=error_type,
            )
            await _safe_reply(
                message,
                f"❌ Apply failed for `{proposal_id}` ({error_type}).",
            )
            log_metadata(
                "proposal_apply_failed",
                proposal_id=proposal_id,
                error_type=error_type,
            )

        # Edit the original approval-request message if we have it
        refreshed = self._ledger.get_proposal(proposal_id)
        if refreshed.status == PROPOSAL_APPLIED:
            commit = refreshed.git_commit_hash[:8] if refreshed.git_commit_hash else "no commit"
            await self._edit_proposal_message(
                refreshed,
                f"✅ APPLIED — `{proposal_id}` ({commit})",
            )
        else:
            await self._edit_proposal_message(
                refreshed,
                f"❌ FAILED — `{proposal_id}` ({refreshed.last_error})",
            )

    async def _handle_proposal_reject(self, proposal_id: str, message) -> None:
        """Handle `reject VUP-...` from Discord."""
        try:
            proposal = self._ledger.get_proposal(proposal_id)
        except KeyError:
            await _safe_reply(message, f"❌ Proposal `{proposal_id}` not found.")
            return

        from secondbrain.capture_models import PROPOSAL_REJECTED, TERMINAL_PROPOSAL_STATUSES
        if proposal.status in TERMINAL_PROPOSAL_STATUSES:
            await _safe_reply(
                message,
                f"⚠️ Proposal `{proposal_id}` is already closed (status: {proposal.status}).",
            )
            return

        from datetime import UTC
        reviewer = str(message.author) if message.author else "discord"
        self._ledger.update_proposal(
            proposal_id,
            status=PROPOSAL_REJECTED,
            reviewed_by=reviewer,
            reviewed_at=datetime.now(UTC),
            rejected_reason="User rejected via Discord",
        )
        await _safe_reply(message, f"❌ Rejected — `{proposal_id}`")
        refreshed = self._ledger.get_proposal(proposal_id)
        await self._edit_proposal_message(refreshed, f"❌ REJECTED — `{proposal_id}`")

    async def run_stale_lease_reaper_loop(self) -> None:
        from secondbrain.reaper import run_stale_lease_reaper
        await run_stale_lease_reaper(
            settings=self.settings,
            ledger=self._ledger,
            receipt_client=self,
        )

    def status_snapshot(self) -> CaptureStatusSnapshot:
        counts = self._ledger.status_counts()
        return CaptureStatusSnapshot(
            total_captures=self._ledger.total_captures(),
            filed=counts.get(FILED, 0),
            inbox=counts.get(INBOX, 0),
            rejected_sensitive=counts.get(REJECTED_SENSITIVE, 0),
            failed=counts.get(FAILED, 0),
            last_reconciled_discord_message_id=self.last_reconciled_message_id(),
            last_successful_vault_write=self._ledger.last_successful_vault_write(),
        )

    def close(self) -> None:
        self._ledger.close()

    async def _capture_if_allowed(
        self,
        message,
        *,
        notify_downstream: bool,
    ) -> CaptureDisposition | None:
        if not should_capture_message(message, self.settings):
            return None

        original_text = message.content or ""
        content_for_commands = original_text.strip()

        # Correction and proposal commands are handled by the gateway path.
        # Reconciliation must not persist them as normal captures.
        if _FIX_REPLY_RE.match(content_for_commands) or _FIX_EXPLICIT_RE.match(content_for_commands):
            log_metadata(
                "correction_command_skipped_for_capture",
                discord_message_id=str(message.id),
            )
            return None
        if _APPROVE_VUP_RE.match(content_for_commands) or _REJECT_VUP_RE.match(content_for_commands):
            log_metadata(
                "proposal_command_skipped_for_capture",
                discord_message_id=str(message.id),
            )
            return None

        attachment_metadata = extract_attachment_metadata(message)
        is_attachment_only = not content_for_commands and bool(attachment_metadata)

        if not is_attachment_only:
            secret_result = screen_text(content_for_commands)
            if secret_result.is_sensitive:
                return await self._persist_sensitive_rejection(message, secret_result)

        return await self._persist_accepted_capture(
            message,
            raw_text=original_text,
            attachment_metadata=attachment_metadata,
            notify_downstream=notify_downstream,
        )

    async def _persist_sensitive_rejection(
        self,
        message,
        secret_result,
    ) -> CaptureDisposition:
        result = self._ledger.insert_sensitive_rejection(
            discord_message_id=str(message.id),
            discord_channel_id=str(message.channel.id),
            discord_guild_id=str(message.guild.id),
            discord_author_id=str(message.author.id),
            redacted_text=secret_result.redacted_text,
            sensitivity_flags=secret_result.flags,
        )
        capture = result.capture
        if not result.created:
            self._log_duplicate(capture)
            return CaptureDisposition(
                capture_id=capture.capture_id,
                created=False,
                status=capture.status,
                queued=False,
            )

        try:
            receipt_message_id = await send_rejection_receipt(
                message,
                capture,
                flags=secret_result.flags,
            )
        except Exception as exc:
            log_metadata(
                "rejection_receipt_failed",
                capture_id=capture.capture_id,
                discord_message_id=capture.discord_message_id,
                error_type=type(exc).__name__,
            )
            self._ledger.set_receipt_sync_status(
                capture.capture_id,
                status="failed",
                last_attempt_at=datetime.now(UTC).isoformat(),
                error_type=type(exc).__name__,
            )
        else:
            self._ledger.set_receipt_message_id(capture.capture_id, receipt_message_id)
            self._ledger.set_receipt_sync_status(capture.capture_id, status="not_applicable")

        log_metadata(
            "capture_rejected_sensitive",
            capture_id=capture.capture_id,
            discord_message_id=capture.discord_message_id,
            status_transition=f"NEW->{REJECTED_SENSITIVE}",
        )
        return CaptureDisposition(
            capture_id=capture.capture_id,
            created=True,
            status=capture.status,
            queued=False,
        )

    async def _persist_accepted_capture(
        self,
        message,
        *,
        raw_text: str,
        attachment_metadata: list,
        notify_downstream: bool,
    ) -> CaptureDisposition:
        result = self._ledger.insert_accepted_capture(
            discord_message_id=str(message.id),
            discord_channel_id=str(message.channel.id),
            discord_guild_id=str(message.guild.id),
            discord_author_id=str(message.author.id),
            raw_text=raw_text,
            has_attachments=bool(attachment_metadata),
            attachment_metadata=attachment_metadata,
            initial_delivery_status=self._initial_delivery_status,
        )
        capture = result.capture
        if not result.created:
            self._log_duplicate(capture)
            return CaptureDisposition(
                capture_id=capture.capture_id,
                created=False,
                status=capture.status,
                queued=False,
            )

        receipt_message_id = None
        try:
            receipt_message_id = await send_saved_receipt(
                message,
                capture,
                has_attachments=bool(attachment_metadata),
                downstream_processing_enabled=self.settings.downstream_delivery_enabled,
            )
        except Exception as exc:
            log_metadata(
                "saved_receipt_failed",
                capture_id=capture.capture_id,
                discord_message_id=capture.discord_message_id,
                error_type=type(exc).__name__,
            )
        else:
            self._ledger.set_receipt_message_id(capture.capture_id, receipt_message_id)

        queued = notify_downstream and self._notify_capture is not None
        if queued:
            await self._notify_capture(capture.capture_id)
        elif not notify_downstream:
            log_metadata(
                "capture_deferred",
                capture_id=capture.capture_id,
                discord_message_id=capture.discord_message_id,
                reason="historical_reconciliation_deferred",
            )
        elif not self.settings.downstream_delivery_enabled:
            log_metadata(
                "capture_deferred",
                capture_id=capture.capture_id,
                discord_message_id=capture.discord_message_id,
                reason="downstream processing disabled",
            )

        log_metadata(
            "capture_received",
            capture_id=capture.capture_id,
            discord_message_id=capture.discord_message_id,
            status_transition=f"NEW->{RECEIVED}",
            receipt_message_id=receipt_message_id,
            queued=queued,
        )
        if attachment_metadata:
            log_metadata(
                "capture_has_unarchived_attachments",
                capture_id=capture.capture_id,
                discord_message_id=capture.discord_message_id,
                attachment_count=len(attachment_metadata),
            )
        return CaptureDisposition(
            capture_id=capture.capture_id,
            created=True,
            status=capture.status,
            queued=queued,
        )

    async def _deliver_final_receipt(self, capture: CaptureRecord, content: str) -> ReceiptDeliveryResult:
        if self._receipt_client is None:
            return ReceiptDeliveryResult(delivered=False, replaced=False, receipt_message_id=None)
        try:
            delivery = await deliver_final_receipt(self._receipt_client, capture, content)
        except Exception as exc:
            log_metadata(
                "final_receipt_failed",
                capture_id=capture.capture_id,
                discord_message_id=capture.discord_message_id,
                error_type=type(exc).__name__,
            )
            return ReceiptDeliveryResult(delivered=False, replaced=False, receipt_message_id=None)

        if delivery.replaced and delivery.receipt_message_id is not None:
            self._ledger.update_capture(
                capture.capture_id,
                receipt_message_id=delivery.receipt_message_id,
                event_type="RECEIPT_REPLACED",
                event_payload={
                    "old_receipt_message_id": capture.receipt_message_id,
                    "new_receipt_message_id": delivery.receipt_message_id,
                    "reason": delivery.replacement_reason,
                },
            )
        return delivery

    def _transition_capture(
        self,
        capture_id: str,
        *,
        from_statuses: set[str],
        to_status: str,
        event_type: str,
        event_payload: dict | None = None,
        classification_json: dict | None | object = UNSET,
        derived_note_path: str | None | object = UNSET,
        last_error: str | None | object = UNSET,
        replay_payload: dict | None = None,
    ) -> TransitionResult:
        capture = self.get_capture(capture_id)
        if capture.status == to_status:
            if replay_payload is not None and not self._replay_payload_matches(capture, replay_payload):
                raise ConflictingReplayError("conflicting replay payload")
            return TransitionResult(
                capture_id=capture.capture_id,
                previous_status=capture.status,
                status=capture.status,
                changed=False,
            )

        if capture.status not in from_statuses:
            raise InvalidCaptureTransitionError("invalid capture transition")

        # In local-full mode, terminal transitions must not leave delivery_status pending forward
        delivery_status_override = None
        if to_status in TERMINAL_STATUSES and self._initial_delivery_status == NOT_APPLICABLE:
            delivery_status_override = NOT_APPLICABLE

        result = self._ledger.transition_capture(
            capture_id,
            from_statuses=from_statuses,
            to_status=to_status,
            classification_json=classification_json,
            derived_note_path=derived_note_path,
            last_error=last_error,
            event_type=event_type,
            event_payload=event_payload,
            delivery_status=delivery_status_override,
        )
        if result is None:
            raise InvalidCaptureTransitionError("invalid capture transition")
        return result

    def _replay_payload_matches(self, capture: CaptureRecord, replay_payload: dict) -> bool:
        if "classification_json" in replay_payload:
            if self._ledger.capture_classification_json(capture.capture_id) != replay_payload["classification_json"]:
                return False
        if "derived_note_path" in replay_payload and capture.derived_note_path != replay_payload["derived_note_path"]:
            return False
        if "last_error" in replay_payload and capture.last_error != replay_payload["last_error"]:
            return False
        return True

    @staticmethod
    def _log_duplicate(capture: CaptureRecord) -> None:
        log_metadata(
            "duplicate_capture_ignored",
            capture_id=capture.capture_id,
            discord_message_id=capture.discord_message_id,
            status=capture.status,
        )


async def _safe_reply(message, content: str) -> None:
    """Reply to a Discord message, swallowing errors."""
    try:
        await message.channel.send(content)
    except Exception as exc:
        from secondbrain.observability import log_metadata as _log
        _log("proposal_reply_failed", error_type=type(exc).__name__)


def _format_proposal_approval_message(proposal) -> str:
    import json as _json
    try:
        change = _json.loads(proposal.change_json)
    except Exception:
        change = {}

    lines = [
        f"📋 {proposal.proposal_id} — vault update proposal",
        "",
        f"Operation:  {proposal.operation}",
        f"File:       {proposal.target_note_path}",
    ]
    if change:
        for k, v in change.items():
            lines.append(f"{k.capitalize()}: {v!r}")
    if proposal.reason:
        lines.append(f"Reason:     {proposal.reason}")
    lines += [
        "",
        "Reply with:",
        f"  approve {proposal.proposal_id}  — to apply this change",
        f"  reject {proposal.proposal_id}   — to discard without applying",
    ]
    return "\n".join(lines)


def _safe_inbox_reason_type(reason: str | None) -> str:
    if not reason:
        return "unspecified"
    if reason.startswith("classifier failed:"):
        return "classifier_failure"
    if reason == "classification confidence below threshold":
        return "low_confidence"
    if reason == "classification needs clarification":
        return "needs_clarification"
    if reason == "classifier selected inbox":
        return "classifier_selected_inbox"
    if reason.startswith("attachment-only capture"):
        return "attachment_only"
    return "other"


def _safe_inbox_error_type(reason: str | None) -> str | None:
    if not reason or not reason.startswith("classifier failed:"):
        return None
    parts = reason.split(":", maxsplit=2)
    if len(parts) < 2:
        return "ClassifierError"
    return parts[1].strip() or "ClassifierError"


# Matches: fix SB-YYYYMMDD-NNNN: <reason including folder>
_FIX_EXPLICIT_RE = re.compile(
    r"^fix\s+(SB-\d{8}-\d{4})\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL
)
# Matches: fix: <reason including folder> (reply-to-receipt form)
_FIX_REPLY_RE = re.compile(r"^fix\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)

# SB-138: Vault update proposal approval / rejection patterns
_APPROVE_VUP_RE = re.compile(r"^approve\s+(VUP-\d{8}-\d{4})\s*$", re.IGNORECASE)
_REJECT_VUP_RE = re.compile(r"^reject\s+(VUP-\d{8}-\d{4})\s*$", re.IGNORECASE)

# Simple keyword-to-folder mapping used when parsing a correction reason
_FOLDER_KEYWORDS = {
    "inbox": "inbox",
    "people": "people",
    "project": "projects",
    "projects": "projects",
    "idea": "ideas",
    "ideas": "ideas",
    "learning": "learning",
    "admin": "admin",
}


def _infer_folder_from_reason(reason: str) -> str:
    lower = reason.lower()
    for keyword, folder in _FOLDER_KEYWORDS.items():
        if keyword in lower:
            return folder
    return "inbox"


def _parse_fix_command(
    content: str,
    message,
) -> tuple[str | None, str, str]:
    """Return (capture_id, new_folder, reason) or (None, ...) if not a valid fix command."""
    explicit = _FIX_EXPLICIT_RE.match(content)
    if explicit:
        capture_id = explicit.group(1)
        reason = explicit.group(2).strip()
        folder = _infer_folder_from_reason(reason)
        return capture_id, folder, reason

    reply_match = _FIX_REPLY_RE.match(content)
    if reply_match:
        # Must be a reply to a receipt message (reference field set)
        referenced_id = _referenced_message_id(message)
        if referenced_id is None:
            # Bare unthreaded fix: — reject
            return None, "", ""
        reason = reply_match.group(1).strip()
        folder = _infer_folder_from_reason(reason)
        # Return the referenced message id as a sentinel; caller resolves capture
        return f"__receipt__{referenced_id}", folder, reason

    return None, "", ""


def _referenced_message_id(message) -> str | None:
    ref = getattr(message, "reference", None)
    if ref is None:
        return None
    return str(ref.message_id) if ref.message_id else None


def _format_clarification_receipt(*, capture_id: str, question: str) -> str:
    return (
        f"**Needs clarification** (`{capture_id}`)\n"
        f"Filed to inbox. {question}\n"
        "Reply to this message with your answer to re-classify."
    )


def _format_correction_receipt(*, capture_id: str, new_note_path: str) -> str:
    return (
        f"**Correction applied** (`{capture_id}`)\n"
        f"Note moved to: `{new_note_path}`"
    )


def _safe_failure_error_type(reason: str) -> str:
    if ": " not in reason:
        return "CaptureFailure"
    prefix = reason.split(":", maxsplit=1)[0].strip()
    # Sanitized format: "{ExceptionType}: {description}" — prefix is a Python identifier
    if prefix.isidentifier():
        return prefix
    return "CaptureFailure"


_WRITER_TOKEN_HEADER = "X-Second-Brain-Writer-Token"


class WriterServiceClient:
    """Calls writer-service directly for note operations."""

    def __init__(self, *, url: str, token: str, timeout_seconds: int = 30) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds

    async def move_note(
        self,
        *,
        capture_id: str,
        new_folder: str,
        new_project: str | None,
        correction_reason: str,
    ) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._url}/internal/notes/move",
                json={
                    "capture_id": capture_id,
                    "new_folder": new_folder,
                    "new_project": new_project,
                    "correction_reason": correction_reason,
                },
                headers={_WRITER_TOKEN_HEADER: self._token},
            )
        response.raise_for_status()
        return response.json()

    async def apply_proposal(self, *, proposal_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._url}/internal/vault/apply-proposal",
                json={"proposal_id": proposal_id},
                headers={_WRITER_TOKEN_HEADER: self._token},
            )
        response.raise_for_status()
        return response.json()
