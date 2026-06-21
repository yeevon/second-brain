# Tech Debt Milestone TD-01: P0 Security and Data Integrity Hardening

Addresses all P0 items from TechDebt.md. These are production safety, data durability, auth/security, and secret exposure risks that must be resolved before any future expansion of production operations.

This milestone does not add features. It closes known holes that exist today.

---

## SB-130 — WAL-safe SQLite backup and restore procedure

**Source:** TD-P0-001

See [SB-130.md](SB-130.md) for the full spec.

---

## SB-131 — Sanitize persisted local-worker exception messages

**Source:** TD-P0-002

See [SB-131.md](SB-131.md) for the full spec.

---

## SB-132 — Gate correction commands through capture authorization

**Source:** TD-P0-003

See [SB-132.md](SB-132.md) for the full spec.

---

## SB-133 — Resolve clarifications only after correction succeeds

**Source:** TD-P0-004

See [SB-133.md](SB-133.md) for the full spec.

---

## SB-134 — Preserve no-op correction move results

**Source:** TD-P0-005

See [SB-134.md](SB-134.md) for the full spec.

---

## SB-135 — Verify writer-service production startup and deploy-key handling

**Source:** TD-P0-006

See [SB-135.md](SB-135.md) for the full spec.

---

## Do not implement in this milestone

- P1 or lower tech debt items — those belong in TD-02 and later.
- New features or capability expansion.
- Architecture changes beyond what is needed to close the P0 items.
