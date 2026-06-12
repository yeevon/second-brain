# TD-005: Validate adopted legacy schemas before recording migration adoption

**Status:** Open
**Priority:** Medium — required before schema migrations become more complex

## Problem

Migration `001_initial_mvp_schema` uses `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` so a pre-migration MVP database can be adopted without data loss. This is correct for the known MVP schema, but it does not verify that an existing database has the expected columns, indexes, foreign keys, constraints, or sensitive-text protections.

A manually modified, partially restored, or corrupted database could be marked as migrated while retaining schema drift. Later migrations that assume the schema is correct would then silently corrupt data or fail in unpredictable ways.

## Acceptance criteria

- Before marking migration `001` as adopted, run a schema adoption validator that checks:
  - `PRAGMA table_info(...)` — all expected columns present with correct types
  - `PRAGMA index_list(...)` / `PRAGMA index_info(...)` — all expected indexes present
  - `PRAGMA foreign_key_list(...)` — all expected foreign key constraints
  - `sqlite_master` — no unexpected triggers or views that could bypass constraints
- On schema mismatch, fail startup with a clear error message listing the incompatible element.
- Do not attempt silent schema repair.
- Validator has unit tests covering: correct schema passes, missing column fails, wrong column type fails, missing index fails.

## Do not

- Do not change migration behavior for a fresh database (no adoption path taken).
- Do not attempt auto-repair of drift — fail clearly and require manual intervention.
