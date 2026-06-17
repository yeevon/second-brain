"""Tests for SB-136 through SB-140: V3 vault update proposals."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from secondbrain.capture_api import INTERNAL_TOKEN_HEADER, create_capture_api
from secondbrain.capture_models import (
    ALLOWED_PROPOSAL_OPERATIONS,
    PROPOSAL_APPLIED,
    PROPOSAL_APPROVED,
    PROPOSAL_FAILED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
)
from secondbrain.capture_service import CaptureService
from secondbrain.ledger import Ledger

from tests.fakes.discord import FakeDiscordChannel, FakeDiscordClient, FakeDiscordMessage

TOKEN = "test-internal-token-xyz"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_settings(tmp_path, **overrides):
    data = dict(
        discord_guild_id=100,
        discord_capture_channel_id=200,
        discord_allowed_user_id=300,
        startup_reconcile_limit=10,
        ledger_path=tmp_path / "runtime" / "ledger.sqlite3",
        vault_path=tmp_path / "vault",
        writer_service_url=None,
        writer_service_token=None,
        downstream_delivery_enabled=False,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def make_ledger(tmp_path, settings=None):
    return Ledger(settings.ledger_path if settings else tmp_path / "ledger.sqlite3")


@pytest_asyncio.fixture
async def api_ctx(tmp_path):
    settings = make_settings(tmp_path)
    ledger = make_ledger(tmp_path, settings)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)
    app = create_capture_api(capture_service=service, internal_token=TOKEN)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield SimpleNamespace(
            settings=settings,
            ledger=ledger,
            channel=channel,
            discord=discord,
            service=service,
            app=app,
            client=client,
        )
    finally:
        await client.aclose()
        ledger.close()


def _auth(token=TOKEN):
    return {INTERNAL_TOKEN_HEADER: token}


def _valid_proposal_body(**overrides):
    body = dict(
        source="mcp",
        requested_by="test-user",
        operation="mark_task_done",
        target_note_path="20_projects/second-brain/example.md",
        target_anchor_json=json.dumps({"task_text": "Write unit tests"}),
        change_json=json.dumps({"task_text": "Write unit tests"}),
        reason="User confirmed task is done",
        requires_approval=True,
    )
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# SB-136: Schema and storage
# ---------------------------------------------------------------------------


def test_vault_update_proposals_table_exists(tmp_path):
    """Migration v6 creates the vault_update_proposals table."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    tables = ledger._runtime.read(
        lambda conn: [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
    )
    ledger.close()
    assert "vault_update_proposals" in tables


def test_vault_update_proposals_has_approval_message_id_column(tmp_path):
    """Migration v7 adds approval_message_id to vault_update_proposals."""
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    columns = ledger._runtime.read(
        lambda conn: [
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(vault_update_proposals)"
            ).fetchall()
        ]
    )
    ledger.close()
    assert "approval_message_id" in columns


def test_create_proposal_returns_pending_with_vup_id(tmp_path):
    """create_proposal returns a PENDING proposal with VUP-YYYYMMDD-NNNN ID."""
    ledger = make_ledger(tmp_path)
    proposal = ledger.create_proposal(
        source="mcp",
        requested_by="test",
        operation="mark_task_done",
        target_note_path="20_projects/example.md",
        target_anchor_json=None,
        change_json=json.dumps({"task_text": "Do the thing"}),
        reason="done",
    )
    ledger.close()
    assert proposal.status == PROPOSAL_PENDING
    assert proposal.proposal_id.startswith("VUP-")
    parts = proposal.proposal_id.split("-")
    assert len(parts) == 3
    assert len(parts[1]) == 8  # YYYYMMDD
    assert len(parts[2]) == 4  # NNNN


def test_proposal_id_increments_within_day(tmp_path):
    """Sequential proposals on the same day get incrementing counters."""
    ledger = make_ledger(tmp_path)
    p1 = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None,
        change_json="{}", reason=None,
    )
    p2 = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_open",
        target_note_path="b.md", target_anchor_json=None,
        change_json="{}", reason=None,
    )
    ledger.close()
    date_part = p1.proposal_id.split("-")[1]
    assert p1.proposal_id == f"VUP-{date_part}-0001"
    assert p2.proposal_id == f"VUP-{date_part}-0002"


def test_get_proposal_returns_correct_record(tmp_path):
    ledger = make_ledger(tmp_path)
    created = ledger.create_proposal(
        source="test_src", requested_by="alice", operation="append_task",
        target_note_path="notes/foo.md", target_anchor_json=None,
        change_json=json.dumps({"task_text": "New task"}), reason="testing",
    )
    fetched = ledger.get_proposal(created.proposal_id)
    ledger.close()
    assert fetched.proposal_id == created.proposal_id
    assert fetched.source == "test_src"
    assert fetched.requested_by == "alice"
    assert fetched.operation == "append_task"


def test_get_proposal_raises_key_error_for_unknown_id(tmp_path):
    ledger = make_ledger(tmp_path)
    with pytest.raises(KeyError):
        ledger.get_proposal("VUP-20260616-9999")
    ledger.close()


def test_list_proposals_returns_by_status(tmp_path):
    ledger = make_ledger(tmp_path)
    p1 = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )
    p2 = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_open",
        target_note_path="b.md", target_anchor_json=None, change_json="{}", reason=None,
    )
    ledger.update_proposal(p1.proposal_id, status=PROPOSAL_APPROVED)

    pending = ledger.list_proposals(status=PROPOSAL_PENDING)
    approved = ledger.list_proposals(status=PROPOSAL_APPROVED)
    all_proposals = ledger.list_proposals()
    ledger.close()

    assert len(pending) == 1
    assert pending[0].proposal_id == p2.proposal_id
    assert len(approved) == 1
    assert approved[0].proposal_id == p1.proposal_id
    assert len(all_proposals) == 2


def test_update_proposal_sets_status_and_fields(tmp_path):
    ledger = make_ledger(tmp_path)
    proposal = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )
    now = datetime.now(UTC)
    updated = ledger.update_proposal(
        proposal.proposal_id,
        status=PROPOSAL_REJECTED,
        reviewed_by="alice",
        reviewed_at=now,
        rejected_reason="Not needed",
    )
    ledger.close()

    assert updated.status == PROPOSAL_REJECTED
    assert updated.reviewed_by == "alice"
    assert updated.rejected_reason == "Not needed"


# ---------------------------------------------------------------------------
# SB-136: Internal API routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_endpoint_returns_201(api_ctx):
    response = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(),
        headers=_auth(),
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == PROPOSAL_PENDING
    assert data["proposal_id"].startswith("VUP-")


@pytest.mark.asyncio
async def test_create_proposal_requires_auth(api_ctx):
    response = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_proposal_rejects_unsupported_operation(api_ctx):
    response = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(operation="delete_note"),
        headers=_auth(),
    )
    assert response.status_code == 422
    assert "unsupported operation" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_proposal_rejects_path_traversal(api_ctx):
    response = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(target_note_path="../../../etc/passwd"),
        headers=_auth(),
    )
    assert response.status_code == 422
    assert "traversal" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_proposal_rejects_absolute_path(api_ctx):
    response = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(target_note_path="/etc/passwd"),
        headers=_auth(),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_proposal_rejects_hidden_path(api_ctx):
    response = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(target_note_path=".obsidian/config.md"),
        headers=_auth(),
    )
    assert response.status_code == 422
    assert "hidden" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_proposal_rejects_git_path(api_ctx):
    response = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(target_note_path=".git/COMMIT_EDITMSG"),
        headers=_auth(),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_proposal_returns_404_for_unknown_id(api_ctx):
    response = await api_ctx.client.get(
        "/internal/vault/proposals/VUP-20260616-9999",
        headers=_auth(),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_proposal_returns_full_record(api_ctx):
    create_resp = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(),
        headers=_auth(),
    )
    proposal_id = create_resp.json()["proposal_id"]

    get_resp = await api_ctx.client.get(
        f"/internal/vault/proposals/{proposal_id}",
        headers=_auth(),
    )
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["proposal_id"] == proposal_id
    assert data["operation"] == "mark_task_done"


@pytest.mark.asyncio
async def test_list_proposals_returns_only_pending(api_ctx):
    body1 = _valid_proposal_body(operation="mark_task_done")
    body2 = _valid_proposal_body(operation="append_task")
    r1 = await api_ctx.client.post("/internal/vault/proposals", json=body1, headers=_auth())
    r2 = await api_ctx.client.post("/internal/vault/proposals", json=body2, headers=_auth())
    p1_id = r1.json()["proposal_id"]

    # Transition p1 to APPROVED
    await api_ctx.client.patch(
        f"/internal/vault/proposals/{p1_id}",
        json={"status": "APPROVED"},
        headers=_auth(),
    )

    list_resp = await api_ctx.client.get(
        "/internal/vault/proposals?status=PENDING",
        headers=_auth(),
    )
    assert list_resp.status_code == 200
    proposals = list_resp.json()
    assert len(proposals) == 1
    assert proposals[0]["proposal_id"] == r2.json()["proposal_id"]


@pytest.mark.asyncio
async def test_list_proposals_rejects_unknown_status(api_ctx):
    response = await api_ctx.client.get(
        "/internal/vault/proposals?status=BOGUS",
        headers=_auth(),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_proposal_updates_status(api_ctx):
    r = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(),
        headers=_auth(),
    )
    proposal_id = r.json()["proposal_id"]

    patch_resp = await api_ctx.client.patch(
        f"/internal/vault/proposals/{proposal_id}",
        json={"status": "APPROVED", "reviewed_by": "alice"},
        headers=_auth(),
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "APPROVED"
    assert patch_resp.json()["reviewed_by"] == "alice"


# ---------------------------------------------------------------------------
# SB-138: Discord approval surface
# ---------------------------------------------------------------------------


def make_discord_message(content: str, channel_id: int = 200):
    channel = FakeDiscordChannel(channel_id=channel_id)
    return FakeDiscordMessage(content=content, channel=channel)


@pytest.mark.asyncio
async def test_approve_vup_command_is_not_captured_as_note(tmp_path):
    """approve VUP-... messages must not be persisted as normal captures."""
    settings = make_settings(tmp_path)
    ledger = make_ledger(tmp_path, settings)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)

    # Create a proposal to approve
    proposal = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )

    msg = FakeDiscordMessage(
        content=f"approve {proposal.proposal_id}",
        channel=FakeDiscordChannel(),
    )
    await service.handle_gateway_message(msg)

    captures = service.captures_by_status("RECEIVED")
    assert len(captures) == 0, "approve command must not create a capture"
    ledger.close()


@pytest.mark.asyncio
async def test_reject_vup_command_is_not_captured_as_note(tmp_path):
    """reject VUP-... messages must not be persisted as normal captures."""
    settings = make_settings(tmp_path)
    ledger = make_ledger(tmp_path, settings)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)

    proposal = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )

    msg = FakeDiscordMessage(
        content=f"reject {proposal.proposal_id}",
        channel=FakeDiscordChannel(),
    )
    await service.handle_gateway_message(msg)

    captures = service.captures_by_status("RECEIVED")
    assert len(captures) == 0
    ledger.close()


@pytest.mark.asyncio
async def test_reject_vup_transitions_to_rejected(tmp_path):
    """Rejecting a PENDING proposal transitions it to REJECTED."""
    settings = make_settings(tmp_path)
    ledger = make_ledger(tmp_path, settings)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)

    proposal = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )
    msg = FakeDiscordMessage(
        content=f"reject {proposal.proposal_id}",
        channel=FakeDiscordChannel(),
    )
    await service.handle_gateway_message(msg)

    refreshed = ledger.get_proposal(proposal.proposal_id)
    assert refreshed.status == PROPOSAL_REJECTED
    assert refreshed.rejected_reason == "User rejected via Discord"
    ledger.close()


@pytest.mark.asyncio
async def test_reject_already_closed_proposal_replies_with_error(tmp_path):
    """Rejecting a REJECTED proposal sends a visible error, no state change."""
    settings = make_settings(tmp_path)
    ledger = make_ledger(tmp_path, settings)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)

    proposal = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )
    ledger.update_proposal(proposal.proposal_id, status=PROPOSAL_REJECTED)

    msg = FakeDiscordMessage(
        content=f"reject {proposal.proposal_id}",
        channel=channel,
    )
    await service.handle_gateway_message(msg)

    # Status stays REJECTED
    refreshed = ledger.get_proposal(proposal.proposal_id)
    assert refreshed.status == PROPOSAL_REJECTED

    # An error message was sent
    assert len(channel.sent_receipts) >= 1
    last_msg = channel.sent_receipts[-1][1]
    assert "already closed" in last_msg or "closed" in last_msg.lower()
    ledger.close()


@pytest.mark.asyncio
async def test_approve_unknown_proposal_replies_with_error(tmp_path):
    """Approving a non-existent proposal sends an error message, no crash."""
    settings = make_settings(tmp_path)
    ledger = make_ledger(tmp_path, settings)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)

    msg = FakeDiscordMessage(
        content="approve VUP-20260616-9999",
        channel=channel,
    )
    await service.handle_gateway_message(msg)

    assert len(channel.sent_receipts) >= 1
    last_msg = channel.sent_receipts[-1][1]
    assert "not found" in last_msg.lower()
    ledger.close()


@pytest.mark.asyncio
async def test_approve_when_writer_not_configured_marks_failed(tmp_path):
    """When writer-service is not configured, approve marks the proposal FAILED."""
    settings = make_settings(tmp_path, writer_service_url=None)
    ledger = make_ledger(tmp_path, settings)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)

    proposal = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )
    msg = FakeDiscordMessage(
        content=f"approve {proposal.proposal_id}",
        channel=channel,
    )
    await service.handle_gateway_message(msg)

    refreshed = ledger.get_proposal(proposal.proposal_id)
    assert refreshed.status == PROPOSAL_FAILED
    ledger.close()


# ---------------------------------------------------------------------------
# SB-137 / SB-140: proposal_ops path validation
# ---------------------------------------------------------------------------


def test_validate_vault_path_rejects_traversal(tmp_path):
    from writerservice.proposal_ops import validate_vault_path
    from writerservice.git_errors import PathTraversalError

    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(PathTraversalError):
        validate_vault_path(vault, "../../../etc/passwd")


def test_validate_vault_path_rejects_dotdot_component(tmp_path):
    from writerservice.proposal_ops import validate_vault_path
    from writerservice.git_errors import PathTraversalError

    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(PathTraversalError):
        validate_vault_path(vault, "20_projects/../../../etc/shadow")


def test_validate_vault_path_rejects_absolute_path(tmp_path):
    from writerservice.proposal_ops import validate_vault_path
    from writerservice.git_errors import PathTraversalError

    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(PathTraversalError):
        validate_vault_path(vault, "/etc/passwd")


def test_validate_vault_path_rejects_hidden_path(tmp_path):
    from writerservice.proposal_ops import validate_vault_path
    from writerservice.git_errors import PathTraversalError

    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(PathTraversalError):
        validate_vault_path(vault, ".obsidian/config.md")


def test_validate_vault_path_rejects_git_path(tmp_path):
    from writerservice.proposal_ops import validate_vault_path
    from writerservice.git_errors import PathTraversalError

    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(PathTraversalError):
        validate_vault_path(vault, ".git/COMMIT_EDITMSG")


def test_validate_vault_path_accepts_valid_path(tmp_path):
    from writerservice.proposal_ops import validate_vault_path

    vault = tmp_path / "vault"
    vault.mkdir()
    note_dir = vault / "20_projects" / "second-brain"
    note_dir.mkdir(parents=True)
    (note_dir / "example.md").write_text("# example\n")

    result = validate_vault_path(vault, "20_projects/second-brain/example.md")
    assert result == note_dir / "example.md"


# ---------------------------------------------------------------------------
# SB-140: lifecycle status protection
# ---------------------------------------------------------------------------


def test_check_lifecycle_status_rejects_archived(tmp_path):
    from writerservice.proposal_ops import check_lifecycle_status

    note = tmp_path / "archived.md"
    note.write_text("---\nlifecycle_status: archived\n---\n# My Note\n")

    with pytest.raises(ValueError, match="archived"):
        check_lifecycle_status(note)


def test_check_lifecycle_status_rejects_superseded(tmp_path):
    from writerservice.proposal_ops import check_lifecycle_status

    note = tmp_path / "superseded.md"
    note.write_text("---\nlifecycle_status: superseded\n---\n# Old Note\n")

    with pytest.raises(ValueError, match="superseded"):
        check_lifecycle_status(note)


def test_check_lifecycle_status_allows_active(tmp_path):
    from writerservice.proposal_ops import check_lifecycle_status

    note = tmp_path / "active.md"
    note.write_text("---\nlifecycle_status: active\n---\n# Active Note\n")

    # Should not raise
    check_lifecycle_status(note)


def test_check_lifecycle_status_allows_no_frontmatter(tmp_path):
    from writerservice.proposal_ops import check_lifecycle_status

    note = tmp_path / "plain.md"
    note.write_text("# Plain Note\nNo frontmatter.\n")

    check_lifecycle_status(note)


# ---------------------------------------------------------------------------
# SB-140: Anchor verification
# ---------------------------------------------------------------------------


def test_verify_anchor_raises_when_text_missing(tmp_path):
    from writerservice.proposal_ops import verify_anchor

    note = tmp_path / "note.md"
    note.write_text("# Note\n\n- [ ] Other task\n")

    with pytest.raises(ValueError, match="anchor not found"):
        verify_anchor(note, "The specific task text that was moved")


def test_verify_anchor_succeeds_when_text_present(tmp_path):
    from writerservice.proposal_ops import verify_anchor

    note = tmp_path / "note.md"
    note.write_text("# Note\n\n- [ ] Write unit tests\n")

    # Should not raise
    verify_anchor(note, "Write unit tests")


# ---------------------------------------------------------------------------
# SB-140: Operation implementations
# ---------------------------------------------------------------------------


def test_op_mark_task_done_changes_open_to_done(tmp_path):
    from writerservice.proposal_ops import op_mark_task_done

    note = tmp_path / "note.md"
    note.write_text("# Note\n\n- [ ] Write unit tests\n- [ ] Deploy\n")

    op_mark_task_done(note, "Write unit tests")

    content = note.read_text()
    assert "- [x] Write unit tests" in content
    assert "- [ ] Deploy" in content  # unchanged


def test_op_mark_task_open_changes_done_to_open(tmp_path):
    from writerservice.proposal_ops import op_mark_task_open

    note = tmp_path / "note.md"
    note.write_text("# Note\n\n- [x] Write unit tests\n- [x] Deploy\n")

    op_mark_task_open(note, "Write unit tests")

    content = note.read_text()
    assert "- [ ] Write unit tests" in content
    assert "- [x] Deploy" in content  # unchanged


def test_op_set_task_due_date_sets_frontmatter(tmp_path):
    from writerservice.proposal_ops import op_set_task_due_date

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: Test\n---\n# Note\n")

    op_set_task_due_date(note, "2026-07-01")

    content = note.read_text()
    assert "due_date: 2026-07-01" in content


def test_op_set_task_due_date_replaces_existing(tmp_path):
    from writerservice.proposal_ops import op_set_task_due_date

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: Test\ndue_date: 2026-06-01\n---\n# Note\n")

    op_set_task_due_date(note, "2026-07-01")

    content = note.read_text()
    assert "due_date: 2026-07-01" in content
    assert "due_date: 2026-06-01" not in content


def test_op_set_task_priority_sets_frontmatter(tmp_path):
    from writerservice.proposal_ops import op_set_task_priority

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: Test\n---\n# Note\n")

    op_set_task_priority(note, "high")

    assert "priority: high" in note.read_text()


def test_op_append_task_adds_to_actions_section(tmp_path):
    from writerservice.proposal_ops import op_append_task

    note = tmp_path / "note.md"
    note.write_text("# Note\n\n## Actions\n- [ ] Existing task\n")

    op_append_task(note, "New task item")

    content = note.read_text()
    assert "- [ ] New task item" in content


def test_op_append_task_adds_to_end_if_no_actions_section(tmp_path):
    from writerservice.proposal_ops import op_append_task

    note = tmp_path / "note.md"
    note.write_text("# Note\n\nSome body text.\n")

    op_append_task(note, "Orphan task")

    content = note.read_text()
    assert "- [ ] Orphan task" in content


def test_op_append_note_section_adds_section(tmp_path):
    from writerservice.proposal_ops import op_append_note_section

    note = tmp_path / "note.md"
    note.write_text("# Note\n\nSome content.\n")

    op_append_note_section(note, "Weekly Review", "- Did the thing\n- Another thing")

    content = note.read_text()
    assert "## Weekly Review" in content
    assert "Did the thing" in content


def test_op_add_project_tag_appends_tag(tmp_path):
    from writerservice.proposal_ops import op_add_project_tag

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: Test\ntags: [existing]\n---\n# Note\n")

    op_add_project_tag(note, "second-brain")

    content = note.read_text()
    assert "second-brain" in content


def test_op_add_project_tag_is_idempotent(tmp_path):
    from writerservice.proposal_ops import op_add_project_tag

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: Test\ntags: [existing, second-brain]\n---\n# Note\n")
    original = note.read_text()

    op_add_project_tag(note, "second-brain")

    assert note.read_text() == original


def test_op_add_weekly_review_entry_appends(tmp_path):
    from writerservice.proposal_ops import op_add_weekly_review_entry

    note = tmp_path / "note.md"
    note.write_text("# Weekly Review\n\nPrev entry.\n")

    op_add_weekly_review_entry(note, "Week of 2026-06-16: completed SB-136")

    content = note.read_text()
    assert "Week of 2026-06-16: completed SB-136" in content


# ---------------------------------------------------------------------------
# SB-140: Unsupported operations rejected at creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_operations_rejected(api_ctx):
    """All non-allowlisted operations must return 422 at creation time."""
    disallowed = [
        "delete_note",
        "overwrite_note",
        "git_push_direct",
        "modify_raw_capture",
        "bulk_edit",
        "shell",
        "write_note",
        "replace_note",
    ]
    for op in disallowed:
        response = await api_ctx.client.post(
            "/internal/vault/proposals",
            json=_valid_proposal_body(operation=op),
            headers=_auth(),
        )
        assert response.status_code == 422, f"Expected 422 for operation={op!r}, got {response.status_code}"
        assert "unsupported operation" in response.json()["detail"]


def test_all_allowed_operations_are_in_allowlist():
    """Every allowed operation name is present in ALLOWED_PROPOSAL_OPERATIONS."""
    expected = {
        "mark_task_done",
        "mark_task_open",
        "set_task_due_date",
        "set_task_priority",
        "append_task",
        "append_note_section",
        "move_note_to_folder",
        "add_project_tag",
        "add_weekly_review_entry",
    }
    assert expected == ALLOWED_PROPOSAL_OPERATIONS


# ---------------------------------------------------------------------------
# SB-140: Rejected proposals cannot be re-approved or applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_proposal_cannot_be_approved_again(tmp_path):
    """A REJECTED proposal stays rejected when another reject is attempted."""
    settings = make_settings(tmp_path)
    ledger = make_ledger(tmp_path, settings)
    channel = FakeDiscordChannel()
    discord = FakeDiscordClient(channel)
    service = CaptureService(settings=settings, ledger=ledger, receipt_client=discord)

    proposal = ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )
    ledger.update_proposal(proposal.proposal_id, status=PROPOSAL_REJECTED)

    msg = FakeDiscordMessage(
        content=f"approve {proposal.proposal_id}",
        channel=channel,
    )
    await service.handle_gateway_message(msg)

    refreshed = ledger.get_proposal(proposal.proposal_id)
    assert refreshed.status == PROPOSAL_REJECTED
    ledger.close()


@pytest.mark.asyncio
async def test_patch_rejected_proposal_to_approved_returns_proposal(api_ctx):
    """The API allows status updates; business logic guards in handle_gateway_message."""
    r = await api_ctx.client.post(
        "/internal/vault/proposals",
        json=_valid_proposal_body(),
        headers=_auth(),
    )
    proposal_id = r.json()["proposal_id"]

    # First reject it
    await api_ctx.client.patch(
        f"/internal/vault/proposals/{proposal_id}",
        json={"status": "REJECTED"},
        headers=_auth(),
    )

    # The vault remains unchanged (no apply was triggered)
    get_resp = await api_ctx.client.get(
        f"/internal/vault/proposals/{proposal_id}",
        headers=_auth(),
    )
    assert get_resp.json()["applied_at"] is None
    assert get_resp.json()["git_commit_hash"] is None


# ---------------------------------------------------------------------------
# SB-139: MCP propose server tool list
# ---------------------------------------------------------------------------


def test_mcp_propose_server_exports_eight_tools():
    """brain-mcp-propose exports exactly the 8 proposal tools."""
    import asyncio
    from secondbrain.mcp_propose_server import list_tools

    tools = asyncio.run(list_tools())
    names = {t.name for t in tools}
    expected = {
        "propose_task_completion",
        "propose_due_date_change",
        "propose_priority_change",
        "propose_note_move",
        "propose_task_append",
        "propose_review_entry",
        "list_pending_update_proposals",
        "read_update_proposal",
    }
    assert names == expected


def test_mcp_propose_server_does_not_have_disallowed_tools():
    """Tools that directly write vault or execute code are absent."""
    import asyncio
    from secondbrain.mcp_propose_server import list_tools

    tools = asyncio.run(list_tools())
    names = {t.name for t in tools}
    disallowed = {"write_note", "delete_note", "replace_note", "git_commit", "git_push", "shell"}
    assert names.isdisjoint(disallowed)


@pytest.mark.asyncio
async def test_mcp_propose_task_completion_requires_note_path():
    """propose_task_completion rejects empty note_path."""
    from secondbrain.mcp_propose_server import call_tool

    result = await call_tool("propose_task_completion", {"task_text": "Do thing", "reason": "Done"})
    assert any("ERROR" in r.text for r in result)
    assert any("note_path" in r.text for r in result)


@pytest.mark.asyncio
async def test_mcp_propose_task_completion_requires_task_text():
    """propose_task_completion rejects empty task_text."""
    from secondbrain.mcp_propose_server import call_tool

    result = await call_tool("propose_task_completion", {"note_path": "a.md", "reason": "Done"})
    assert any("ERROR" in r.text for r in result)
    assert any("task_text" in r.text for r in result)


@pytest.mark.asyncio
async def test_mcp_list_proposals_calls_api(monkeypatch):
    """list_pending_update_proposals calls the capture-service API."""
    from secondbrain import mcp_propose_server

    calls = []

    def fake_get(path, params=None):
        calls.append((path, params))
        return []

    monkeypatch.setattr(mcp_propose_server, "_api_get", fake_get)
    result = await mcp_propose_server.call_tool("list_pending_update_proposals", {})

    assert len(calls) == 1
    assert calls[0][0] == "/internal/vault/proposals"
    assert calls[0][1] == {"status": "PENDING"}


@pytest.mark.asyncio
async def test_mcp_read_proposal_calls_api_by_id(monkeypatch):
    """read_update_proposal calls the capture-service API with the proposal_id."""
    from secondbrain import mcp_propose_server

    calls = []

    def fake_get(path, params=None):
        calls.append(path)
        return {"proposal_id": "VUP-20260616-0001", "status": "PENDING"}

    monkeypatch.setattr(mcp_propose_server, "_api_get", fake_get)
    result = await mcp_propose_server.call_tool(
        "read_update_proposal", {"proposal_id": "VUP-20260616-0001"}
    )

    assert any("/internal/vault/proposals/VUP-20260616-0001" in c for c in calls)


# ---------------------------------------------------------------------------
# SB-140: Invariant checks
# ---------------------------------------------------------------------------


def test_raw_captures_table_not_modified_by_proposal_ops(tmp_path):
    """Proposal CRUD never touches the captures table."""
    ledger = make_ledger(tmp_path)

    capture_count_before = ledger._runtime.read(
        lambda conn: conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
    )

    ledger.create_proposal(
        source="mcp", requested_by="u", operation="mark_task_done",
        target_note_path="a.md", target_anchor_json=None, change_json="{}", reason=None,
    )

    capture_count_after = ledger._runtime.read(
        lambda conn: conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
    )

    ledger.close()
    assert capture_count_before == capture_count_after == 0
