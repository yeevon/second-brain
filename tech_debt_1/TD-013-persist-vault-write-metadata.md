# TD-013: Persist dedicated last-successful-vault-write metadata

**Status:** Open
**Priority:** Low

## Problem

The operational status report infers the last successful vault write by selecting the most recently updated `FILED` or `INBOX` row. A later metadata or receipt update on an older note can make it appear to be the most recent vault write, producing an incorrect status report.

## Acceptance criteria

- Add two system-state fields (either as a dedicated `system_state` table or as config rows):
  - `last_successful_vault_write_at` (timestamp)
  - `last_successful_vault_write_path` (sanitized vault path, no note content)
- These fields are updated only when the actual vault write succeeds — not on receipt update or metadata change.
- The operational status command reads these fields directly instead of inferring from `updated_at` on capture rows.
- The vault path stored is a safe relative path (no tokens, no content).

## Do not

- Do not store note content or raw text in the system-state fields.
- Do not change the capture row schema — use a separate system state store.
