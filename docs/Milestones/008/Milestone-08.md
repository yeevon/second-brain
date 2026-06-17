# Milestone 8: Tech Debt Resolution

Addresses all twelve open items from `docs/tech_debt_1/`. These were identified during Milestone 3 and deferred to keep earlier milestones focused on feature delivery. They are resolved after V3 (Milestone 7) and before EC2 production deployment (Milestone 9).

Items are grouped by theme so related changes land together.

---

## SB-130 — SQLite runtime resilience (TD-003, TD-004, TD-007)

**Branch:** `tech-debt/sqlite-runtime-resilience`

See [SB-130.md](SB-130.md) for the full spec.

Covers:

- TD-003: Prevent SQLite contention from blocking the asyncio event loop
- TD-004: Add bounded SQLite runtime startup and shutdown watchdogs
- TD-007: Add defensive validation inside `SQLiteRuntime` constructor

---

## SB-131 — Schema migration safety (TD-005)

**Branch:** `tech-debt/schema-migration-safety`

See [SB-131.md](SB-131.md) for the full spec.

Covers:

- TD-005: Validate adopted legacy schemas before recording migration adoption

---

## SB-132 — WAL-aware backup procedure (TD-006)

**Branch:** `tech-debt/wal-aware-backup`

See [SB-132.md](SB-132.md) for the full spec.

Covers:

- TD-006: Define a WAL-aware backup and restore procedure

---

## SB-133 — Exception sanitization and centralized logging (TD-001, TD-008)

**Branch:** `tech-debt/logging-hardening`

See [SB-133.md](SB-133.md) for the full spec.

Covers:

- TD-001: Sanitize persisted local-worker exception messages
- TD-008: Centralize structured application logging

---

## SB-134 — Shutdown resilience and receipt repair tracking (TD-002, TD-009)

**Branch:** `tech-debt/shutdown-receipt-resilience`

See [SB-134.md](SB-134.md) for the full spec.

Covers:

- TD-002: Make shutdown cleanup resilient to cleanup errors
- TD-009: Add durable receipt-repair tracking

---

## SB-135 — Operations command enhancements (TD-011, TD-012, TD-013)

**Branch:** `tech-debt/operations-tooling`

See [SB-135.md](SB-135.md) for the full spec.

Covers:

- TD-011: Add a configuration preflight command
- TD-012: Monitor background-task liveness independently
- TD-013: Persist dedicated last-successful-vault-write metadata

---

## Completion rule

Milestone 8 is done when all twelve open `tech_debt_1` items are resolved and their README entries are moved from Open to Resolved.

---

## Merge notes — staging

**Validation status:** Unit and integration-contract validated. E2E operational validation deferred.

Full end-to-end validation (live Discord → n8n → writer-service → vault write) is deferred until the complete V3 flow is assembled and clean enough to exercise without noise. Known runtime gaps identified during partial V3 validation will be handled as follow-up tech-debt fixes. Each item below is implemented and covered by targeted tests; the operational flow is not marked fully validated.

**Merge bar:**

```bash
uv run pytest tests/unit/test_m8_features.py
uv run pytest tests/unit writer-service/tests/unit
```

Both suites pass on the `milestone_8` branch. No E2E gate is required before merging to staging.
