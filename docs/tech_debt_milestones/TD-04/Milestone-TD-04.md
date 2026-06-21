# Tech Debt Milestone TD-04: P2 Workflow Correctness and Observability

Addresses all P2 items from TechDebt.md. These are correctness gaps, edge cases, and observability holes that affect workflow reliability but are not production-blocking in the same way as P0/P1 items.

---

## SB-142 — Durable receipt-repair tracking

**Source:** TD-P2-001

See [SB-142.md](SB-142.md) for the full spec.

---

## SB-143 — Split Gemini error handling by failure class

**Source:** TD-P2-002

See [SB-143.md](SB-143.md) for the full spec.

---

## SB-144 — Handle attachment-only captures before text security screening

**Source:** TD-P2-003

See [SB-144.md](SB-144.md) for the full spec.

---

## SB-145 — Explicit invalid-classifier fallback in n8n Intake

**Source:** TD-P2-004

See [SB-145.md](SB-145.md) for the full spec.

---

## SB-146 — Writer-service classification schema and Markdown renderer metadata sync

**Source:** TD-P2-005

See [SB-146.md](SB-146.md) for the full spec.

---

## SB-147 — Fallback weekly scan explicit completion rules

**Source:** TD-P2-006

See [SB-147.md](SB-147.md) for the full spec.

---

## SB-148 — SQLite contention instrumentation

**Source:** TD-P2-007

See [SB-148.md](SB-148.md) for the full spec.

---

## Do not implement in this milestone

- P3 cleanup items — those belong in TD-05.
- SQLite async adapter — TD-P2-007 mandates instrumentation first; do not change the architecture without measurements.
- New user-facing features.
