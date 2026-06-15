# Milestone 6: Add review and local-query features

Implements Architecture Phase 5 (scheduled digests) and Phase 6 (local query access and Obsidian sync).

---

## Status summary

- **SB-120** Add daily digest workflow — Partial, 3 ACs open
- **SB-121** Add weekly review workflow — Done
- **SB-122** Add local pull-only Obsidian sync — Partial, 4 items open
- **SB-123** Add the local read-only MCP server — Partial, 1 AC open
- **SB-124** Add gembrain CLI wrapper — Not started

SB-122 and SB-123 are close to done — gaps are small and clearly bounded. SB-120 has three meaningful open acceptance criteria. SB-124 has not been started.

---

## SB-120 — Add daily digest workflow

See [SB-120.md](SB-120.md).

Backend API, ledger snapshot, and n8n workflow are implemented and tested. Three acceptance criteria remain open:

- Open tasks grouped by project/folder (currently reports total count only).
- "No new activity" branch when all counts are zero.
- Digest delivery failure wired to n8n Error Trigger.

---

## SB-121 — Add weekly review workflow

See [SB-121.md](SB-121.md).

All acceptance criteria met. Completion is grounded in explicit `note_type` state changes only. AI priorities section is clearly labeled and never written back to the ledger. One housekeeping item: wire the n8n Error Trigger to the weekly review workflow the same way it is wired to intake.

---

## SB-122 — Add local pull-only Obsidian sync wrapper

See [SB-122.md](SB-122.md).

Core implementation done. Open items:

- `main()` unit tests (3 missing).
- Untracked-files gap: `vault_pull.py` uses bare `--porcelain`; the MCP preflight uses `--untracked-files=no`. Fix `vault_pull.py` to be consistent so Obsidian metadata not in `.gitignore` does not incorrectly block a pull.

---

## SB-123 — Add the local read-only MCP server

See [SB-123.md](SB-123.md).

All five tools implemented with path enforcement, result limits, no mutations, and clean preflight. One open acceptance criterion:

- End-to-end validation that Claude Code can read a vault note filed by the Docker pipeline — requires `LOCAL_VAULT_PATH` bind-mount during EC2 setup, not a code change.

---

## SB-124 — Add gembrain CLI wrapper and Gemini CLI integration

See [SB-124.md](SB-124.md).

Not started. This is the only fully unimplemented piece of Milestone 6. Requires `src/secondbrain/gembrain.py`, a `gembrain` entry point in `pyproject.toml`, and unit tests. Commands: `status`, `recent`, `tasks`, `ask`.

---

## Do not implement these yet

- S3-compatible attachment archive.
- Two-way Obsidian sync.
- Writable MCP tools.
- Vector search and embeddings.
- Redis or n8n queue mode.
- Separate Bouncer model call.
- Automatic prompt tuning.
- Multi-user capture.
