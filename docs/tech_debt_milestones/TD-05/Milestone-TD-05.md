# Tech Debt Milestone TD-05: P3 Cleanup and Local Ergonomics

Addresses all P3 items from TechDebt.md. These are lower-priority cleanup, polish, and local ergonomics improvements. None are production blockers. They can be batched, interleaved with other work, or deferred further without risk.

Items marked with a dependency note should be evaluated after the referenced higher-priority work is done — they may be partially or fully resolved by that work.

---

## SB-149 — Startup reconciliation deferred-capture log clarity

**Source:** TD-P3-001 / GitHub #5

See [SB-149.md](SB-149.md) for the full spec.

---

## SB-150 — Stale local n8n owner mismatch diagnostics

**Source:** TD-P3-002 / GitHub #8

See [SB-150.md](SB-150.md) for the full spec.

---

## SB-151 — Stronger n8n readiness signal or documented sufficiency

**Source:** TD-P3-003 / GitHub #8

See [SB-151.md](SB-151.md) for the full spec.

---

## SB-152 — Deterministic local classifier mode for smoke tests

**Source:** TD-P3-004 / GitHub #8

See [SB-152.md](SB-152.md) for the full spec.

---

## SB-153 — Reduce local startup reconciliation surprises

**Source:** TD-P3-005 / GitHub #8

See [SB-153.md](SB-153.md) for the full spec.

---

## SB-154 — Rename duplicate delivery acceptance log noise

**Source:** TD-P3-006 / GitHub #8

See [SB-154.md](SB-154.md) for the full spec.

---

## SB-155 — Compose orchestration regression tests

**Source:** TD-P3-007 / GitHub #8

See [SB-155.md](SB-155.md) for the full spec.

---

## SB-156 — Defensive SQLiteRuntime constructor validation

**Source:** TD-P3-008

See [SB-156.md](SB-156.md) for the full spec.

> **Note:** Evaluate after SB-136 (SQLite watchdogs). May be fully covered by that work.

---

## SB-157 — Centralize structured application logging

**Source:** TD-P3-009

See [SB-157.md](SB-157.md) for the full spec.

> **Note:** Only prioritize if nearby logging work (SB-148 instrumentation, SB-137 liveness) makes it cheap.

---

## SB-158 — Persist last-successful-vault-write metadata

**Source:** TD-P3-010

See [SB-158.md](SB-158.md) for the full spec.

> **Note:** Evaluate after SB-141 (raw vault) and SB-142 (receipt repair). May be partially superseded.

---

## SB-159 — Dockerized MCP profile ownership and safe.directory behavior

**Source:** TD-P3-011 / GitHub #16

See [SB-159.md](SB-159.md) for the full spec.

> **Note:** Low priority if host-process `brain-mcp` remains the supported MCP path.

---

## Do not implement in this milestone

- New features.
- Architecture changes.
- Any P0/P1/P2 items that were missed — those belong in their respective milestones.
