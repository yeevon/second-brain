# TD-006: Define a WAL-aware backup and restore procedure

**Status:** Open
**Priority:** High — required before backups are treated as trustworthy

## Problem

SB-105 enables WAL (Write-Ahead Logging) mode for SQLite. A naive filesystem copy of only `ledger.sqlite3` may not include committed pages still present in `ledger.sqlite3-wal`. Such a backup would be silently incomplete and would restore to an older state.

There is currently no documented or tested backup procedure.

## Acceptance criteria

- Define and document at least one WAL-safe backup approach, such as:
  - SQLite Online Backup API (`sqlite3_backup_*`) via Python `sqlite3.Connection.backup()`
  - Controlled checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`) followed by filesystem snapshot
  - EBS snapshot with quiesced WAL
- The procedure must include a restore test: back up, restore to a separate path, verify row counts and checksums match.
- The procedure is documented in `deploy/README.md` under a "Backup and restore" section.
- Monitor WAL file growth before adding manual checkpoint tuning; keep SQLite defaults unless measurements show a problem.
- A backup that has never been restored is not considered trusted.

## Do not

- Do not add automated backup infrastructure before the procedure has a successful restore test.
- Do not tune WAL checkpoint parameters speculatively — measure first.
