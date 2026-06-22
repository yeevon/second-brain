# Tech Debt Milestone TD-03: P1.5 Vault Auditability / Lossless Capture History

Addresses TD-P1.5-001 from TechDebt.md. Implements the immutable raw vault substrate so every capture has a lossless vault-visible artifact independent of LLM classification or writer-service transformation.

This is a single large feature. It touches writer-service, capture-service, and the vault schema.

Tracked by GitHub #20.

---

## SB-141 — Immutable raw vault substrate

**Source:** TD-P1.5-001

See [SB-141.md](SB-141.md) for the full spec.

The spec is complete and ready for implementation. All open questions have been resolved:

- **Sensitive-capture policy**: Option A — raw files may contain sensitive content; vault is trusted private storage.
- **Data-flow ownership**: writer-service writes raw file first, then classification proceeds. `FileNoteRequest` gains `raw_text`.
- **Raw file path**: `00_raw/YYYY/MM/<capture_id>.md` — deterministic from `capture_id` alone.
- **Hash definition**: `SHA-256(body.encode("utf-8"))` — body bytes only, no frontmatter, no line-ending normalization.
- **Attachment behavior**: text-only captures get exact body; text+attachments get body + `## Attachments` metadata section; attachment-only captures get metadata section only. No binary bytes in TD-03.
- **Failure semantics**: raw write failure blocks classification; retry reuses existing raw file if hash matches; hash mismatch fails hard.

---

## Do not implement in this milestone

- P2 or P3 items.
- UI changes or query tooling over raw captures.
- S3 attachment storage (post-production backlog).
- Encryption or redaction of raw vault files (not needed under Option A policy).
