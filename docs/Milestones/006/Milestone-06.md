# Milestone 6: Add review and local-query features

These are useful, but they should not distract from durable intake, retries, and backups.

This milestone implements Architecture Phase 5: scheduled digests, local query access, and Obsidian sync.

---

## SB-120 — Add daily digest workflow

**Branch:** `feature/daily-digest`

See [SB-120.md](SB-120.md) for the full spec.

---

## SB-121 — Add weekly review workflow

**Branch:** `feature/weekly-review`

See [SB-121.md](SB-121.md) for the full spec.

---

## SB-122 — Add local pull-only Obsidian sync wrapper

**Branch:** `feature/local-vault-pull`

See [SB-122.md](SB-122.md) for the full spec.

---

## SB-123 — Add the local read-only MCP server

**Branch:** `feature/read-only-mcp`

See [SB-123.md](SB-123.md) for the full spec.

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
