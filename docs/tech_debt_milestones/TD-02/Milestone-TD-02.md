# Tech Debt Milestone TD-02: P1 Unattended EC2 Operation Hardening

Addresses all P1 items from TechDebt.md. These are the changes required for the system to run reliably without operator intervention on EC2 — timeouts, liveness monitoring, graceful shutdown, configuration preflight, and schema validation.

Depends on TD-01 being complete.

---

## SB-136 — SQLite runtime startup/shutdown watchdogs

**Source:** TD-P1-001

See [SB-136.md](SB-136.md) for the full spec.

---

## SB-137 — Background task liveness monitoring

**Source:** TD-P1-002

See [SB-137.md](SB-137.md) for the full spec.

---

## SB-138 — Shutdown cleanup resilience

**Source:** TD-P1-003

See [SB-138.md](SB-138.md) for the full spec.

---

## SB-139 — Configuration preflight command

**Source:** TD-P1-004

See [SB-139.md](SB-139.md) for the full spec.

---

## SB-140 — Legacy schema adoption validation

**Source:** TD-P1-005

See [SB-140.md](SB-140.md) for the full spec.

---

## Do not implement in this milestone

- P1.5, P2, or P3 items — those belong in TD-03, TD-04, TD-05.
- New user-facing features.
- SQLite async adapter or architecture changes (TD-P2-007 instrumentation comes first).
