# Tech Debt Milestone TD-03: P1.5 Vault Auditability / Lossless Capture History

Addresses TD-P1.5-001 from TechDebt.md. Implements the immutable raw vault substrate so every capture has a lossless vault-visible artifact independent of LLM classification or writer-service transformation.

This is a single large feature. It touches writer-service, capture-service, and the vault schema.

Tracked by GitHub #20.

---

## SB-141 — Immutable raw vault substrate

**Source:** TD-P1.5-001

See [SB-141.md](SB-141.md) for the full spec.

---

## Do not implement in this milestone

- P2 or P3 items.
- UI changes or query tooling over raw captures.
- S3 attachment storage (post-production backlog).
