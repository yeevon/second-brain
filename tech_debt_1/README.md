# Tech Debt — Milestone Three

One file per actionable item. Each file has a Status, Priority, and acceptance criteria.

## Open

| ID | Title | Priority |
| --- | --- | --- |
| [TD-001](TD-001-sanitize-local-worker-exception-messages.md) | Sanitize persisted local-worker exception messages | High |
| [TD-002](TD-002-shutdown-cleanup-resilience.md) | Make shutdown cleanup resilient to cleanup errors | Medium |
| [TD-003](TD-003-sqlite-event-loop-contention.md) | Prevent SQLite contention from blocking the asyncio event loop | Medium |
| [TD-004](TD-004-sqlite-runtime-watchdogs.md) | Add bounded SQLite runtime startup and shutdown watchdogs | Medium |
| [TD-005](TD-005-validate-legacy-schema-adoption.md) | Validate adopted legacy schemas before recording migration adoption | Medium |
| [TD-006](TD-006-wal-aware-backup-restore.md) | Define a WAL-aware backup and restore procedure | High |
| [TD-007](TD-007-sqlite-runtime-constructor-validation.md) | Add defensive validation inside SQLiteRuntime constructor | Low |
| [TD-008](TD-008-centralize-structured-logging.md) | Centralize structured application logging | Low |
| [TD-009](TD-009-durable-receipt-repair-tracking.md) | Add durable receipt-repair tracking | Medium |
| [TD-011](TD-011-configuration-preflight-command.md) | Add a configuration preflight command | Medium |
| [TD-012](TD-012-background-task-liveness-monitoring.md) | Monitor background-task liveness independently | Medium |
| [TD-013](TD-013-persist-vault-write-metadata.md) | Persist dedicated last-successful-vault-write metadata | Low |

## Resolved

| ID | Title |
| --- | --- |
| [TD-010](TD-010-idempotent-terminal-failure-callbacks.md) | Make explicit terminal-failure callbacks idempotent |
| [TD-014](TD-014-simplify-local-developer-workflow.md) | Simplify the local developer workflow |
| [TD-015](TD-015-docker-compose-lifecycle-broken.md) | Plain Docker lifecycle commands broken without shell exports |
