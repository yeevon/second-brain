# Second Brain Tech Debt

This document replaces the old per-file `tech_debt_1/` backlog. The goal is to keep tech debt visible, prioritized, and actionable without scattering small markdown files across the repo.

Tech debt should be promoted into a GitHub issue when it is ready to implement, needs discussion, or blocks a production milestone.

## Status model

| Status        | Meaning                                           |
| ------------- | ------------------------------------------------- |
| `Open`        | Valid concern, not started                        |
| `In Progress` | Actively being worked                             |
| `Blocked`     | Valid but waiting on another change               |
| `Resolved`    | Fixed and verified                                |
| `Obsolete`    | No longer applies after architecture/code changes |
| `Duplicate`   | Tracked by another item                           |

## Priority model

| Priority | Meaning                                                               |
| -------- | --------------------------------------------------------------------- |
| `P0`     | Production safety, data durability, auth/security, or secret exposure |
| `P1`     | Required for unattended EC2 operation or operator confidence          |
| `P2`     | Important correctness, observability, or workflow reliability         |
| `P3`     | Cleanup, polish, local ergonomics, or low-risk maintainability        |

## Canonical open GitHub trackers

| Issue | Purpose                                         | Notes                                                               |
| ----- | ----------------------------------------------- | ------------------------------------------------------------------- |
| #5    | Startup reconciliation deferred-capture logging | Canonical issue; #7 closed as duplicate                             |
| #8    | M5 local n8n validation follow-ups              | Canonical issue; #10 closed as duplicate                            |
| #16   | Pre-prod Codex review hardening checklist       | Treat as checklist; split into focused issues before implementation |
| #20   | Immutable raw vault substrate                   | Larger hardening feature for lossless technical capture history     |

---

# Backlog

## P0 — Production safety / data integrity / security

### TD-P0-001 — WAL-safe backup and restore procedure

**Status:** Open
**Source:** Former TD-006

SQLite WAL mode means copying only `ledger.sqlite3` may miss committed data still present in `ledger.sqlite3-wal`. Backups are not trustworthy until a restore has been tested.

**Acceptance criteria**

* Define a WAL-safe backup procedure.
* Procedure supports at least one safe path:

  * SQLite online backup API via Python `sqlite3.Connection.backup()`
  * controlled checkpoint followed by filesystem snapshot
  * quiesced EBS snapshot
* Add a restore test:

  * create backup
  * restore to separate path
  * verify row counts and integrity checks
* Document the procedure in `deploy/README.md`.
* Do not add automatic backup infrastructure until the manual procedure has passed restore testing.

---

### TD-P0-002 — Sanitize persisted local-worker exception messages

**Status:** Open
**Source:** Former TD-001

The local classifier worker can persist raw exception text into SQLite. Exception strings may contain tokens, internal URLs, raw HTTP bodies, note content, or unbounded stack/error text.

**Acceptance criteria**

* Add shared sanitizer for local-worker failure paths.
* Persist only:

  * safe error category
  * exception class name
  * optional bounded summary
* Do not persist:

  * tokens
  * credentials
  * internal URLs
  * raw HTTP response bodies
  * raw note text
  * unbounded exception strings
* Remove raw `str(exc)` from persisted failure fields.
* Add unit tests for secret-looking strings, URLs, long exception text, vault failures, and unexpected worker failures.

---

### TD-P0-003 — Gate correction commands through capture authorization

**Status:** Open
**Source:** GitHub #16

Correction commands must not bypass the same guild/channel/user/bot authorization rules used by normal captures.

**Acceptance criteria**

* `fix:` and `fix SB-...:` commands are ignored unless the message passes normal capture authorization.
* Unauthorized guild/channel/user correction attempts are tested.
* Authorized correction behavior remains unchanged.

---

### TD-P0-004 — Resolve clarifications only after correction succeeds

**Status:** Open
**Source:** GitHub #16

Clarifications must not be marked resolved until the correction has actually succeeded.

**Acceptance criteria**

* `resolve_clarification()` runs only after `apply_correction()` returns a successful result.
* Failed correction leaves clarification status unchanged.
* Add test for writer-service correction failure.

---

### TD-P0-005 — Preserve no-op correction move results

**Status:** Open
**Source:** GitHub #16

Same-folder/no-op correction moves should not be reported as real moves, and they should not erase prior delivery commit metadata.

**Acceptance criteria**

* Writer-service move response distinguishes moved vs no-op.
* Capture-service preserves original commit hash for no-op corrections.
* Correction history records no-op accurately.

---

### TD-P0-006 — Verify writer-service production startup and deploy-key handling

**Status:** Partially resolved
**Source:** GitHub #16 + EC2 hotfix

The EC2 hotfix removed the immediate `cap_drop: ALL` blocker for writer-service startup, but production hardening is not done until Git sync is verified end-to-end.

**Acceptance criteria**

* EC2 writer-service starts healthy.
* Runtime user can access copied SSH deploy key and known_hosts safely.
* Writer-service can `git fetch`, fast-forward, commit, and push with Git sync enabled.
* GitHub-backed mode fails fast with clear errors when key or known_hosts are missing.
* Local named-volume/fake-remote mode does not require GitHub deploy keys.
* Compose/config tests cover both local fake remote and GitHub-backed modes.
* Production security posture is documented.

---

## P1 — Unattended EC2 operation

### TD-P1-001 — SQLite runtime startup/shutdown watchdogs

**Status:** Open
**Source:** Former TD-004

SQLite runtime startup, queue operations, job completion, and shutdown should not wait forever.

**Acceptance criteria**

* Add configurable timeouts:

  * `SQLITE_STARTUP_TIMEOUT_S`
  * `SQLITE_QUEUE_WAIT_TIMEOUT_S`
  * `SQLITE_JOB_COMPLETION_TIMEOUT_S`
  * `SQLITE_SHUTDOWN_DRAIN_TIMEOUT_S`
* Timeout logs include:

  * step name
  * timeout value
  * elapsed time
* Timeout logs do not include raw exception bodies.
* Accepted captures must fail visibly on timeout.
* Queued work must not be silently discarded.
* Hung shutdown worker is logged and main thread exits.

---

### TD-P1-002 — Background task liveness monitoring

**Status:** Open
**Source:** Former TD-012

Important background tasks can die silently unless status output tracks their liveness.

Tracked tasks:

* capture-service heartbeat
* periodic Discord reconciliation
* stale-processing-lease reaper
* local classifier worker
* downstream delivery dispatcher

**Acceptance criteria**

* Each task records last successful pass timestamp.
* Stale task heartbeat marks status as degraded.
* `uv run secondbrain status` reports each task:

  * task name
  * running / completed_unexpectedly / degraded
  * last successful pass timestamp
  * last safe error type
* Status output contains safe metadata only.

---

### TD-P1-003 — Shutdown cleanup resilience

**Status:** Open
**Source:** Former TD-002

One cleanup failure should not prevent later shutdown cleanup from running.

**Acceptance criteria**

* Each shutdown step runs in its own try/except.
* Failure logs include cleanup step name and error type only.
* SQLite close always runs.
* Cleanup failures are logged, not swallowed.
* Existing shutdown tests continue to pass.

---

### TD-P1-004 — Configuration preflight command

**Status:** Open
**Source:** Former TD-011

Operators need a safe way to detect missing or invalid configuration before deployment.

**Acceptance criteria**

* Add `uv run secondbrain config-check`.
* Reports missing required variables by name only.
* Reports renamed variables where applicable.
* Reports which env template covers each variable.
* Distinguishes local-full vs capture-only requirements.
* Validates token minimum lengths.
* Validates numeric ranges.
* Never prints secret values.
* Exits non-zero on failure.
* Does not replace runtime fail-closed validation.

---

### TD-P1-005 — Legacy schema adoption validation

**Status:** Open
**Source:** Former TD-005

Existing pre-migration SQLite databases should not be marked migrated unless their schema matches the expected baseline.

**Acceptance criteria**

* Before adopting migration `001`, validate:

  * expected columns and types
  * expected indexes
  * expected foreign keys
  * no unexpected triggers/views that bypass constraints
* Startup fails clearly on mismatch.
* No silent repair.
* Unit tests cover:

  * correct schema passes
  * missing column fails
  * wrong type fails
  * missing index fails

---

## P1.5 — Vault auditability / lossless capture history

### TD-P1.5-001 — Immutable raw vault substrate

**Status:** Open
**Source:** GitHub #20

Sanitized notes are readable, but technical/admin/debug captures need a lossless vault-visible raw artifact.

**Acceptance criteria**

* Every capture writes raw Markdown under `00_raw/YYYY/MM/`.
* Raw file is written before LLM classification or writer-service transformation.
* Raw content preserves exact original input:

  * terminal output
  * logs
  * code fences
  * whitespace
  * line breaks
* Raw file includes `raw_sha256`.
* Sanitized note frontmatter includes:

  * `raw_capture_path`
  * `raw_sha256`
  * `derived_from_capture_id`
* Raw content is immutable after write.
* Duplicate/idempotent delivery does not create multiple raw files for the same capture ID.
* Tests prove raw hash matches sanitized note metadata.

---

## P2 — Workflow correctness / observability

### TD-P2-001 — Durable receipt-repair tracking

**Status:** Open
**Source:** Former TD-009

When terminal capture state commits but Discord receipt update fails, operators need durable visibility that the visible receipt is out of sync.

**Acceptance criteria**

* Add durable receipt sync marker:

  * `receipt_sync_status`
  * `receipt_sync_last_attempt_at`
  * `receipt_sync_last_error_type`
* Mark receipt sync failure after all fallback delivery paths fail.
* Include receipt sync status in operational status output.
* Receipt failure never rolls back committed capture state.

---

### TD-P2-002 — Split Gemini error handling by failure class

**Status:** Open
**Source:** GitHub #8

Gemini HTTP failures should not all map to one generic retry path.

**Acceptance criteria**

* Map `429` to rate-limit handling.
* Map `5xx` and timeout to server/timeout handling.
* Treat `401` and `403` as credential/config failures.
* Keep error payloads sanitized.

---

### TD-P2-003 — Handle attachment-only captures before text security screening

**Status:** Open
**Source:** GitHub #16

Attachment-only Discord messages can produce empty `raw_text`, which may fail text security screening.

**Acceptance criteria**

* Attachment-only captures route before text screening, or screening accepts empty text safely.
* Add workflow/architecture test for attachment-only path.
* Operator-visible outcome is clear.

---

### TD-P2-004 — Explicit invalid-classifier fallback in n8n Intake

**Status:** Open
**Source:** GitHub #16

Invalid classifier output should not wait for stale-lease recovery.

**Acceptance criteria**

* `valid=false` or `route=null` immediately routes to retry or terminal failure.
* No stale-lease wait required for schema-invalid Gemini output.
* Add n8n workflow test for invalid classifier output.

---

### TD-P2-005 — Writer-service classification schema and Markdown renderer metadata sync

**Status:** Open
**Source:** GitHub #16

Writer-service must accept and preserve metadata needed for briefs and structured notes.

**Acceptance criteria**

* Writer-service models accept same metadata fields as capture-service/n8n.
* Renderer preserves:

  * `note_date`
  * action `due`
  * action `priority`
  * action `project`
* Daily/Weekly brief tests cover birthday, event, reminder, and due-date notes filed through writer-service.

---

### TD-P2-006 — Fallback weekly scan explicit completion rules

**Status:** Open
**Source:** GitHub #16

Fallback weekly scan should not treat ordinary notes/tasks as completed accomplishments.

**Acceptance criteria**

* Only `note_type: done` and `note_type: fix` count as accomplished.
* Completed tasks require explicit completed action state.
* Tests match writer-service scanner behavior.

---

### TD-P2-007 — SQLite contention instrumentation

**Status:** Open
**Source:** Former TD-003

Do instrumentation first. Do not change the serialized SQLite worker design without real measurements.

**Acceptance criteria**

* Add structured log events for:

  * SQLite queue depth
  * queue wait duration
  * job execution duration
  * retry count
  * busy exhaustion count
* Logs contain numeric metadata only.
* Do not implement async adapter unless measurements justify it.

---

## P3 — Lower-priority cleanup / local ergonomics

### TD-P3-001 — Startup reconciliation deferred-capture log clarity

**Status:** Open
**Source:** GitHub #5

Startup/history reconciliation should not claim downstream processing is disabled when old messages are intentionally not enqueued.

**Acceptance criteria**

* Historical/startup messages use a distinct reason such as `historical_reconciliation_deferred`.
* `downstream processing disabled` is used only when downstream processing is actually disabled.
* Logs clearly show whether capture was queued for n8n delivery.

---

### TD-P3-002 — Stale local n8n owner mismatch diagnostics

**Status:** Open
**Source:** GitHub #8

Local n8n init should clearly explain 401 owner mismatch cases.

**Acceptance criteria**

* Catch login 401 in `deploy/local-n8n-init.py`.
* Print local remediation message:

  * stale n8n data volume
  * changed local email/password
  * manual owner creation in UI
* Do not print password or secrets.
* Add targeted test.

---

### TD-P3-003 — Stronger n8n readiness signal or documented sufficiency

**Status:** Open
**Source:** GitHub #8

The current readiness signal worked but may be fragile across n8n versions.

**Acceptance criteria**

* Identify stronger readiness signal, or
* Extend init retry behavior around import/activation/webhook registration, or
* Document why current readiness is sufficient.

---

### TD-P3-004 — Deterministic local classifier mode for smoke tests

**Status:** Open
**Source:** GitHub #8

Local smoke tests should not always burn Gemini tokens or depend on model behavior.

**Acceptance criteria**

* Add local-only deterministic classifier mode.
* Enable by env var.
* Keep one real Gemini manual/integration validation path.

---

### TD-P3-005 — Reduce local startup reconciliation surprises

**Status:** Open
**Source:** GitHub #8

Manual local validation can be confusing when startup reconciliation captures existing Discord messages.

**Acceptance criteria**

* Document clean-channel requirement, or
* Add local flag to disable startup reconciliation for smoke tests, or
* Log clearer startup reconciliation warning.

---

### TD-P3-006 — Rename duplicate delivery acceptance log noise

**Status:** Open
**Source:** GitHub #8

`duplicate_delivery_acceptance_ignored` is normal idempotency behavior but looks scary.

**Acceptance criteria**

* Rename/downgrade event.
* Include `outcome=idempotent_replay`.
* Document when expected.

---

### TD-P3-007 — Compose orchestration regression tests

**Status:** Open
**Source:** GitHub #8

Local compose ordering should be protected by tests.

**Acceptance criteria**

* Tests assert:

  * n8n healthcheck readiness path
  * local-n8n-init depends on n8n healthy
  * capture-service depends on local-n8n-init completed successfully
  * capture-service depends on writer-service healthy

---

### TD-P3-008 — Defensive SQLiteRuntime constructor validation

**Status:** Open
**Source:** Former TD-007

Keep if still valid after SQLite runtime watchdog work. Otherwise fold into TD-P1-001.

---

### TD-P3-009 — Centralize structured application logging

**Status:** Open
**Source:** Former TD-008

Do not prioritize as a standalone cleanup unless nearby logging work makes it cheap.

---

### TD-P3-010 — Persist last-successful-vault-write metadata

**Status:** Open
**Source:** Former TD-013

Keep as observability enhancement. May be partially superseded by immutable raw substrate and receipt-repair tracking.

---

### TD-P3-011 — Dockerized MCP profile ownership / safe.directory behavior

**Status:** Open
**Source:** GitHub #16

Low priority if host-process `brain-mcp` remains the supported MCP path.

**Acceptance criteria**

* Either fix Dockerized MCP vault ownership/safe.directory behavior, or
* Explicitly remove Dockerized MCP as a supported documented path.

---

# Obsolete / resolved / duplicate tracking

## Resolved from old folder

* Former TD-010 — terminal-failure callbacks idempotent
* Former TD-014 — simplified local developer workflow
* Former TD-015 — plain Docker lifecycle commands fixed

## Closed duplicate GitHub issues

* #7 duplicate of #5
* #10 duplicate of #8

---

# Next process

1. Delete old `tech_debt_1/` folder after this document is committed.
2. Keep this document as the canonical backlog.
3. Create GitHub issues from this document in priority order.
4. Do not create one giant implementation PR.
5. Each implementation issue should include:

   * problem
   * risk
   * acceptance criteria
   * files likely touched
   * tests required
   * production verification required, if applicable
