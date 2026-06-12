from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from secondbrain.capture_models import CaptureRecord
from secondbrain.models import Classification
from secondbrain.observability import log_metadata


ATTACHMENT_WARNING = "⚠️ Attachment detected but not archived in the MVP."


@dataclass(frozen=True)
class ReceiptDeliveryResult:
    delivered: bool
    replaced: bool
    receipt_message_id: str | None


async def send_saved_receipt(
    message,
    capture: CaptureRecord,
    *,
    has_attachments: bool,
    downstream_processing_enabled: bool = True,
) -> str:
    receipt = await message.channel.send(
        format_saved_receipt(
            capture,
            has_attachments=has_attachments,
            downstream_processing_enabled=downstream_processing_enabled,
        )
    )
    return str(receipt.id)


async def send_rejection_receipt(message, capture: CaptureRecord, *, flags: tuple[str, ...]) -> str:
    receipt = await message.channel.send(format_sensitive_rejection_receipt())
    return str(receipt.id)


async def edit_final_receipt(client, capture: CaptureRecord, content: str) -> None:
    if not capture.receipt_message_id:
        return

    channel = client.get_channel(int(capture.discord_channel_id))
    if channel is None:
        channel = await client.fetch_channel(int(capture.discord_channel_id))

    receipt = await channel.fetch_message(int(capture.receipt_message_id))
    await receipt.edit(content=content)


async def send_replacement_final_receipt(client, capture: CaptureRecord, content: str) -> str:
    channel = await _receipt_channel(client, capture)
    receipt = await channel.send(content)
    return str(receipt.id)


async def deliver_final_receipt(
    client: Any,
    capture: CaptureRecord,
    content: str,
) -> ReceiptDeliveryResult:
    if capture.receipt_message_id:
        try:
            await edit_final_receipt(client, capture, content)
            return ReceiptDeliveryResult(
                delivered=True,
                replaced=False,
                receipt_message_id=capture.receipt_message_id,
            )
        except Exception as exc:
            log_metadata(
                "receipt_edit_failed",
                capture_id=capture.capture_id,
                discord_message_id=capture.discord_message_id,
                error_type=type(exc).__name__,
            )

    try:
        replacement_receipt_message_id = await send_replacement_final_receipt(client, capture, content)
    except Exception as exc:
        log_metadata(
            "replacement_receipt_failed",
            capture_id=capture.capture_id,
            discord_message_id=capture.discord_message_id,
            error_type=type(exc).__name__,
        )
        return ReceiptDeliveryResult(delivered=False, replaced=False, receipt_message_id=None)

    return ReceiptDeliveryResult(
        delivered=True,
        replaced=True,
        receipt_message_id=replacement_receipt_message_id,
    )


def format_saved_receipt(
    capture: CaptureRecord,
    *,
    has_attachments: bool,
    downstream_processing_enabled: bool = True,
) -> str:
    if downstream_processing_enabled:
        content = (
            f"⏳ {capture.capture_id} received.\n"
            "Your note is safely captured.\n"
            "Queued for downstream filing."
        )
    else:
        content = (
            f"⏳ {capture.capture_id} received.\n"
            "Your note is safely captured.\n"
            "Downstream filing is not enabled yet."
        )
    if has_attachments:
        content += f"\n{ATTACHMENT_WARNING}"
    return content


def format_filed_receipt(
    *,
    capture_id: str,
    note_path: str,
    classification: Classification,
    has_attachments: bool,
) -> str:
    content = (
        f"✅ {capture_id} filed.\n"
        f"Location: {_location_from_note_path(note_path)}\n"
        f"Type: {classification.note_type}\n"
        f"Tags: {_tag_list(classification.tags)}"
    )
    if has_attachments:
        content += f"\n{ATTACHMENT_WARNING}"
    return content


def format_inbox_receipt(
    *,
    capture_id: str,
    note_path: str,
    reason: str | None,
    has_attachments: bool,
) -> str:
    content = (
        f"⚠️ {capture_id} saved to {_top_level_folder(note_path)}.\n"
        f"Reason: {_inbox_reason_text(reason)}"
    )
    if has_attachments:
        content += f"\n{ATTACHMENT_WARNING}"
    return content


def format_sensitive_rejection_receipt() -> str:
    return (
        "⚠️ Message rejected.\n"
        "It appears to contain a credential or sensitive identifier.\n"
        "The original text was not saved or sent to Gemini."
    )


def format_downstream_filed_receipt(
    *,
    capture_id: str,
    note_path: str,
    has_attachments: bool,
) -> str:
    content = (
        f"✅ {capture_id} filed.\n"
        f"Location: {_location_from_note_path(note_path)}"
    )
    if has_attachments:
        content += f"\n{ATTACHMENT_WARNING}"
    return content


def format_delivery_retry_scheduled_receipt(
    capture_id: str,
    *,
    retry_attempts: int,
    next_attempt_at: datetime,
) -> str:
    return (
        f"⚠️ {capture_id} captured, but downstream processing was interrupted.\n"
        "Your original note is safe.\n"
        f"Automatic retry {retry_attempts} is scheduled."
    )


def format_delivery_retry_exhausted_receipt(capture_id: str) -> str:
    return (
        f"❌ {capture_id} captured, but filing failed after repeated retries.\n"
        "Your original note is safe in the local ledger.\n"
        "Manual retry is available."
    )


def format_stub_filed_receipt(capture_id: str, *, has_attachments: bool) -> str:
    content = (
        f"✅ {capture_id} filed (stub).\n"
        "Vault write is not yet enabled. Note is durably captured in the ledger."
    )
    if has_attachments:
        content += f"\n{ATTACHMENT_WARNING}"
    return content


def format_stub_inbox_receipt(capture_id: str, *, has_attachments: bool) -> str:
    content = (
        f"⚠️ {capture_id} saved to inbox (stub).\n"
        "Vault write is not yet enabled. Note is durably captured in the ledger."
    )
    if has_attachments:
        content += f"\n{ATTACHMENT_WARNING}"
    return content


def format_manual_retry_accepted_receipt(capture_id: str) -> str:
    return (
        f"⏳ {capture_id} queued for manual retry.\n"
        "Your original note remains safe in the local ledger."
    )


def format_vault_failure_receipt(capture_id: str, *, has_attachments: bool) -> str:
    content = (
        f"❌ {capture_id} captured but vault filing failed.\n"
        "Your original note is safe in the local ledger."
    )
    if has_attachments:
        content += f"\n{ATTACHMENT_WARNING}"
    return content


async def _receipt_channel(client: Any, capture: CaptureRecord):
    channel_id = int(capture.discord_channel_id)
    channel = None
    get_channel = getattr(client, "get_channel", None)
    if get_channel is not None:
        channel = get_channel(channel_id)
    if channel is None:
        channel = await client.fetch_channel(channel_id)
    return channel


def _location_from_note_path(note_path: str) -> str:
    parent = PurePosixPath(note_path).parent
    if str(parent) == ".":
        return note_path
    return " / ".join(parent.parts)


def _top_level_folder(note_path: str) -> str:
    parts = PurePosixPath(note_path).parts
    return parts[0] if parts else "00_inbox"


def _tag_list(tags: list[str]) -> str:
    return ", ".join(tags) if tags else "none"


def _inbox_reason_text(reason: str | None) -> str:
    if not reason:
        return "classification was uncertain."
    if reason.startswith("classifier failed:"):
        return "automatic classification failed. Your note is safe."
    if reason in {
        "classification confidence below threshold",
        "classification needs clarification",
        "classifier selected inbox",
    }:
        return "classification was uncertain."
    return reason
