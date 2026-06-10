from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

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
        await self._capture_if_allowed(message, notify_downstream=True)

    async def startup_reconcile(self, client: Any) -> ReconcileResult:
        return await reconcile_discord_history(
            client=client,
            settings=self.settings,
            ledger=self._ledger,
            handle_capture=self.make_capture_handler(notify_downstream=False),
            mode="startup",
            scan_limit=self.settings.startup_reconcile_limit,
        )

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
            except ReceiptDeliveryError:
                pass

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
            except ReceiptDeliveryError:
                pass

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
            except ReceiptDeliveryError:
                pass

    def get_capture(self, capture_id: str) -> CaptureRecord:
        try:
            return self._ledger.get_capture(capture_id)
        except KeyError as exc:
            raise CaptureNotFoundError("capture not found") from exc

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
    ) -> DeliveryMutationResult:
        capture = self.get_capture(capture_id)
        result = self._ledger.mark_filed(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            derived_note_path=derived_note_path,
            git_commit_hash=git_commit_hash,
        )
        if result.outcome in {"changed", "idempotent_replay"}:
            try:
                await self.edit_receipt(
                    capture_id=capture_id,
                    content=format_downstream_filed_receipt(
                        capture_id=capture_id,
                        note_path=derived_note_path,
                        has_attachments=capture.has_attachments,
                    ),
                )
            except ReceiptDeliveryError:
                log_metadata(
                    "downstream_filed_receipt_failed",
                    capture_id=capture_id,
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
    ) -> DeliveryMutationResult:
        capture = self.get_capture(capture_id)
        result = self._ledger.mark_inbox(
            capture_id=capture_id,
            delivery_attempt=delivery_attempt,
            derived_note_path=derived_note_path,
            git_commit_hash=git_commit_hash,
            reason_type=reason_type,
        )
        if result.outcome in {"changed", "idempotent_replay"}:
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
            except ReceiptDeliveryError:
                log_metadata(
                    "downstream_inbox_receipt_failed",
                    capture_id=capture_id,
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
            await _edit_receipt_best_effort(
                self,
                capture_id=capture_id,
                content=_FAILED_RECEIPT.format(capture_id=capture_id),
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
            await _edit_receipt_best_effort(
                self,
                capture_id=capture_id,
                content=_FAILED_RECEIPT.format(capture_id=capture_id),
            )
        return result

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

        raw_text = message.content.strip() if message.content else ""
        secret_result = screen_text(raw_text)
        if secret_result.is_sensitive:
            return await self._persist_sensitive_rejection(message, secret_result)

        return await self._persist_accepted_capture(
            message,
            raw_text=raw_text,
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
        else:
            self._ledger.set_receipt_message_id(capture.capture_id, receipt_message_id)

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
        notify_downstream: bool,
    ) -> CaptureDisposition:
        attachment_metadata = extract_attachment_metadata(message)
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
                downstream_processing_enabled=self._notify_capture is not None,
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
        elif self._notify_capture is None:
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


def _safe_failure_error_type(reason: str) -> str:
    if ": " not in reason:
        return "CaptureFailure"
    prefix = reason.split(":", maxsplit=1)[0]
    if prefix in {"vault write failed", "worker error"}:
        parts = reason.split(":", maxsplit=2)
        if len(parts) >= 2:
            return parts[1].strip() or "CaptureFailure"
    return "CaptureFailure"
