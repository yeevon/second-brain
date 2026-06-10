# SB-110 — Milestone 2 Exit Regression Runbook

This document is the completion gate for Milestone 2. Every item must pass before the milestone is closed.

**Automated** items have permanent test coverage and must pass on every commit.
**Manual** items require infrastructure (Docker, EC2) and must be re-run at release time.

---

## Step 1 — Full automated suite

```bash
uv run pytest -q
```

Expected: all tests pass, zero failures, zero errors.

The suite covers the following exit criteria automatically:

| Criterion | Primary test coverage |
|---|---|
| Duplicate suppression | `test_duplicate_discord_event_is_idempotent`<br>`test_capture_only_mode_duplicate_gateway_event_does_not_duplicate_row_or_receipt`<br>`test_duplicate_event_after_filing_does_not_refile_or_resend` |
| Sensitive-input rejection | `test_secret_like_input_stores_redacted_rejection_only`<br>`test_capture_only_mode_rejects_sensitive_message_before_plaintext_persistence`<br>`test_retry_api_rejects_free_form_error_message`<br>`test_terminal_failure_api_rejects_secret_like_reason` |
| Offline reconciliation | `test_capture_only_reconciliation_failure_allows_retry`<br>`test_startup_reconciliation_failure_does_not_prevent_reaper_start` |
| Periodic skipped-Gateway recovery | `test_skipped_gateway_event_is_recovered_by_periodic_reconciliation` |
| Retry and lease recovery | `test_schedule_retry_makes_capture_reclaimable`<br>`test_schedule_retry_reaches_terminal_failure_at_cap`<br>`test_repeated_fake_downstream_crashes_eventually_fail_visibly`<br>`test_manual_retry_requeues_capture_after_retry_cap_failure`<br>`test_reaper_receipt_alert_runs_only_after_database_commit` |
| Missing-volume fail-closed | `test_local_full_mode_requires_vault_path`<br>`test_local_full_mode_requires_gemini_api_key` |
| Status-command validation | `tests/unit/test_status.py` (49 tests)<br>`tests/unit/test_heartbeat.py` (12 tests)<br>`tests/integration/test_capture_only_runtime.py` status tests |
| Clean shutdown | `test_sigterm_style_shutdown_closes_service_cleanly`<br>`test_local_full_runtime_records_stopped_state_on_shutdown` |
| Discord capture (fake) | `test_capture_only_mode_persists_normal_message_as_received`<br>`test_capture_only_mode_sends_durable_capture_receipt`<br>`test_happy_path_capture_to_vault_edits_original_receipt` |
| EC2 reboot persistence (startup recovery) | `test_startup_catchup_recovers_missed_message_once`<br>`test_crash_before_sqlite_commit_is_recovered_by_next_catchup` |

---

## Step 2 — SB-109 status-command slice

Run the focused operational-status test slice to confirm the status command is fully wired:

```bash
uv run pytest \
  tests/unit/test_status.py \
  tests/unit/test_heartbeat.py \
  tests/unit/test_config.py \
  tests/unit/test_app_capture_flow.py \
  tests/unit/test_capture_only_mode.py \
  tests/integration/test_capture_only_runtime.py \
  tests/integration/test_mvp_flow.py \
  -q
```

Then run the command against the live ledger and confirm it exits cleanly:

```bash
uv run python -m secondbrain status
echo "exit code: $?"
```

Expected output contains all sections (`Capture intake`, `Note lifecycle`, `Delivery backlog`, `Discord reconciliation`, `Capture service`).

Expected exit code: `0` if service is running with a fresh heartbeat, `1` if no service is running (`UNKNOWN` health), `2` only if the ledger file is missing.

---

## Step 3 — Local Docker regression

### 3a — Build and start the container

```bash
docker compose build
docker compose up -d
```

Confirm the container starts and does not exit within 30 seconds:

```bash
docker compose ps
```

Expected: `capture-service` is `Up`.

### 3b — Discord capture test

Send a test message in the configured Discord channel from the allowed user account.

Confirm within 60 seconds:

1. A durable receipt appears in Discord (bot reply with `⏳ SB-…`).
2. In capture-only mode, no vault write occurs.
3. In local-full mode, the vault note is created and the receipt is edited to `✅`.

Confirm in logs:

```bash
docker compose logs --tail=50 capture-service
```

Expected: `{"event":"capture_received",...}` log line, no error events.

### 3c — Live SQLite monitoring

While the container is running, check the ledger:

```bash
uv run python -m secondbrain status
```

Expected: health `HEALTHY`, heartbeat within the last 60 seconds.

Run the status command continuously:

```bash
watch -n 15 'uv run python -m secondbrain status | tail -8'
```

Confirm the `capture-service last heartbeat` timestamp advances every ~15 seconds.

### 3d — Duplicate suppression (live)

Send the exact same Discord message ID twice (e.g., edit a message to trigger a re-delivery if your test harness supports it, or send two identical messages rapidly).

Confirm the ledger shows exactly one capture row:

```bash
sqlite3 .runtime/ledger.sqlite3 "SELECT COUNT(*) FROM captures WHERE discord_message_id = '<id>';"
```

Expected: `1`.

### 3e — Container recreation

Stop and remove the container while keeping the named volume:

```bash
docker compose stop capture-service
docker compose rm -f capture-service
docker compose up -d capture-service
```

Confirm after restart:

1. The service starts without errors.
2. `uv run python -m secondbrain status` shows the new instance ID but no lost rows.
3. `total captures` matches the count before recreation.

### 3f — Missing-volume fail-closed behavior

Start the container without the ledger volume mounted:

```bash
docker run --rm \
  -e CAPTURE_PROCESSING_MODE=capture-only \
  -e DISCORD_BOT_TOKEN=fake \
  -e DISCORD_GUILD_ID=1 \
  -e DISCORD_CAPTURE_CHANNEL_ID=2 \
  -e DISCORD_ALLOWED_USER_ID=3 \
  -e LEDGER_PATH=/nonexistent/path/ledger.sqlite3 \
  -e CAPTURE_SERVICE_INTERNAL_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))") \
  -e STARTUP_RECONCILE_LIMIT=100 \
  -e CAPTURE_API_HOST=127.0.0.1 \
  -e CAPTURE_API_PORT=8000 \
  <image_name>
```

Expected: container exits with a clear error message referencing the missing path (or parent directory). No silent data loss.

### 3g — Clean shutdown (container)

Send SIGTERM to the container:

```bash
docker compose stop --timeout 15 capture-service
docker compose logs --tail=20 capture-service
```

Expected log events:

```text
{"event":"capture_service_stopped",...}
shutdown complete
```

Expected: container exits with code `0`. No `{"event":"capture_service_heartbeat_superseded",...}` or dangling tasks logged.

Confirm the ledger shows `STOPPED` state:

```bash
sqlite3 .runtime/ledger.sqlite3 \
  "SELECT value FROM system_state WHERE key='capture_service_state';"
```

Expected: `STOPPED`.

---

## Step 4 — EC2 reboot persistence

### 4a — Deploy to EC2

Push the current branch to the EC2 instance and start the service:

```bash
# On EC2
git pull
docker compose up -d
```

Send a test capture and confirm it is durably saved.

### 4b — Reboot the instance

```bash
sudo reboot
```

### 4c — Post-reboot verification

After the instance restarts and the container comes back up (typically 1–3 minutes):

1. Confirm the service is running:

   ```bash
   docker compose ps
   ```

2. Check the status command:

   ```bash
   uv run python -m secondbrain status
   ```

   Expected: health `HEALTHY`, new instance ID, total captures unchanged from before reboot.

3. Confirm no captures were lost:

   ```bash
   sqlite3 .runtime/ledger.sqlite3 "SELECT COUNT(*) FROM captures;"
   ```

4. Confirm the capture sent before reboot is present:

   ```bash
   sqlite3 .runtime/ledger.sqlite3 \
     "SELECT capture_id, status FROM captures ORDER BY id DESC LIMIT 5;"
   ```

---

## Step 5 — Periodic skipped-Gateway recovery (live confirmation)

This is covered by the automated integration test `test_skipped_gateway_event_is_recovered_by_periodic_reconciliation`.

For a live confirmation:

1. Note the current `last_reconciled_discord_message_id` from `uv run python -m secondbrain status`.
2. Send a Discord message and confirm it is captured normally.
3. Manually delete the row from the ledger (test environment only):
   ```bash
   sqlite3 .runtime/ledger.sqlite3 \
     "DELETE FROM captures WHERE discord_message_id = '<id>';"
   ```
4. Wait for the next periodic reconciliation pass (default 60 seconds).
5. Confirm the row is re-inserted.

---

## Step 6 — Retry and lease recovery (live confirmation)

This is covered by the automated tests `test_schedule_retry_makes_capture_reclaimable`, `test_repeated_fake_downstream_crashes_eventually_fail_visibly`, and `test_manual_retry_requeues_capture_after_retry_cap_failure`.

For a live confirmation with the downstream webhook deliberately broken:

1. Point `N8N_WEBHOOK_URL` at a non-existent endpoint.
2. Send a Discord message.
3. Wait for the stale-lease reaper to run (default 30-second interval).
4. Confirm the capture moves through `RETRY_WAIT` states via `uv run python -m secondbrain status`.
5. After the retry cap, confirm `captures failed: 1` and a Discord alert.
6. Run `uv run python -m secondbrain retry <capture-id>` and confirm the capture re-enters the delivery queue.

---

## Completion checklist

Mark each item when verified:

- [ ] Step 1 — full automated suite passes (`uv run pytest -q`)
- [ ] Step 2 — status-command slice passes and live command exits correctly
- [ ] Step 3a — Docker container builds and starts
- [ ] Step 3b — Discord capture test (live)
- [ ] Step 3c — live SQLite monitoring shows fresh heartbeat
- [ ] Step 3d — duplicate suppression (live)
- [ ] Step 3e — container recreation preserves all rows
- [ ] Step 3f — missing-volume fail-closed behavior
- [ ] Step 3g — clean shutdown writes STOPPED state
- [ ] Step 4a–4c — EC2 reboot persistence
- [ ] Step 5 — periodic skipped-Gateway recovery confirmed
- [ ] Step 6 — retry and lease recovery confirmed

Milestone 2 is closed when all boxes are checked.
