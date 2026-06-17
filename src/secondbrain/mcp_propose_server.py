"""SB-139: Proposal-only MCP server (brain-mcp-propose profile).

Exposes vault-update proposal tools that call the capture-service proposal API.
No tool in this profile writes vault files directly or calls the apply endpoint.
All proposals require user approval through the Discord approval surface.

Run via: brain-mcp-propose (configured in pyproject.toml)
Required env vars:
  CAPTURE_SERVICE_URL   — e.g. http://localhost:8080
  CAPTURE_SERVICE_TOKEN — the X-Second-Brain-Internal-Token value
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("second-brain-propose")

_INTERNAL_TOKEN_HEADER = "X-Second-Brain-Internal-Token"


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _capture_url() -> str:
    raw = os.getenv("CAPTURE_SERVICE_URL", "").strip()
    return raw.rstrip("/") if raw else ""


def _capture_token() -> str:
    return os.getenv("CAPTURE_SERVICE_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _api_post(path: str, body: dict) -> dict:
    url = _capture_url()
    token = _capture_token()
    if not url:
        raise RuntimeError("CAPTURE_SERVICE_URL is not configured")
    if not token:
        raise RuntimeError("CAPTURE_SERVICE_TOKEN is not configured")
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{url}{path}",
        data=data,
        headers={
            _INTERNAL_TOKEN_HEADER: token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"capture-service returned {exc.code}: {body_text}") from exc


def _api_get(path: str, params: dict[str, str] | None = None) -> Any:
    url = _capture_url()
    token = _capture_token()
    if not url:
        raise RuntimeError("CAPTURE_SERVICE_URL is not configured")
    if not token:
        raise RuntimeError("CAPTURE_SERVICE_TOKEN is not configured")
    full_url = f"{url}{path}"
    if params:
        from urllib.parse import urlencode
        full_url = f"{full_url}?{urlencode(params)}"
    req = urllib.request.Request(
        full_url,
        headers={_INTERNAL_TOKEN_HEADER: token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"capture-service returned {exc.code}: {body_text}") from exc


# ---------------------------------------------------------------------------
# Tool list
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="propose_task_completion",
            description=(
                "Propose marking a task as done. "
                "Requires explicit confirmation that the task is complete — "
                "never infer completion from vague prose. "
                "Returns a proposal_id that the user must approve via Discord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {"type": "string", "description": "Relative vault path to the note"},
                    "task_text": {"type": "string", "description": "Exact task text as it appears in the note"},
                    "reason": {"type": "string", "description": "Why this task is believed to be complete"},
                    "completion_note": {"type": "string", "description": "Optional: brief note on how it was completed"},
                },
                "required": ["note_path", "task_text", "reason"],
            },
        ),
        Tool(
            name="propose_due_date_change",
            description=(
                "Propose setting or changing the due_date frontmatter field on a note. "
                "Returns a proposal_id that the user must approve via Discord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {"type": "string", "description": "Relative vault path to the note"},
                    "due_date": {"type": "string", "description": "New due date in YYYY-MM-DD format"},
                    "reason": {"type": "string", "description": "Why the due date is being changed"},
                },
                "required": ["note_path", "due_date"],
            },
        ),
        Tool(
            name="propose_priority_change",
            description=(
                "Propose setting or changing the priority frontmatter field on a note. "
                "Returns a proposal_id that the user must approve via Discord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {"type": "string", "description": "Relative vault path to the note"},
                    "priority": {"type": "string", "description": "New priority value (e.g. high, medium, low)"},
                    "reason": {"type": "string", "description": "Why the priority is being changed"},
                },
                "required": ["note_path", "priority"],
            },
        ),
        Tool(
            name="propose_note_move",
            description=(
                "Propose moving a note to a different vault folder. "
                "The target folder must be a top-level vault folder. "
                "Returns a proposal_id that the user must approve via Discord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {"type": "string", "description": "Relative vault path to the note"},
                    "new_folder": {"type": "string", "description": "Target vault folder (e.g. '20_projects/my-project')"},
                    "reason": {"type": "string", "description": "Why the note is being moved"},
                },
                "required": ["note_path", "new_folder"],
            },
        ),
        Tool(
            name="propose_task_append",
            description=(
                "Propose appending a new open task to the ## Actions section of a note. "
                "Returns a proposal_id that the user must approve via Discord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {"type": "string", "description": "Relative vault path to the note"},
                    "task_text": {"type": "string", "description": "Text for the new task (without '- [ ] ' prefix)"},
                    "reason": {"type": "string", "description": "Why this task is being added"},
                },
                "required": ["note_path", "task_text"],
            },
        ),
        Tool(
            name="propose_review_entry",
            description=(
                "Propose appending a weekly review entry to a digest note. "
                "Returns a proposal_id that the user must approve via Discord."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {"type": "string", "description": "Relative vault path to the review digest note"},
                    "entry": {"type": "string", "description": "Review entry text to append"},
                    "reason": {"type": "string", "description": "Description of the review period or context"},
                },
                "required": ["note_path", "entry"],
            },
        ),
        Tool(
            name="list_pending_update_proposals",
            description="List vault update proposals currently in PENDING status awaiting user approval.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20, "description": "Max results (1-50)"},
                },
            },
        ),
        Tool(
            name="read_update_proposal",
            description="Read the full detail of a single vault update proposal by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string", "description": "Proposal ID in VUP-YYYYMMDD-NNNN format"},
                },
                "required": ["proposal_id"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool call handler
# ---------------------------------------------------------------------------


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:

    if name == "propose_task_completion":
        note_path = (arguments.get("note_path") or "").strip()
        task_text = (arguments.get("task_text") or "").strip()
        reason = (arguments.get("reason") or "").strip()
        if not note_path:
            return [TextContent(type="text", text="ERROR: note_path is required")]
        if not task_text:
            return [TextContent(type="text", text="ERROR: task_text is required")]
        if not reason:
            return [TextContent(type="text", text="ERROR: reason is required")]
        change: dict[str, Any] = {"task_text": task_text}
        if arguments.get("completion_note"):
            change["completion_note"] = arguments["completion_note"]
        return await _submit_proposal(
            operation="mark_task_done",
            note_path=note_path,
            target_anchor_json=json.dumps({"task_text": task_text}),
            change=change,
            reason=reason,
        )

    if name == "propose_due_date_change":
        note_path = (arguments.get("note_path") or "").strip()
        due_date = (arguments.get("due_date") or "").strip()
        if not note_path:
            return [TextContent(type="text", text="ERROR: note_path is required")]
        if not due_date:
            return [TextContent(type="text", text="ERROR: due_date is required")]
        return await _submit_proposal(
            operation="set_task_due_date",
            note_path=note_path,
            target_anchor_json=None,
            change={"due_date": due_date},
            reason=arguments.get("reason"),
        )

    if name == "propose_priority_change":
        note_path = (arguments.get("note_path") or "").strip()
        priority = (arguments.get("priority") or "").strip()
        if not note_path:
            return [TextContent(type="text", text="ERROR: note_path is required")]
        if not priority:
            return [TextContent(type="text", text="ERROR: priority is required")]
        return await _submit_proposal(
            operation="set_task_priority",
            note_path=note_path,
            target_anchor_json=None,
            change={"priority": priority},
            reason=arguments.get("reason"),
        )

    if name == "propose_note_move":
        note_path = (arguments.get("note_path") or "").strip()
        new_folder = (arguments.get("new_folder") or "").strip()
        if not note_path:
            return [TextContent(type="text", text="ERROR: note_path is required")]
        if not new_folder:
            return [TextContent(type="text", text="ERROR: new_folder is required")]
        return await _submit_proposal(
            operation="move_note_to_folder",
            note_path=note_path,
            target_anchor_json=None,
            change={"new_folder": new_folder},
            reason=arguments.get("reason"),
        )

    if name == "propose_task_append":
        note_path = (arguments.get("note_path") or "").strip()
        task_text = (arguments.get("task_text") or "").strip()
        if not note_path:
            return [TextContent(type="text", text="ERROR: note_path is required")]
        if not task_text:
            return [TextContent(type="text", text="ERROR: task_text is required")]
        return await _submit_proposal(
            operation="append_task",
            note_path=note_path,
            target_anchor_json=None,
            change={"task_text": task_text},
            reason=arguments.get("reason"),
        )

    if name == "propose_review_entry":
        note_path = (arguments.get("note_path") or "").strip()
        entry = (arguments.get("entry") or "").strip()
        if not note_path:
            return [TextContent(type="text", text="ERROR: note_path is required")]
        if not entry:
            return [TextContent(type="text", text="ERROR: entry is required")]
        return await _submit_proposal(
            operation="add_weekly_review_entry",
            note_path=note_path,
            target_anchor_json=None,
            change={"entry": entry},
            reason=arguments.get("reason"),
        )

    if name == "list_pending_update_proposals":
        limit = max(1, min(int(arguments.get("limit", 20)), 50))
        try:
            results = _api_get("/internal/vault/proposals", {"status": "PENDING"})
            if isinstance(results, list):
                results = results[:limit]
            return [TextContent(type="text", text=json.dumps(results, indent=2, default=str))]
        except Exception as exc:
            return [TextContent(type="text", text=f"ERROR: {exc}")]

    if name == "read_update_proposal":
        proposal_id = (arguments.get("proposal_id") or "").strip()
        if not proposal_id:
            return [TextContent(type="text", text="ERROR: proposal_id is required")]
        try:
            result = _api_get(f"/internal/vault/proposals/{proposal_id}")
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
        except RuntimeError as exc:
            if "404" in str(exc):
                return [TextContent(type="text", text=f"ERROR: proposal {proposal_id!r} not found")]
            return [TextContent(type="text", text=f"ERROR: {exc}")]

    return [TextContent(type="text", text=f"ERROR: unknown tool: {name!r}")]


async def _submit_proposal(
    *,
    operation: str,
    note_path: str,
    target_anchor_json: str | None,
    change: dict,
    reason: str | None,
) -> list[TextContent]:
    try:
        result = _api_post(
            "/internal/vault/proposals",
            {
                "source": "mcp",
                "requested_by": "brain-mcp-propose",
                "operation": operation,
                "target_note_path": note_path,
                "target_anchor_json": target_anchor_json,
                "change_json": json.dumps(change),
                "reason": reason or "",
                "requires_approval": True,
            },
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
    except RuntimeError as exc:
        return [TextContent(type="text", text=f"ERROR: {exc}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    main()
