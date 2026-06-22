"""Tests for Tech Debt Milestone TD-04 (SB-142 through SB-148)."""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from secondbrain.capture_models import CLASSIFYING, FAILED, FILED, INBOX, RECEIVED
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger
from secondbrain.models import Classification
from secondbrain.sqlite_runtime import SQLiteRuntime

from tests.fakes.discord import (
    FakeDiscordChannel,
    FakeDiscordClient,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ROOT = Path(".")
_INTAKE_FIXTURE = _ROOT / "n8n" / "workflows" / "second-brain-intake.json"


def _intake() -> dict:
    return json.loads(_INTAKE_FIXTURE.read_text())


def _make_settings(tmp_path, **overrides):
    data = {
        "discord_guild_id": 100,
        "discord_capture_channel_id": 200,
        "discord_allowed_user_id": 300,
        "startup_reconcile_limit": 10,
        "ledger_path": tmp_path / "runtime" / "ledger.sqlite3",
        "vault_path": tmp_path / "vault",
        "downstream_delivery_enabled": False,
        "delivery_retry_max_attempts": 3,
        "delivery_retry_base_delay_seconds": 1,
        "delivery_retry_max_delay_seconds": 10,
        "delivery_forward_lease_seconds": 60,
        "delivery_processing_lease_seconds": 300,
        "delivery_dispatch_interval_seconds": 2,
        "delivery_dispatch_batch_size": 25,
        "delivery_reaper_interval_seconds": 30,
        "delivery_reaper_batch_size": 100,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _make_classification(**overrides):
    data = {
        "folder": "projects",
        "project": "halo",
        "note_type": "task",
        "title": "Test note",
        "tags": [],
        "body": "body",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.9,
    }
    data.update(overrides)
    return Classification.model_validate(data)


def _insert_classifying(ledger):
    result = ledger.insert_accepted_capture(
        discord_message_id="111",
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        raw_text="test capture",
    )
    ledger.mark_classifying(result.capture.capture_id)
    return ledger.get_capture(result.capture.capture_id)


# ===========================================================================
# SB-142 — Durable receipt-repair tracking
# ===========================================================================


class _FailingEditChannel(FakeDiscordChannel):
    """Channel whose receipt edits always fail after initial send."""

    async def send(self, content):
        from types import SimpleNamespace
        self.sent_receipts.append((9001, content))
        return SimpleNamespace(id=9001)

    async def fetch_message(self, message_id):
        from tests.fakes.discord import FakeReceiptMessage
        msg = FakeReceiptMessage(int(message_id), "old", self)
        self.messages[int(message_id)] = msg
        return msg


def test_sb142_migration_adds_receipt_sync_columns(tmp_path):
    """Migration 8 adds receipt_sync_* columns with correct defaults."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    cols = ledger._runtime.read(
        lambda conn: [r["name"] for r in conn.execute("PRAGMA table_info(captures)").fetchall()]
    )
    assert "receipt_sync_status" in cols
    assert "receipt_sync_last_attempt_at" in cols
    assert "receipt_sync_last_error_type" in cols
    ledger.close()


def test_sb142_new_capture_defaults_to_clean_receipt_sync(tmp_path):
    """Newly inserted captures default to receipt_sync_status='clean'."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    result = ledger.insert_accepted_capture(
        discord_message_id="1",
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        raw_text="hello",
    )
    capture = result.capture
    assert capture.receipt_sync_status == "clean"
    assert capture.receipt_sync_last_attempt_at is None
    assert capture.receipt_sync_last_error_type is None
    ledger.close()


def test_sb142_set_receipt_sync_status_failed(tmp_path):
    """set_receipt_sync_status writes 'failed' and does not change capture status."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    result = ledger.insert_accepted_capture(
        discord_message_id="1",
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        raw_text="hello",
    )
    capture_id = result.capture.capture_id
    now_iso = datetime.now(UTC).isoformat()

    ledger.set_receipt_sync_status(
        capture_id,
        status="failed",
        last_attempt_at=now_iso,
        error_type="ReceiptDeliveryError",
    )

    updated = ledger.get_capture(capture_id)
    assert updated.receipt_sync_status == "failed"
    assert updated.receipt_sync_last_attempt_at == now_iso
    assert updated.receipt_sync_last_error_type == "ReceiptDeliveryError"
    assert updated.status == RECEIVED  # capture status must not change
    ledger.close()


def test_sb142_set_receipt_sync_status_clean_clears_error(tmp_path):
    """Setting receipt_sync_status='clean' clears the error fields."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    result = ledger.insert_accepted_capture(
        discord_message_id="1",
        discord_channel_id="200",
        discord_guild_id="100",
        discord_author_id="300",
        raw_text="hello",
    )
    capture_id = result.capture.capture_id

    ledger.set_receipt_sync_status(
        capture_id,
        status="failed",
        last_attempt_at=datetime.now(UTC).isoformat(),
        error_type="SomeError",
    )
    ledger.set_receipt_sync_status(capture_id, status="clean", error_type=None)

    updated = ledger.get_capture(capture_id)
    assert updated.receipt_sync_status == "clean"
    assert updated.receipt_sync_last_error_type is None
    ledger.close()


def test_sb142_get_out_of_sync_receipts_returns_failed_only(tmp_path):
    """get_out_of_sync_receipts returns captures where status != 'clean'/'not_applicable'."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    r1 = ledger.insert_accepted_capture(
        discord_message_id="1", discord_channel_id="200",
        discord_guild_id="100", discord_author_id="300", raw_text="a",
    )
    r2 = ledger.insert_accepted_capture(
        discord_message_id="2", discord_channel_id="200",
        discord_guild_id="100", discord_author_id="300", raw_text="b",
    )
    r3 = ledger.insert_accepted_capture(
        discord_message_id="3", discord_channel_id="200",
        discord_guild_id="100", discord_author_id="300", raw_text="c",
    )

    ledger.set_receipt_sync_status(r1.capture.capture_id, status="failed", error_type="E")
    ledger.set_receipt_sync_status(r2.capture.capture_id, status="not_applicable")
    # r3 stays clean

    out_of_sync = ledger.get_out_of_sync_receipts()
    ids = [c.capture_id for c in out_of_sync]
    assert r1.capture.capture_id in ids
    assert r2.capture.capture_id not in ids
    assert r3.capture.capture_id not in ids
    ledger.close()


@pytest.mark.asyncio
async def test_sb142_successful_receipt_sets_clean(tmp_path):
    """Successful edit_receipt call sets receipt_sync_status='clean'."""
    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_classifying(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")

    channel = FakeDiscordChannel()
    channel.messages[9001] = _EditableFakeReceipt(9001, channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=FakeDiscordClient(channel))

    await service.complete_filed(
        capture_id=capture.capture_id,
        classification=_make_classification(),
        note_path="20_projects/halo/test.md",
    )

    updated = ledger.get_capture(capture.capture_id)
    assert updated.status == FILED
    assert updated.receipt_sync_status == "clean"
    ledger.close()


class _AlwaysFailingReceiptClient:
    """Receipt client that fails both edit and replacement send."""

    def get_channel(self, channel_id):
        return self

    async def fetch_channel(self, channel_id):
        return self

    async def fetch_message(self, message_id):
        return self

    async def edit(self, *, content):
        raise RuntimeError("simulated receipt edit failure")

    async def send(self, content):
        raise RuntimeError("simulated replacement send failure")


@pytest.mark.asyncio
async def test_sb142_receipt_failure_sets_failed_and_does_not_change_capture_status(tmp_path):
    """Receipt delivery failure sets 'failed' sync status; committed state is unchanged."""
    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_classifying(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")

    service = CaptureService(
        settings=settings,
        ledger=ledger,
        receipt_client=_AlwaysFailingReceiptClient(),
    )

    await service.complete_filed(
        capture_id=capture.capture_id,
        classification=_make_classification(),
        note_path="20_projects/halo/test.md",
    )

    updated = ledger.get_capture(capture.capture_id)
    assert updated.status == FILED  # committed state unchanged
    assert updated.receipt_sync_status == "failed"
    assert updated.receipt_sync_last_error_type is not None
    assert updated.receipt_sync_last_attempt_at is not None
    ledger.close()


class _ToggleFailingReceiptClient:
    """Receipt client that fails on first call and succeeds on subsequent calls."""

    def __init__(self):
        self._fail_next = True
        self._message_content: str | None = None

    def get_channel(self, channel_id):
        return self

    async def fetch_channel(self, channel_id):
        return self

    async def fetch_message(self, message_id):
        return self

    async def edit(self, *, content):
        if self._fail_next:
            raise RuntimeError("simulated edit failure")
        self._message_content = content

    async def send(self, content):
        if self._fail_next:
            raise RuntimeError("simulated send failure")
        self._message_content = content
        return SimpleNamespace(id=9002)


@pytest.mark.asyncio
async def test_sb142_receipt_repair_resets_to_clean(tmp_path):
    """A late successful receipt delivery after prior failure resets to 'clean'."""
    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    capture = _insert_classifying(ledger)
    ledger.set_receipt_message_id(capture.capture_id, "9001")

    receipt_client = _ToggleFailingReceiptClient()
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=receipt_client)

    await service.complete_filed(
        capture_id=capture.capture_id,
        classification=_make_classification(),
        note_path="20_projects/halo/test.md",
    )

    updated = ledger.get_capture(capture.capture_id)
    assert updated.receipt_sync_status == "failed"

    # Simulate a repair: allow the client to succeed and call edit_receipt again
    receipt_client._fail_next = False
    ledger.set_receipt_message_id(capture.capture_id, "9002")
    await service.edit_receipt(capture_id=capture.capture_id, content="repaired ✅")

    repaired = ledger.get_capture(capture.capture_id)
    assert repaired.receipt_sync_status == "clean"
    ledger.close()


def test_sb142_status_output_includes_out_of_sync_receipts(tmp_path):
    """format_operational_status surfaces out-of-sync receipts."""
    from secondbrain.status import StatusSettings, read_operational_status, format_operational_status

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    result = ledger.insert_accepted_capture(
        discord_message_id="1", discord_channel_id="200",
        discord_guild_id="100", discord_author_id="300", raw_text="hello",
    )
    capture_id = result.capture.capture_id
    ledger.set_receipt_sync_status(
        capture_id, status="failed",
        last_attempt_at="2026-06-21T10:00:00+00:00",
        error_type="ReceiptDeliveryError",
    )
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    output = format_operational_status(snapshot)

    assert "Out-of-sync receipts" in output
    assert capture_id in output
    assert "failed" in output


def test_sb142_status_output_omits_clean_receipts(tmp_path):
    """format_operational_status omits captures with clean receipt sync."""
    from secondbrain.status import StatusSettings, read_operational_status, format_operational_status

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.insert_accepted_capture(
        discord_message_id="1", discord_channel_id="200",
        discord_guild_id="100", discord_author_id="300", raw_text="hello",
    )
    ledger.close()

    settings = StatusSettings(
        ledger_path=tmp_path / "ledger.sqlite3",
        vault_path=None,
        status_timezone="UTC",
        capture_service_health_stale_after_seconds=60,
    )
    snapshot = read_operational_status(settings=settings)
    output = format_operational_status(snapshot)

    assert "Out-of-sync receipts" not in output


# Helper for receipt tests
class _EditableFakeReceipt:
    def __init__(self, message_id, channel):
        self.id = message_id
        self.channel = channel

    async def edit(self, *, content):
        self.channel.edit_attempts += 1
        if self.channel.fail_receipt_edit:
            raise RuntimeError("simulated edit failure")


# ===========================================================================
# SB-143 — n8n intake: Gemini error class split
# ===========================================================================


def test_sb143_intake_has_gemini_error_switch_node():
    """Intake workflow has a Switch node for Gemini error branching."""
    wf = _intake()
    switch_nodes = [n for n in wf["nodes"] if n.get("type") == "n8n-nodes-base.switch"]
    switch_names = [n["name"] for n in switch_nodes]
    assert any("Gemini" in name for name in switch_names), (
        f"Expected a Gemini error Switch node, found switch nodes: {switch_names}"
    )


def test_sb143_gemini_switch_has_rate_limited_branch():
    """Gemini error switch routes 429 to a rate-limited retry node."""
    wf = _intake()
    fixture_text = _INTAKE_FIXTURE.read_text()
    assert "gemini_rate_limited" in fixture_text, (
        "Expected 'gemini_rate_limited' reason_type in workflow"
    )


def test_sb143_gemini_switch_has_server_error_branch():
    """Gemini error switch routes server errors to a server-error retry node."""
    fixture_text = _INTAKE_FIXTURE.read_text()
    assert "gemini_server_or_timeout" in fixture_text, (
        "Expected 'gemini_server_or_timeout' reason_type in workflow"
    )


def test_sb143_gemini_auth_failure_routes_to_acknowledge_failed():
    """Gemini auth failure (401/403) routes to acknowledge-failed, not schedule-retry."""
    wf = _intake()
    fixture_text = _INTAKE_FIXTURE.read_text()
    assert "gemini_auth_failed" in fixture_text, (
        "Expected 'gemini_auth_failed' reason_type in workflow"
    )
    # Must call acknowledge-failed (not schedule-retry) for auth failures
    ack_auth_nodes = [
        n for n in wf["nodes"]
        if "gemini_auth_failed" in json.dumps(n.get("parameters", {}))
    ]
    assert len(ack_auth_nodes) >= 1
    for node in ack_auth_nodes:
        url = node["parameters"].get("url", "")
        assert "acknowledge-failed" in url, (
            f"Auth failure node must call acknowledge-failed endpoint, got: {url!r}"
        )


def test_sb143_gemini_rate_limited_routes_to_schedule_retry():
    """Rate-limited branch (429) routes to schedule-retry."""
    wf = _intake()
    rate_limited_nodes = [
        n for n in wf["nodes"]
        if "gemini_rate_limited" in json.dumps(n.get("parameters", {}))
    ]
    assert len(rate_limited_nodes) >= 1
    for node in rate_limited_nodes:
        url = node["parameters"].get("url", "")
        assert "schedule-retry" in url, (
            f"Rate-limited node must call schedule-retry endpoint, got: {url!r}"
        )


def test_sb143_gemini_server_error_routes_to_schedule_retry():
    """Server error branch (5xx) routes to schedule-retry."""
    wf = _intake()
    server_nodes = [
        n for n in wf["nodes"]
        if "gemini_server_or_timeout" in json.dumps(n.get("parameters", {}))
    ]
    assert len(server_nodes) >= 1
    for node in server_nodes:
        url = node["parameters"].get("url", "")
        assert "schedule-retry" in url, (
            f"Server error node must call schedule-retry endpoint, got: {url!r}"
        )


# ===========================================================================
# SB-144 — n8n intake: attachment-only capture bypass
# ===========================================================================


def test_sb144_intake_has_attachment_only_if_node():
    """Intake workflow has an 'Attachment Only?' If node."""
    wf = _intake()
    names = [n["name"] for n in wf["nodes"]]
    assert "Attachment Only?" in names, (
        f"Expected 'Attachment Only?' node in workflow, found: {names}"
    )


def test_sb144_attachment_only_node_is_before_screen_for_sensitive():
    """Get Capture → Attachment Only? (not directly to Screen for Sensitive)."""
    wf = _intake()
    conns = wf["connections"]
    get_capture_targets = [
        c["node"]
        for entry in conns.get("Get Capture", {}).get("main", [])
        for c in entry
    ]
    assert "Attachment Only?" in get_capture_targets, (
        f"Get Capture must route through Attachment Only?, got: {get_capture_targets}"
    )
    assert "Screen for Sensitive" not in get_capture_targets, (
        "Get Capture must not route directly to Screen for Sensitive (bypass via Attachment Only?)"
    )


def test_sb144_attachment_only_true_branch_skips_screen():
    """Attachment Only? true branch goes to Claim Attempt (not Screen for Sensitive)."""
    wf = _intake()
    conns = wf["connections"]
    attachment_only_conns = conns.get("Attachment Only?", {}).get("main", [])
    assert len(attachment_only_conns) >= 2

    true_branch_targets = [c["node"] for c in attachment_only_conns[0]]
    assert "Claim Attempt" in true_branch_targets, (
        f"Attachment Only? true branch must go to Claim Attempt, got: {true_branch_targets}"
    )
    assert "Screen for Sensitive" not in true_branch_targets


def test_sb144_attachment_only_false_branch_goes_to_screen():
    """Attachment Only? false branch still goes to Screen for Sensitive."""
    wf = _intake()
    conns = wf["connections"]
    attachment_only_conns = conns.get("Attachment Only?", {}).get("main", [])
    assert len(attachment_only_conns) >= 2

    false_branch_targets = [c["node"] for c in attachment_only_conns[1]]
    assert "Screen for Sensitive" in false_branch_targets, (
        f"Attachment Only? false branch must go to Screen for Sensitive, got: {false_branch_targets}"
    )


# ===========================================================================
# SB-145 — n8n intake: invalid classifier fallback
# ===========================================================================


def test_sb145_intake_has_invalid_output_type_node():
    """Intake workflow has an 'Invalid Output Type?' node."""
    wf = _intake()
    names = [n["name"] for n in wf["nodes"]]
    assert "Invalid Output Type?" in names, (
        f"Expected 'Invalid Output Type?' node, found: {names}"
    )


def test_sb145_invalid_classifier_output_routes_to_schedule_retry():
    """invalid_classifier_output reason_type routes to schedule-retry."""
    wf = _intake()
    nodes_with_reason = [
        n for n in wf["nodes"]
        if "invalid_classifier_output" in json.dumps(n.get("parameters", {}))
    ]
    assert len(nodes_with_reason) >= 1
    for node in nodes_with_reason:
        url = node["parameters"].get("url", "")
        assert "schedule-retry" in url, (
            f"invalid_classifier_output node must call schedule-retry, got: {url!r}"
        )


def test_sb145_malformed_gemini_output_routes_to_schedule_retry():
    """malformed_gemini_output reason_type routes to schedule-retry."""
    fixture_text = _INTAKE_FIXTURE.read_text()
    assert "malformed_gemini_output" in fixture_text, (
        "Expected 'malformed_gemini_output' reason_type in workflow"
    )
    wf = _intake()
    nodes_with_reason = [
        n for n in wf["nodes"]
        if "malformed_gemini_output" in json.dumps(n.get("parameters", {}))
    ]
    assert len(nodes_with_reason) >= 1
    for node in nodes_with_reason:
        url = node["parameters"].get("url", "")
        assert "schedule-retry" in url, (
            f"malformed_gemini_output node must call schedule-retry, got: {url!r}"
        )


def test_sb145_valid_classification_false_routes_through_invalid_output_type():
    """Valid Classification? false branch routes through Invalid Output Type? node."""
    wf = _intake()
    conns = wf["connections"]
    valid_class_conns = conns.get("Valid Classification?", {}).get("main", [])
    assert len(valid_class_conns) >= 2

    false_branch_targets = [c["node"] for c in valid_class_conns[1]]
    assert "Invalid Output Type?" in false_branch_targets, (
        f"Valid Classification? false branch must route to Invalid Output Type?, got: {false_branch_targets}"
    )


def test_sb145_valid_classification_true_branch_unchanged():
    """Valid Classification? true branch still routes to File or Inbox?."""
    wf = _intake()
    conns = wf["connections"]
    valid_class_conns = conns.get("Valid Classification?", {}).get("main", [])
    assert len(valid_class_conns) >= 1

    true_branch_targets = [c["node"] for c in valid_class_conns[0]]
    assert "File or Inbox?" in true_branch_targets, (
        f"Valid Classification? true branch must route to File or Inbox?, got: {true_branch_targets}"
    )


# ===========================================================================
# SB-146 — writer-service: classification schema and renderer sync
# ===========================================================================


def test_sb146_classified_action_has_due_priority_project():
    """ClassifiedAction model accepts due, priority, project fields."""
    from writerservice.api_models import ClassifiedAction

    action = ClassifiedAction.model_validate({
        "text": "Send gift",
        "status": "open",
        "due": "2026-08-14",
        "priority": "high",
        "project": "personal",
    })
    assert action.due == "2026-08-14"
    assert action.priority == "high"
    assert action.project == "personal"


def test_sb146_classified_action_fields_are_optional():
    """ClassifiedAction works without the new optional fields."""
    from writerservice.api_models import ClassifiedAction

    action = ClassifiedAction.model_validate({"text": "Do it", "status": "open"})
    assert action.due is None
    assert action.priority is None
    assert action.project is None


def test_sb146_classification_has_note_date():
    """Classification model accepts note_date field."""
    from writerservice.api_models import Classification

    cls = Classification.model_validate({
        "folder": "people",
        "project": None,
        "note_type": "birthday",
        "title": "Jane's Birthday",
        "tags": [],
        "body": "Remember to call Jane",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.95,
        "note_date": "2026-08-15",
    })
    assert cls.note_date == "2026-08-15"


def test_sb146_classification_note_date_optional():
    """Classification works without note_date."""
    from writerservice.api_models import Classification

    cls = Classification.model_validate({
        "folder": "projects",
        "project": "halo",
        "note_type": "task",
        "title": "Test",
        "tags": [],
        "body": "body",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.9,
    })
    assert cls.note_date is None


def test_sb146_renderer_writes_note_date_to_frontmatter(tmp_path):
    """_build_frontmatter_and_body writes note_date when present."""
    from writerservice.api_models import Classification
    from writerservice.writer import render_markdown as _build_frontmatter_and_body
    from datetime import datetime, timezone

    cls = Classification.model_validate({
        "folder": "people",
        "project": None,
        "note_type": "birthday",
        "title": "Jane's Birthday",
        "tags": [],
        "body": "Remember to call Jane",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.95,
        "note_date": "2026-08-15",
    })
    content = _build_frontmatter_and_body(
        capture_id="SB-20260622-0001",
        source_message_id="12345",
        created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        classification=cls,
        model="gemini",
        prompt_version="v1",
    )
    assert "note_date:" in content
    assert "2026-08-15" in content


def test_sb146_renderer_omits_note_date_when_absent(tmp_path):
    """_build_frontmatter_and_body omits note_date when not set."""
    from writerservice.api_models import Classification
    from writerservice.writer import render_markdown as _build_frontmatter_and_body
    from datetime import datetime, timezone

    cls = Classification.model_validate({
        "folder": "projects",
        "project": "halo",
        "note_type": "task",
        "title": "Test note",
        "tags": [],
        "body": "body",
        "actions": [],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.9,
    })
    content = _build_frontmatter_and_body(
        capture_id="SB-20260622-0001",
        source_message_id="12345",
        created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        classification=cls,
        model="gemini",
        prompt_version="v1",
    )
    assert "note_date:" not in content


def test_sb146_renderer_writes_action_due_priority_project(tmp_path):
    """Renderer writes due, priority, project on actions when present."""
    from writerservice.api_models import Classification
    from writerservice.writer import render_markdown as _build_frontmatter_and_body
    from datetime import datetime, timezone

    cls = Classification.model_validate({
        "folder": "projects",
        "project": "halo",
        "note_type": "task",
        "title": "Test note",
        "tags": [],
        "body": "body",
        "actions": [
            {
                "text": "Send gift",
                "status": "open",
                "due": "2026-08-14",
                "priority": "high",
                "project": "personal",
            }
        ],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.9,
    })
    content = _build_frontmatter_and_body(
        capture_id="SB-20260622-0001",
        source_message_id="12345",
        created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        classification=cls,
        model="gemini",
        prompt_version="v1",
    )
    assert "due: " in content
    assert "2026-08-14" in content
    assert "priority: " in content
    assert "high" in content
    assert "project: " in content
    assert "personal" in content


def test_sb146_renderer_omits_action_fields_when_absent(tmp_path):
    """Renderer omits due/priority/project when they are None."""
    from writerservice.api_models import Classification
    from writerservice.writer import render_markdown as _build_frontmatter_and_body
    from datetime import datetime, timezone

    cls = Classification.model_validate({
        "folder": "projects",
        "project": "halo",
        "note_type": "task",
        "title": "Test note",
        "tags": [],
        "body": "body",
        "actions": [{"text": "Do something", "status": "open"}],
        "needs_clarification": False,
        "clarifying_question": None,
        "confidence": 0.9,
    })
    content = _build_frontmatter_and_body(
        capture_id="SB-20260622-0001",
        source_message_id="12345",
        created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        classification=cls,
        model="gemini",
        prompt_version="v1",
    )
    # Should have the action text but not due/priority/project fields
    assert "Do something" in content
    lines_with_action = [l for l in content.splitlines() if "due:" in l or "priority:" in l]
    assert len(lines_with_action) == 0, f"Expected no due/priority lines, found: {lines_with_action}"


# ===========================================================================
# SB-147 — weekly scan: explicit completion rules
# ===========================================================================


def _make_weekly_vault(tmp_path, notes: list[tuple[str, str]]) -> Path:
    """Create a vault with (filename, content) pairs and return the vault path."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    for fname, content in notes:
        (vault / fname).write_text(content, encoding="utf-8")
    return vault


_THIS_WEEK_ISO = datetime.now(UTC).strftime("%Y-%m-%dT10:00:00+00:00")


def _run_weekly_scan(vault_dir: str, note_content: str, filename: str = "note.md") -> dict:
    """Helper: write a note to vault and run scan_weekly_brief."""
    from writerservice.brief import scan_weekly_brief
    from datetime import date, timedelta
    from pathlib import Path

    vault = Path(vault_dir)
    (vault / filename).write_text(note_content)
    # Use week range covering today so created_at this week is found
    today = date.today()
    week_start = today - timedelta(days=7)
    return scan_weekly_brief(vault, week_start=week_start, week_end=today)


def test_sb147_done_note_type_counted_as_accomplished():
    """note_type: done counted as accomplished in weekly scan."""
    import tempfile
    from datetime import date, timedelta

    today = date.today()
    # Use yesterday to ensure it falls within the 7-day window
    created = (today - timedelta(days=1)).isoformat()
    note_content = f"""\
---
note_type: done
title: "Shipped feature"
created_at: "{created}T10:00:00+00:00"
---
body
"""
    with tempfile.TemporaryDirectory() as vault_dir:
        result = _run_weekly_scan(vault_dir, note_content)
        accomplished = [a["title"] for a in result["accomplished"]]
        assert "Shipped feature" in accomplished


def test_sb147_fix_note_type_counted_as_accomplished():
    """note_type: fix counted as accomplished in weekly scan."""
    import tempfile
    from datetime import date, timedelta

    today = date.today()
    created = (today - timedelta(days=1)).isoformat()
    note_content = f"""\
---
note_type: fix
title: "Fixed the bug"
created_at: "{created}T10:00:00+00:00"
---
body
"""
    with tempfile.TemporaryDirectory() as vault_dir:
        result = _run_weekly_scan(vault_dir, note_content)
        accomplished = [a["title"] for a in result["accomplished"]]
        assert "Fixed the bug" in accomplished


def test_sb147_task_note_type_not_counted_as_accomplished():
    """Non-done note types (e.g. task) not counted as accomplished."""
    import tempfile
    from datetime import date, timedelta

    today = date.today()
    created = (today - timedelta(days=1)).isoformat()
    note_content = f"""\
---
note_type: task
title: "Pending task"
created_at: "{created}T10:00:00+00:00"
---
body
"""
    with tempfile.TemporaryDirectory() as vault_dir:
        result = _run_weekly_scan(vault_dir, note_content)
        accomplished = [a["title"] for a in result["accomplished"]]
        assert "Pending task" not in accomplished


def test_sb147_action_status_done_counted_as_completed_task():
    """Action with status='done' created this week is counted as completed task."""
    import tempfile
    from datetime import date, timedelta

    today = date.today()
    created = (today - timedelta(days=1)).isoformat()
    note_content = f"""\
---
note_type: task
title: "Weekly task"
created_at: "{created}T10:00:00+00:00"
actions:
  - text: "Completed action"
    status: "done"
---
body
"""
    with tempfile.TemporaryDirectory() as vault_dir:
        result = _run_weekly_scan(vault_dir, note_content)
        completed = [t["title"] for t in result["completed_tasks"]]
        assert "Completed action" in completed


def test_sb147_action_status_open_not_counted_as_completed():
    """Action with status='open' is not counted as completed task."""
    import tempfile
    from datetime import date, timedelta

    today = date.today()
    created = (today - timedelta(days=1)).isoformat()
    note_content = f"""\
---
note_type: task
title: "Weekly task"
created_at: "{created}T10:00:00+00:00"
actions:
  - text: "Open action"
    status: "open"
---
body
"""
    with tempfile.TemporaryDirectory() as vault_dir:
        result = _run_weekly_scan(vault_dir, note_content)
        completed = [t["title"] for t in result["completed_tasks"]]
        assert "Open action" not in completed


def test_sb147_action_status_completed_not_counted_as_completed_task():
    """Action with legacy status='completed' is NOT counted as a completed task."""
    import tempfile
    from datetime import date, timedelta

    today = date.today()
    created = (today - timedelta(days=1)).isoformat()
    note_content = f"""\
---
note_type: task
title: "Weekly task"
created_at: "{created}T10:00:00+00:00"
actions:
  - text: "Legacy completed action"
    status: "completed"
---
body
"""
    with tempfile.TemporaryDirectory() as vault_dir:
        result = _run_weekly_scan(vault_dir, note_content)
        completed = [t["title"] for t in result["completed_tasks"]]
        assert "Legacy completed action" not in completed


# ===========================================================================
# SB-148 — SQLite contention instrumentation
# ===========================================================================


def _collect_events(capsys, fn):
    """Run fn() and return parsed log events from stdout."""
    fn()
    out = capsys.readouterr().out
    events = []
    for line in out.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def test_sb148_sqlite_queue_depth_emitted_on_enqueue(tmp_path, capsys):
    """sqlite_queue_depth is emitted with 'depth' and 'operation_name' fields."""
    rt = SQLiteRuntime(tmp_path / "test.sqlite3")
    try:
        events = _collect_events(capsys, lambda: rt.read(lambda conn: None, operation_name="test_read"))
        depth_events = [e for e in events if e.get("event") == "sqlite_queue_depth"]
        assert len(depth_events) >= 1
        for e in depth_events:
            assert "depth" in e, f"sqlite_queue_depth missing 'depth' field: {e}"
            assert "operation_name" in e, f"sqlite_queue_depth missing 'operation_name' field: {e}"
    finally:
        rt.close()


def test_sb148_sqlite_queue_wait_ms_emitted_on_dequeue(tmp_path, capsys):
    """sqlite_queue_wait_ms is emitted with 'wait_ms' and 'operation_name' fields."""
    rt = SQLiteRuntime(tmp_path / "test.sqlite3")
    try:
        events = _collect_events(capsys, lambda: rt.read(lambda conn: None, operation_name="test_read"))
        wait_events = [e for e in events if e.get("event") == "sqlite_queue_wait_ms"]
        assert len(wait_events) >= 1
        for e in wait_events:
            assert "wait_ms" in e, f"sqlite_queue_wait_ms missing 'wait_ms' field: {e}"
            assert "operation_name" in e, f"sqlite_queue_wait_ms missing 'operation_name' field: {e}"
    finally:
        rt.close()


def test_sb148_sqlite_job_duration_ms_emitted_after_job(tmp_path, capsys):
    """sqlite_job_duration_ms is emitted with 'duration_ms' and 'operation_name' fields."""
    rt = SQLiteRuntime(tmp_path / "test.sqlite3")
    try:
        events = _collect_events(capsys, lambda: rt.read(lambda conn: None, operation_name="test_read"))
        duration_events = [e for e in events if e.get("event") == "sqlite_job_duration_ms"]
        assert len(duration_events) >= 1
        for e in duration_events:
            assert "duration_ms" in e, f"sqlite_job_duration_ms missing 'duration_ms' field: {e}"
            assert "operation_name" in e, f"sqlite_job_duration_ms missing 'operation_name' field: {e}"
    finally:
        rt.close()


def test_sb148_sqlite_busy_retry_count_emitted_on_lock(tmp_path, capsys):
    """sqlite_busy_retry_count is emitted with 'attempt', 'operation_name', 'retrying' fields."""
    import sqlite3 as _sqlite3
    from secondbrain.sqlite_runtime import _is_transient_lock_error

    rt = SQLiteRuntime(tmp_path / "test.sqlite3", retry_attempts=2, retry_base_delay_ms=1)
    try:
        call_count = [0]

        def failing_write(conn):
            call_count[0] += 1
            if call_count[0] < 2:
                err = _sqlite3.OperationalError("database is locked")
                raise err
            return "ok"

        capsys.readouterr()  # flush
        result = rt.write(failing_write, operation_name="lock_test")
        assert result == "ok"

        out = capsys.readouterr().out
        events = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
        retry_events = [e for e in events if e.get("event") == "sqlite_busy_retry_count"]
        assert len(retry_events) >= 1
        for e in retry_events:
            assert "attempt" in e, f"sqlite_busy_retry_count missing 'attempt': {e}"
            assert "operation_name" in e, f"sqlite_busy_retry_count missing 'operation_name': {e}"
            assert "retrying" in e, f"sqlite_busy_retry_count missing 'retrying': {e}"
    finally:
        rt.close()


def test_sb148_sqlite_busy_exhausted_count_emitted_when_all_retries_fail(tmp_path, capsys):
    """sqlite_busy_exhausted_count is emitted with 'operation_name' when all retries fail."""
    import sqlite3 as _sqlite3
    from secondbrain.sqlite_runtime import SQLiteBusyError

    rt = SQLiteRuntime(tmp_path / "test.sqlite3", retry_attempts=2, retry_base_delay_ms=1)
    try:
        def always_locked(conn):
            raise _sqlite3.OperationalError("database is locked")

        capsys.readouterr()
        with pytest.raises(SQLiteBusyError):
            rt.write(always_locked, operation_name="exhausted_test")

        out = capsys.readouterr().out
        events = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
        exhausted_events = [e for e in events if e.get("event") == "sqlite_busy_exhausted_count"]
        assert len(exhausted_events) >= 1
        for e in exhausted_events:
            assert "operation_name" in e, f"sqlite_busy_exhausted_count missing 'operation_name': {e}"
    finally:
        rt.close()


def test_sb148_all_duration_fields_are_milliseconds(tmp_path, capsys):
    """All duration/wait fields are numeric (milliseconds)."""
    rt = SQLiteRuntime(tmp_path / "test.sqlite3")
    try:
        events = _collect_events(capsys, lambda: rt.read(lambda conn: None, operation_name="ms_check"))
        for e in events:
            if e.get("event") in ("sqlite_queue_wait_ms", "sqlite_job_duration_ms"):
                for field in ("wait_ms", "duration_ms"):
                    if field in e:
                        assert isinstance(e[field], (int, float)), (
                            f"Field '{field}' in event '{e['event']}' must be numeric, got: {type(e[field])}"
                        )
    finally:
        rt.close()


# ===========================================================================
# Blocker fixes — Review feedback round 1
# ===========================================================================

# ------ Fix 1: receipt sync on all terminal delivery paths ------

@pytest.mark.asyncio
async def test_fix1_schedule_delivery_retry_terminal_sets_failed_when_receipt_fails(tmp_path):
    """schedule_delivery_retry with terminal failure sets receipt_sync_status='failed' if edit fails."""
    from unittest.mock import patch
    from secondbrain.ledger import RetryDisposition

    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    result = ledger.insert_accepted_capture(
        discord_message_id="1", discord_channel_id="200",
        discord_guild_id="100", discord_author_id="300", raw_text="hello",
    )
    capture_id = result.capture.capture_id
    ledger.set_receipt_message_id(capture_id, "9001")

    service = CaptureService(
        settings=settings, ledger=ledger,
        receipt_client=_AlwaysFailingReceiptClient(),
    )

    terminal_disposition = RetryDisposition(
        capture_id=capture_id,
        delivery_status="DELIVERY_FAILED",
        delivery_attempts=3,
        next_attempt_at=None,
        retry_scheduled=False,
        failed_terminally=True,
        outcome="terminal_failure",
    )
    with patch.object(ledger, "schedule_retry", return_value=terminal_disposition):
        await service.schedule_delivery_retry(
            capture_id=capture_id, delivery_attempt=3, error_type="WebhookError",
        )

    updated = ledger.get_capture(capture_id)
    assert updated.receipt_sync_status == "failed"
    ledger.close()


@pytest.mark.asyncio
async def test_fix1_acknowledge_delivery_failed_sets_failed_when_receipt_fails(tmp_path):
    """acknowledge_delivery_failed sets receipt_sync_status='failed' when the receipt edit fails."""
    from unittest.mock import patch
    from secondbrain.ledger import DeliveryMutationResult

    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    result = ledger.insert_accepted_capture(
        discord_message_id="1", discord_channel_id="200",
        discord_guild_id="100", discord_author_id="300", raw_text="hello",
    )
    capture_id = result.capture.capture_id
    ledger.set_receipt_message_id(capture_id, "9001")

    service = CaptureService(
        settings=settings, ledger=ledger,
        receipt_client=_AlwaysFailingReceiptClient(),
    )

    changed_result = DeliveryMutationResult(
        capture_id=capture_id, delivery_status="DELIVERY_FAILED", delivery_attempts=1,
        changed=True, outcome="changed",
    )
    with patch.object(ledger, "mark_delivery_failed_terminally", return_value=changed_result):
        await service.acknowledge_delivery_failed(
            capture_id=capture_id, delivery_attempt=1, reason_type="gemini_auth_failed",
        )

    updated = ledger.get_capture(capture_id)
    assert updated.receipt_sync_status == "failed"
    ledger.close()


# ------ Fix 2: REJECTED_SENSITIVE receipt sync ------

@pytest.mark.asyncio
async def test_fix2_rejected_sensitive_receipt_failure_sets_failed(tmp_path):
    """Rejected-sensitive capture sets receipt_sync_status='failed' when receipt send fails."""
    from tests.fakes.discord import FakeDiscordMessage, FakeDiscordChannel
    from secondbrain.secret_screen import SecretScreenResult

    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)

    channel = FakeDiscordChannel()
    channel.fail_initial_send = True  # makes send() raise on first call

    service = CaptureService(
        settings=settings, ledger=ledger,
        receipt_client=FakeDiscordClient(channel),
    )

    fake_result = SecretScreenResult(is_sensitive=True, redacted_text="[REDACTED]", flags=("password",))
    msg = FakeDiscordMessage(message_id=5555, content="irrelevant", channel=channel)

    await service._persist_sensitive_rejection(msg, fake_result)

    rows = ledger.get_out_of_sync_receipts()
    assert len(rows) >= 1
    cap = next(r for r in rows if r.discord_message_id == "5555")
    assert cap.receipt_sync_status == "failed"
    ledger.close()


@pytest.mark.asyncio
async def test_fix2_rejected_sensitive_receipt_success_sets_not_applicable(tmp_path):
    """Rejected-sensitive capture with successful send is excluded from out-of-sync list."""
    from tests.fakes.discord import FakeDiscordMessage, FakeDiscordChannel
    from secondbrain.secret_screen import SecretScreenResult

    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    channel = FakeDiscordChannel()

    service = CaptureService(
        settings=settings, ledger=ledger,
        receipt_client=FakeDiscordClient(channel),
    )

    fake_result = SecretScreenResult(is_sensitive=True, redacted_text="[REDACTED]", flags=("password",))
    msg = FakeDiscordMessage(message_id=5556, content="irrelevant", channel=channel)

    await service._persist_sensitive_rejection(msg, fake_result)

    # not_applicable is excluded from out_of_sync, so this capture must not appear there
    rows = ledger.get_out_of_sync_receipts()
    assert not any(r.discord_message_id == "5556" for r in rows), (
        "Successful rejection receipt must set not_applicable, not appear in out-of-sync list"
    )
    ledger.close()


# ------ Fix 3: n8n Parse Error? node for malformed routing ------

def test_fix3_intake_has_parse_error_if_node():
    """Intake workflow has a 'Parse Error?' If node."""
    wf = _intake()
    names = [n["name"] for n in wf["nodes"]]
    assert "Parse Error?" in names, (
        f"Expected 'Parse Error?' node in workflow, found: {names}"
    )


def test_fix3_parse_gemini_response_routes_to_parse_error_node():
    """Parse Gemini Response connects to Parse Error? (not directly to Validate Classification)."""
    wf = _intake()
    conns = wf["connections"]
    targets = [
        c["node"]
        for branch in conns.get("Parse Gemini Response", {}).get("main", [])
        for c in branch
    ]
    assert "Parse Error?" in targets, (
        f"Parse Gemini Response must route to Parse Error?, got: {targets}"
    )
    assert "Validate Classification" not in targets, (
        "Parse Gemini Response must not go directly to Validate Classification (must check parse_error first)"
    )


def test_fix3_parse_error_true_branch_routes_to_malformed_retry():
    """Parse Error? true branch routes to Schedule Retry (malformed classifier output)."""
    wf = _intake()
    conns = wf["connections"]
    branches = conns.get("Parse Error?", {}).get("main", [])
    assert len(branches) >= 2
    true_branch_targets = [c["node"] for c in branches[0]]
    assert "Schedule Retry (malformed classifier output)" in true_branch_targets, (
        f"Parse Error? true branch must go to malformed retry, got: {true_branch_targets}"
    )


def test_fix3_parse_error_false_branch_routes_to_validate_classification():
    """Parse Error? false branch continues to Validate Classification."""
    wf = _intake()
    conns = wf["connections"]
    branches = conns.get("Parse Error?", {}).get("main", [])
    assert len(branches) >= 2
    false_branch_targets = [c["node"] for c in branches[1]]
    assert "Validate Classification" in false_branch_targets, (
        f"Parse Error? false branch must go to Validate Classification, got: {false_branch_targets}"
    )


# ------ Fix 4: capture_service attachment-only gate before screen_text ------

@pytest.mark.asyncio
async def test_fix4_attachment_only_capture_bypasses_screen_text(tmp_path):
    """Attachment-only message (empty text + attachment) is accepted without screening."""
    from unittest.mock import patch
    from tests.fakes.discord import FakeDiscordMessage, FakeDiscordAttachment, FakeDiscordChannel

    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    channel = FakeDiscordChannel()

    service = CaptureService(
        settings=settings, ledger=ledger,
        receipt_client=FakeDiscordClient(channel),
    )

    attachment = FakeDiscordAttachment(filename="photo.jpg", content_type="image/jpeg")
    msg = FakeDiscordMessage(
        message_id=7777,
        content="",  # no text
        channel=channel,
        attachments=[attachment],
    )

    with patch("secondbrain.capture_service.screen_text") as mock_screen:
        await service.handle_gateway_message(msg)
        mock_screen.assert_not_called()

    all_caps = ledger._runtime.read(lambda conn: conn.execute("SELECT * FROM captures WHERE has_attachments=1").fetchall())
    assert len(all_caps) >= 1, "Attachment-only message must be persisted with has_attachments=1"
    ledger.close()


@pytest.mark.asyncio
async def test_fix4_message_with_text_and_attachment_still_screened(tmp_path):
    """Message with both text and attachments is still screened for sensitive content."""
    from unittest.mock import patch, MagicMock
    from tests.fakes.discord import FakeDiscordMessage, FakeDiscordAttachment, FakeDiscordChannel

    settings = _make_settings(tmp_path)
    ledger = Ledger(settings.ledger_path)
    channel = FakeDiscordChannel()

    service = CaptureService(
        settings=settings, ledger=ledger,
        receipt_client=FakeDiscordClient(channel),
    )

    attachment = FakeDiscordAttachment(filename="photo.jpg", content_type="image/jpeg")
    msg = FakeDiscordMessage(
        message_id=7778,
        content="some text with attachment",
        channel=channel,
        attachments=[attachment],
    )

    safe_result = MagicMock()
    safe_result.is_sensitive = False
    with patch("secondbrain.capture_service.screen_text", return_value=safe_result) as mock_screen:
        await service.handle_gateway_message(msg)
        mock_screen.assert_called_once()

    ledger.close()


# ------ Fix 5: receipt_sync_status enum validation ------

def test_fix5_set_receipt_sync_status_rejects_invalid_value(tmp_path):
    """set_receipt_sync_status raises ValueError for values outside the defined enum."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    result = ledger.insert_accepted_capture(
        discord_message_id="1", discord_channel_id="200",
        discord_guild_id="100", discord_author_id="300", raw_text="hello",
    )
    capture_id = result.capture.capture_id

    with pytest.raises(ValueError, match="Invalid receipt_sync_status"):
        ledger.set_receipt_sync_status(capture_id, status="broken")

    ledger.close()


def test_fix5_set_receipt_sync_status_accepts_all_valid_values(tmp_path):
    """set_receipt_sync_status accepts all four defined enum values without error."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    for i, status in enumerate(["clean", "failed", "pending_repair", "not_applicable"]):
        result = ledger.insert_accepted_capture(
            discord_message_id=str(i + 1), discord_channel_id="200",
            discord_guild_id="100", discord_author_id="300", raw_text=f"test {i}",
        )
        ledger.set_receipt_sync_status(result.capture.capture_id, status=status)

    ledger.close()
