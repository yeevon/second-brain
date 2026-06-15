# Milestone 6: Add review and local-query features

These are useful, but they should not distract from durable intake, retries, and backups.

This milestone implements Architecture Phase 5 (scheduled digests) and Phase 6 (local query access and Obsidian sync).

---

## Status summary

| Ticket | Title                              | Status         |
|--------|------------------------------------|----------------|
| SB-120 | Add daily digest workflow          | Done           |
| SB-121 | Add weekly review workflow         | Done           |
| SB-122 | Add local pull-only Obsidian sync  | Done           |
| SB-123 | Add the local read-only MCP server | Done           |
| SB-124 | Add gembrain CLI wrapper           | Not started    |

---

## SB-120 — Add daily digest workflow

**Branch:** `feature/daily-digest`

See [SB-120.md](SB-120.md) for the full spec.

All implementation complete. Backend API, ledger snapshot, n8n workflow, and 16 architecture tests passing.

---

## SB-121 — Add weekly review workflow

**Branch:** `feature/weekly-review`

See [SB-121.md](SB-121.md) for the full spec.

All implementation complete. Backend API, ledger snapshot, Gemini AI priorities, n8n workflow, and architecture tests passing.

---

## SB-122 — Add local pull-only Obsidian sync wrapper

**Branch:** `feature/local-vault-pull`

See [SB-122.md](SB-122.md) for the full spec.

Implemented as `vault_pull.py` with `vault-pull` CLI entry point. Dirty-worktree check, git fetch, ff-only merge, visible failure on conflict. Unit tests passing.

---

## SB-123 — Add the local read-only MCP server

**Branch:** `feature/read-only-mcp`

See [SB-123.md](SB-123.md) for the full spec.

All 5 MCP tools implemented (`search_notes`, `read_note`, `list_recent_notes`, `list_open_tasks`, `get_sync_status`). Path-root enforcement, result limits, no mutation, no shell access, sync preflight warning. Unit tests (446 lines) passing. `brain-mcp` entry point in `pyproject.toml`.

**Remaining gap:** Gemini CLI integration is in SB-124, not here. The MCP server's preflight warns on dirty vault rather than auto-syncing; the full fetch+merge preflight is delegated to `gembrain`.

---

## SB-124 — Add gembrain CLI wrapper and Gemini CLI integration

**Branch:** `feature/gembrain-cli`

See [SB-124.md](SB-124.md) for the full spec.

**Not yet started.** This is the only remaining piece of Milestone 6.

Requires:

- `src/secondbrain/gembrain.py`
- `gembrain` entry point in `pyproject.toml`
- `gembrain status`, `gembrain recent`, `gembrain tasks`, `gembrain ask` commands
- Vault-pull preflight before every query
- Gemini CLI invocation with `brain-mcp` as MCP server for `gembrain ask`
- Unit tests

---

## Do not implement these yet

Leave these in the backlog until the simpler design proves insufficient:

- S3-compatible attachment archive.
- Two-way Obsidian sync.
- Writable MCP tools.
- Vector search and embeddings.
- Redis or n8n queue mode.
- Separate Bouncer model call.
- Automatic prompt tuning.
- Multi-user capture.

The canonical architecture deliberately defers each of these until there is evidence that the simpler implementation is inadequate.
