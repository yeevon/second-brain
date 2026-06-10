# SB-110 — Milestone 2 Exit Regression Runbook

This document is the completion gate for Milestone 2. Every item must pass before the milestone is closed.

**Automated** items have permanent test coverage; the suite is the proof.
**Manual** items require infrastructure (Docker, EC2) and must be executed at release time.
**Deferred** items have no live adapter yet; the automated proof is the accepted substitute.

---

## Coverage table

| Exit criterion | Classification | Evidence |
| --- | --- | --- |
| Full automated suite | Automated | `uv run pytest -q` |
| Status-command validation | Automated + Manual | `tests/unit/test_status.py`, `tests/unit/test_heartbeat.py`, live container exec |
| Discord capture tests | Manual | Steps 3b, 4 |
| Duplicate suppression | Automated | `test_duplicate_discord_event_is_idempotent` and friends |
| Sensitive-input rejection | Automated | `test_secret_like_input_stores_redacted_rejection_only` and friends |
| Offline reconciliation | Automated | `test_capture_only_reconciliation_failure_allows_retry` and friends |
| Periodic skipped-Gateway recovery | Automated | `test_skipped_gateway_event_is_recovered_by_periodic_reconciliation` |
| Retry and lease recovery | Automated | `test_schedule_retry_makes_capture_reclaimable` and friends |
| Clean shutdown | Automated + Manual | `test_sigterm_style_shutdown_closes_service_cleanly`, Step 3g |
| Live SQLite monitoring | Manual proxy | Steps 3c (three terminal watches) |
| Container recreation | Manual | Step 3e |
| Missing-volume fail-closed | Manual | Step 3f |
| EC2 reboot persistence | Manual | Step 4 |
| Downstream delivery (retry, n8n) | Deferred | Automated reaper tests; no live n8n adapter exists yet |

---

## Step 1 — Full automated suite

```bash
uv run pytest -q
```

Expected: all tests pass, zero failures, zero errors.

---

## Step 2 — SB-109 status-command slice

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

Expected: all tests pass.

---

## Step 3 — Local Docker regression

### 3a — Prepare a disposable local environment

Create a temporary data directory and set the three shell variables used throughout the Docker steps:

```bash
export CAPTURE_DATA_DIR=/tmp/sb-local-docker/data
export CAPTURE_SERVICE_ENV_FILE=/tmp/sb-local-docker/capture-service.env
export HOST_LEDGER=$CAPTURE_DATA_DIR/ledger.sqlite3

mkdir -p "$CAPTURE_DATA_DIR"
cp deploy/capture-service.env.example "$CAPTURE_SERVICE_ENV_FILE"
```

Edit `$CAPTURE_SERVICE_ENV_FILE` and fill in the required values:

```text
DISCORD_BOT_TOKEN=<real bot token>
DISCORD_GUILD_ID=<real guild ID>
DISCORD_CAPTURE_CHANNEL_ID=<real channel ID>
DISCORD_ALLOWED_USER_ID=<real user ID>
CAPTURE_SERVICE_INTERNAL_TOKEN=<output of: openssl rand -hex 32>
```

Create the EBS sentinel marker and set container-user ownership:

```bash
touch "$CAPTURE_DATA_DIR/.second-brain-ebs-volume"
sudo chown -R 10001:10001 "$CAPTURE_DATA_DIR"
```

Build the image and start the container:

```bash
docker compose build
docker compose up -d
```

Confirm the container starts and does not exit within 30 seconds:

```bash
docker compose ps
```

Expected: `second-brain-capture-service` shows `Up`.

### 3b — Open three monitoring terminals

Open three separate terminals with `CAPTURE_DATA_DIR` and `HOST_LEDGER` exported, then run one watch in each.

**Terminal 1 — captures:**

```bash
watch -n 5 "sqlite3 '$HOST_LEDGER' 'SELECT id, status, discord_message_id, substr(raw_text,1,40) FROM captures ORDER BY id DESC LIMIT 10;'"
```

**Terminal 2 — capture_events:**

```bash
watch -n 5 "sqlite3 '$HOST_LEDGER' 'SELECT id, capture_id, event_type, created_at FROM capture_events ORDER BY id DESC LIMIT 10;'"
```

**Terminal 3 — system_state:**

```bash
watch -n 5 "sqlite3 '$HOST_LEDGER' 'SELECT key, value, updated_at FROM system_state;'"
```

### 3c — Discord capture test

Send a test message in the configured Discord channel from the allowed user account.

Confirm within 60 seconds:

1. A durable receipt appears in Discord (bot reply with `⏳ SB-…`).
2. In capture-only mode, no vault write occurs.

Confirm in logs:

```bash
docker compose logs --tail=50 capture-service
```

Expected: `{"event":"capture_received",...}` log line, no error events.

Confirm in the monitoring terminals:

- Terminal 1: a new row appears with `status = RECEIVED`.
- Terminal 2: a `capture_accepted` event appears.
- Terminal 3: `capture_service_last_heartbeat_at` advances.

### 3d — Live SQLite monitoring via status command

Run the status command from inside the running container:

```bash
docker compose exec -T capture-service \
  /app/.venv/bin/python -m secondbrain status
echo "exit code: $?"
```

Expected: output contains all sections (`Capture intake`, `Note lifecycle`, `Delivery backlog`, `Discord reconciliation`, `Capture service`). Health shows `HEALTHY`, heartbeat within the last 60 seconds.

Expected exit codes:

- `0` — service running with a fresh heartbeat (HEALTHY)
- `1` — any non-HEALTHY capture-service state (STARTING, STOPPED, STALE, UNKNOWN) or operational anomaly
- `2` — ledger cannot be read safely

### 3e — Container recreation

Record the current capture count:

```bash
sqlite3 "$HOST_LEDGER" "SELECT COUNT(*) FROM captures;"
```

Stop and remove the container while keeping the bind-mounted data directory:

```bash
docker compose stop capture-service
docker compose rm -f capture-service
docker compose up -d capture-service
```

Confirm after restart:

1. Container is `Up`:

   ```bash
   docker compose ps
   ```

2. Status command returns HEALTHY with a new instance ID:

   ```bash
   docker compose exec -T capture-service \
     /app/.venv/bin/python -m secondbrain status
   ```

3. Total captures matches the count before recreation:

   ```bash
   sqlite3 "$HOST_LEDGER" "SELECT COUNT(*) FROM captures;"
   ```

### 3f — Missing-volume fail-closed behavior

Run the image without the data volume mounted so the EBS sentinel is absent:

```bash
docker run --rm \
  --env-file "$CAPTURE_SERVICE_ENV_FILE" \
  second-brain-capture-service:local
```

Expected: container exits immediately (within a few seconds) with exit code `1` and the message:

```text
persistent EBS volume marker missing: /var/lib/second-brain/.second-brain-ebs-volume
```

No silent data loss. No service startup.

### 3g — Clean shutdown

Send SIGTERM to the container:

```bash
docker compose stop --timeout 15 capture-service
```

Inspect the final log lines:

```bash
docker compose logs --tail=20 capture-service
```

Expected log events include:

```text
{"event":"capture_service_stopped",...}
```

Expected: container exits with code `0`.

Confirm the ledger shows `STOPPED` state:

```bash
sqlite3 "$HOST_LEDGER" \
  "SELECT value FROM system_state WHERE key='capture_service_state';"
```

Expected: `STOPPED`.

### 3h — Tear down the local environment

```bash
docker compose down
sudo rm -rf /tmp/sb-local-docker
```

---

## Step 4 — EC2 reboot persistence

### 4a — Security pre-checks

Before deploying, verify the instance configuration matches the deployment requirements documented in `deploy/README.md`.

Confirm EBS `DeleteOnTermination=false`:

```bash
aws ec2 describe-instances \
  --instance-ids <INSTANCE_ID> \
  --query "Reservations[].Instances[].BlockDeviceMappings[?DeviceName=='<DATA_ATTACHMENT_DEVICE>'].Ebs.DeleteOnTermination"
```

Expected: `false`.

Confirm IMDSv2 enforcement:

```bash
aws ec2 describe-instances \
  --instance-ids <INSTANCE_ID> \
  --query 'Reservations[].Instances[].MetadataOptions.HttpTokens'
```

Expected: `"required"`.

Confirm SSH hardening on the host:

```bash
sudo sshd -T | grep -E '^(passwordauthentication|pubkeyauthentication|permitrootlogin) '
```

Expected:

```text
passwordauthentication no
pubkeyauthentication yes
permitrootlogin no
```

Confirm security group allows only SSH from the intended `/32` source:

```bash
aws ec2 describe-security-groups \
  --group-ids <SG_ID> \
  --query 'SecurityGroups[].IpPermissions'
```

Expected: one rule — TCP port 22 from your `/32` IP. No rules for ports 8000, 5678, 80, or 443.

### 4b — Deploy to EC2

On the EC2 host, from `/opt/second-brain/app`:

```bash
git pull
deploy/deploy.sh
```

`deploy/deploy.sh` verifies the EBS mount and sentinel before building and starting the container. It will exit with an error if either check fails.

Run the post-deploy verification:

```bash
deploy/verify.sh
```

`deploy/verify.sh` checks: container running, `unless-stopped` restart policy, non-root user, port 8000 not published, sentinel present, ledger exists, container health `healthy`.

Expected output: `capture-service deployment checks passed`.

Send a test capture and confirm it is durably saved:

```bash
docker compose exec -T capture-service \
  /app/.venv/bin/python -m secondbrain status
```

Expected: health `HEALTHY`, `total captures` incremented.

Record the capture count before rebooting:

```bash
sqlite3 /opt/second-brain/data/ledger.sqlite3 "SELECT COUNT(*) FROM captures;"
```

### 4c — Reboot the instance

```bash
sudo reboot
```

### 4d — Post-reboot verification

After the instance restarts and the container comes back up (typically 1–3 minutes):

Run the post-deploy verification again:

```bash
deploy/verify.sh
```

Check the status command:

```bash
docker compose exec -T capture-service \
  /app/.venv/bin/python -m secondbrain status
```

Expected: health `HEALTHY`, new instance ID, total captures unchanged from before reboot.

Confirm no captures were lost:

```bash
sqlite3 /opt/second-brain/data/ledger.sqlite3 "SELECT COUNT(*) FROM captures;"
```

Send a second test capture post-reboot and confirm the receipt appears in Discord.

---

## Step 5 — Periodic skipped-Gateway recovery (automated)

This criterion is fully covered by the automated suite.

Primary test: `test_skipped_gateway_event_is_recovered_by_periodic_reconciliation`

No live procedure is required. The test exercises the full recovery path: missed message in channel history → periodic reconciliation → `RECEIVED` row inserted.

---

## Step 6 — Retry and lease recovery (automated)

This criterion is fully covered by the automated suite. No live n8n adapter is wired; the deferred classification applies.

Primary tests:

- `test_schedule_retry_makes_capture_reclaimable`
- `test_schedule_retry_reaches_terminal_failure_at_cap`
- `test_repeated_fake_downstream_crashes_eventually_fail_visibly`
- `test_manual_retry_requeues_capture_after_retry_cap_failure`
- `test_reaper_receipt_alert_runs_only_after_database_commit`
- `test_status_reports_retry_backlog_without_manual_sqlite_query` (integration — real reaper)

---

## Completion checklist

- [ ] Step 1 — full automated suite passes (`uv run pytest -q`)
- [ ] Step 2 — status-command slice passes
- [ ] Step 3a — local Docker environment prepared, container starts
- [ ] Step 3b — three monitoring terminals open and showing live data
- [ ] Step 3c — Discord capture test passes (live receipt in Discord)
- [ ] Step 3d — status command inside container exits 0, health `HEALTHY`
- [ ] Step 3e — container recreation preserves all rows
- [ ] Step 3f — missing-volume fail-closed: exits 1 with correct message
- [ ] Step 3g — clean shutdown writes `STOPPED` state, container exits 0
- [ ] Step 4a — EC2 security pre-checks pass
- [ ] Step 4b — `deploy/deploy.sh` and `deploy/verify.sh` pass
- [ ] Step 4c/4d — EC2 reboot persistence: row count preserved, service restarts

Milestone 2 is closed when all boxes are checked and evidence is recorded at `docs/Milestones/002/evidence/SB-110-YYYY-MM-DD.md`.

---

## Evidence record

Create one file per execution at `docs/Milestones/002/evidence/SB-110-YYYY-MM-DD.md` using the template below. Fill in each field before closing the milestone.

```markdown
# SB-110 Evidence — YYYY-MM-DD

## Automated suite

- Commit: <git sha>
- Command: `uv run pytest -q`
- Result: <N> passed, 0 failed, 0 errors

## Local Docker regression

- Image built from commit: <git sha>
- CAPTURE_DATA_DIR: /tmp/sb-local-docker/data
- Step 3c: Discord capture received — message ID: <id>, capture ID: <SB-xxx>
- Step 3d: status exit code: 0, health: HEALTHY
- Step 3e: row count before recreation: <N>, row count after: <N>
- Step 3f: container exited code 1 with message "persistent EBS volume marker missing"
- Step 3g: container exited code 0, system_state shows STOPPED

## EC2 reboot persistence

- Instance ID: <EC2 instance ID>
- deploy/verify.sh: passed
- Row count before reboot: <N>
- Row count after reboot: <N>
- Post-reboot Discord capture: <message ID>

## Deferred criteria

- Downstream delivery (retry, n8n): no live adapter; covered by automated reaper tests
```
