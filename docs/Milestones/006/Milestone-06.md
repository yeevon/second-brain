# Milestone 6: Add review and local-query features

Implements Architecture Phase 5 (scheduled digests) and Phase 6 (local query access and Obsidian sync).

---

## Status summary

- **SB-120** Add daily digest workflow — Done
- **SB-121** Add weekly review workflow — Done
- **SB-122** Add local pull-only Obsidian sync — Done
- **SB-123** Add the local read-only MCP server — Done
- **SB-124** Add gembrain CLI wrapper — Done

Milestone 6 is implemented. The final shipped shape uses vault-backed brief endpoints for Daily and Weekly, a tracked-clean pull-only vault sync, a read-only MCP server, and the `gembrain` host CLI wrapper.

---

## SB-120 — Add daily digest workflow

See [SB-120.md](SB-120.md).

Implemented and tested. The current workflow calls `GET /internal/brief/daily`, which is backed by writer-service's vault scan (`GET /internal/vault/brief/daily`) with a local vault-scan fallback in capture-service.

The Discord message is grounded in:

- Today's Focus
- Coming Up
- Upcoming Birthdays
- Pending Tasks
- Stale / Neglected

The n8n workflow includes a no-activity skip branch and delivery-failure logging. It is imported inactive and can be updated in place by existing workflow ID through both local and EC2 bootstrap paths.

---

## SB-121 — Add weekly review workflow

See [SB-121.md](SB-121.md).

Implemented and tested. The current workflow calls `GET /internal/brief/weekly`, which is backed by writer-service's vault scan (`GET /internal/vault/brief/weekly`) with a local vault-scan fallback in capture-service.

Completion is grounded in explicit state only: `note_type: done` / `fix` notes and completed actions. Body text is not scanned for inferred completion. The AI priorities section is clearly labelled and never written back to the ledger. Gemini failure does not block the factual weekly summary from posting.

---

## SB-122 — Add local pull-only Obsidian sync wrapper

See [SB-122.md](SB-122.md).

Implemented and tested. `vault-pull` checks tracked-file cleanliness with `git status --porcelain --untracked-files=no`, so untracked Obsidian metadata does not block pulls. The CLI fails visibly on dirty tracked files, fetch failures, and non-fast-forward merge conflicts; it never force-merges or auto-resolves conflicts.

---

## SB-123 — Add the local read-only MCP server

See [SB-123.md](SB-123.md).

Implemented and tested. All five tools are read-only, enforce vault-root paths, clamp result limits, reject hidden/non-Markdown reads where appropriate, and preflight tracked-file cleanliness while ignoring untracked Obsidian metadata. `LEDGER_PATH` is optional, so vault tools work when only `VAULT_PATH` is configured.

---

## SB-124 — Add gembrain CLI wrapper and Gemini CLI integration

See [SB-124.md](SB-124.md).

Implemented and tested. `gembrain` provides:

- `gembrain status` — reports vault sync state without running a pull.
- `gembrain recent` — runs `vault-pull` preflight, then lists recent notes.
- `gembrain tasks` — runs `vault-pull` preflight, then lists open tasks.
- `gembrain ask` — runs `vault-pull` preflight, then invokes Gemini CLI with `brain-mcp` available as the MCP tool source.

`gembrain` never writes to the vault.

---

## Bootstrap / E2E standard

The local and EC2 n8n bootstrap paths now support no-wipe workflow refresh:

- Local `local-n8n-init` updates Error Handler, Intake, Daily Digest, and Weekly Review in place by existing workflow ID.
- EC2/staging `deploy/bootstrap-n8n.sh` updates Daily Digest and Weekly Review in place by existing workflow ID.
- Daily and Weekly remain inactive unless intentionally activated.
- Architecture tests lock exact local workflow mounts (`./n8n/workflows:/workflows:ro`, `./deploy/local-n8n-init.py:/init.py:ro`) and update-in-place behavior.

Clean E2E bar: `docker compose up -d --build` updates existing workflow JSON automatically, manual Daily Digest execution uses latest brief formatting, and no UI delete/reimport is required.

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
