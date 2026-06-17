# Milestone 7: V3 — Controlled LLM-Assisted Vault Updates

Implements the V3 extension described in `docs/ARCHITECTURE.md` section 18.

The goal is not to give an LLM direct write access to vault files. The goal is to let an LLM propose structured vault changes that are validated, approved by the user, and applied by `writer-service` under the existing Git lock. The read-only MCP server remains unchanged and remains the default.

EC2 production deployment is intentionally deferred to Milestone 9. V3 is built and validated locally first.

---

## SB-136 — Vault-update-proposal schema and storage

**Branch:** `feature/v3-proposal-schema`

See [SB-136.md](SB-136.md) for the full spec.

Defines the `vault_update_proposals` SQLite table, the proposal JSON contract, and the internal API for creating, reading, and listing proposals.

---

## SB-137 — Vault-update-service (validate and apply proposals)

**Branch:** `feature/v3-vault-update-service`

See [SB-137.md](SB-137.md) for the full spec.

Extends `writer-service` with a proposal-apply endpoint. Implements the initial allowed operations (`mark_task_done`, `mark_task_open`, `set_task_due_date`, etc.), anchor verification, path guards, and audit records.

---

## SB-138 — Discord approval surface

**Branch:** `feature/v3-discord-approval`

See [SB-138.md](SB-138.md) for the full spec.

When a proposal is submitted, `capture-service` posts an approval request to Discord showing the target file, operation, and before/after summary. The user approves or rejects by reply. At least one approval surface must exist before any write MCP tools are enabled.

---

## SB-139 — V3 proposal-only MCP tools

**Branch:** `feature/v3-mcp-proposal-tools`

See [SB-139.md](SB-139.md) for the full spec.

Adds a second MCP profile with proposal tools: `propose_task_completion`, `propose_due_date_change`, `propose_priority_change`, `propose_note_move`, `propose_task_append`, `propose_review_entry`, `list_pending_update_proposals`, `read_update_proposal`. These call the proposal API; they do not write vault files directly.

---

## SB-140 — V3 security hardening and acceptance tests

**Branch:** `feature/v3-security-tests`

See [SB-140.md](SB-140.md) for the full spec.

Tests and hardens the V3 write path against: prompt injection in vault notes, hallucinated paths, stale anchor detection, hidden-file access, bulk-edit rejection, and the full V3 acceptance criteria from the architecture.

---

## V3 design invariants (must remain true throughout)

- LLM clients may propose vault updates but may not directly write vault files.
- All approved vault mutations go through `writer-service` or the dedicated update service under the same Git lock.
- Every LLM-proposed update must be schema-validated before approval.
- Every approved update must produce an audit record and a Git commit.
- Rejected proposals are retained for audit but never applied.
- The read-only MCP profile remains the default.
- The raw capture ledger is never modified by LLM tooling.
