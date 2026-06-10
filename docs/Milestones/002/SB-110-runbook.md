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
| Discord capture tests | Manual | Steps 3c, 4 |
| Sensitive-input rejection | Automated + Manual | `test_secret_like_input_stores_redacted_rejection_only` and friends, Step 3d |
| Offline reconciliation | Automated + Manual | `test_capture_only_reconciliation_failure_allows_retry` and friends, Step 3g |
| Duplicate suppression | Automated | `test_duplicate_discord_event_is_idempotent` and friends |
| Periodic skipped-Gateway recovery | Automated | `test_skipped_gateway_event_is_recovered_by_periodic_reconciliation` |
| Retry and lease recovery | Automated | `test_schedule_retry_makes_capture_reclaimable` and friends |
| Clean shutdown | Automated + Manual | `test_sigterm_style_shutdown_closes_service_cleanly`, Step 3i |
| Live SQLite monitoring | Manual | Steps 3b (three terminal watches) |
| Container recreation | Manual | Step 3f |
| Missing-volume fail-closed | Manual | Step 3h |
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
chmod 600 "$CAPTURE_SERVICE_ENV_FILE"
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

Check the internal health endpoint from inside the container:

```bash
docker compose exec -T capture-service \
  /app/.venv/bin/python -c \
  "import urllib.request; print(
      urllib.request.urlopen(
          'http://127.0.0.1:8000/health',
          timeout=2,
      ).read().decode()
  )"
```

Expected: `{"status":"ok","service":"capture-service"}`

Confirm port 8000 is not published to the host:

```bash
docker inspect \
  --format '{{json .NetworkSettings.Ports}}' \
  second-brain-capture-service
```

Expected: `{"8000/tcp":null}`

### 3b — Open three monitoring terminals

Open three separate terminals with `CAPTURE_DATA_DIR` and `HOST_LEDGER` exported, then run one watch in each. Do not print note text.

**Terminal 1 — captures (metadata only):**

```bash
watch -n 5 "
sqlite3 '$HOST_LEDGER' \"
.headers on
.mode column

SELECT
  capture_id,
  discord_message_id,
  status,
  delivery_status,
  delivery_attempts,
  retry_attempts,
  is_sensitive,
  receipt_message_id,
  updated_at
FROM captures
ORDER BY id DESC
LIMIT 10;
\"
"
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
- Terminal 2: a `CAPTURE_RECEIVED` event appears.
- Terminal 3: `capture_service_last_heartbeat_at` advances.

### 3d — Sensitive-input rejection

Post this disposable test-only value from Discord in the capture channel:

```text
password=TEST_ONLY_FAKE_SECRET_123456
```

Query the ledger to confirm the capture was rejected cleanly:

```bash
sqlite3 "$HOST_LEDGER" "
SELECT
  capture_id,
  status,
  is_sensitive,
  raw_text IS NULL AS raw_text_is_null,
  instr(redacted_text, '[REDACTED]') > 0 AS redaction_present
FROM captures
ORDER BY id DESC
LIMIT 1;
"
```

Expected:

```text
status = REJECTED_SENSITIVE
is_sensitive = 1
raw_text_is_null = 1
redaction_present = 1
```

Confirm the fake value is absent from container logs:

```bash
docker compose logs capture-service | \
  grep -F "TEST_ONLY_FAKE_SECRET_123456" && \
  echo "FAIL: plaintext found in logs" || \
  echo "PASS: plaintext absent from logs"
```

Confirm it is absent from all mounted database files (including WAL):

```bash
grep -R -a -F "TEST_ONLY_FAKE_SECRET_123456" \
  "$CAPTURE_DATA_DIR" && \
  echo "FAIL: plaintext found in data directory" || \
  echo "PASS: plaintext absent from data directory"
```

Expected: both checks print `PASS`.

### 3e — Live SQLite monitoring via status command

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

### 3f — Container recreation

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

### 3g — Offline reconciliation

Stop the running container:

```bash
docker compose stop capture-service
```

Post one normal Discord thought from your phone while the container is stopped.

Note the Discord message ID of that message.

Restart the container:

```bash
docker compose up -d capture-service
```

Wait until the service is healthy:

```bash
docker compose ps
```

Verify exactly one row for the offline message:

```bash
sqlite3 "$HOST_LEDGER" "
SELECT COUNT(*)
FROM captures
WHERE discord_message_id = '<offline-message-id>';
"
```

Expected: `1`

Confirm in logs that startup reconciliation processed it:

```bash
docker compose logs --tail=50 capture-service | grep reconcil
```

Expected: a log line showing `handled: 1` for the startup reconciliation pass. No duplicate receipt in Discord.

### 3h — Missing-volume fail-closed behavior

Create a clean directory that has no sentinel file:

```bash
rm -rf /tmp/sb-local-docker/invalid-data
mkdir -p /tmp/sb-local-docker/invalid-data
```

Run the image with that directory mounted:

```bash
set +e

docker run --rm \
  --mount type=bind,src=/tmp/sb-local-docker/invalid-data,dst=/var/lib/second-brain \
  --env-file "$CAPTURE_SERVICE_ENV_FILE" \
  second-brain-capture-service:local

exit_code=$?

set -e

echo "exit code: $exit_code"
```

Expected: `exit code: 1`, with the message:

```text
persistent EBS volume marker missing: /var/lib/second-brain/.second-brain-ebs-volume
```

Confirm no SQLite database was created in the invalid directory:

```bash
test ! -e /tmp/sb-local-docker/invalid-data/ledger.sqlite3 && \
  echo "PASS: no ledger created" || \
  echo "FAIL: ledger found in invalid directory"
```

Expected: `PASS: no ledger created`

### 3i — Clean shutdown

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

Inspect the container exit code:

```bash
docker inspect \
  --format '{{.State.ExitCode}}' \
  second-brain-capture-service
```

Expected: `0`

Confirm the ledger shows `STOPPED` state:

```bash
sqlite3 "$HOST_LEDGER" \
  "SELECT value FROM system_state WHERE key='capture_service_state';"
```

Expected: `STOPPED`

### 3j — Tear down the local environment

Only delete the disposable directory after evidence has been recorded. If any step above failed, preserve it temporarily for diagnosis.

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

`deploy/deploy.sh` verifies the EBS mount and sentinel before building and starting the container. It exits with an error if either check fails.

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
- [ ] Step 3a — local Docker environment prepared, container starts, health endpoint ok, port 8000 not published
- [ ] Step 3b — three monitoring terminals open and showing live data
- [ ] Step 3c — Discord capture test passes (live receipt in Discord, `CAPTURE_RECEIVED` event in Terminal 2)
- [ ] Step 3d — sensitive-input rejection: `REJECTED_SENSITIVE`, plaintext absent from logs and data directory
- [ ] Step 3e — status command inside container exits 0, health `HEALTHY`
- [ ] Step 3f — container recreation preserves all rows
- [ ] Step 3g — offline reconciliation: offline message persisted exactly once, no duplicate receipt
- [ ] Step 3h — missing-volume fail-closed: exits 1 with correct message, no ledger created
- [ ] Step 3i — clean shutdown: exit code 0, `STOPPED` state in ledger
- [ ] Step 4a — EC2 security pre-checks pass
- [ ] Step 4b — `deploy/deploy.sh` and `deploy/verify.sh` pass
- [ ] Step 4c/4d — EC2 reboot persistence: row count preserved, service restarts

Milestone 2 is closed when all boxes are checked and evidence is recorded at `docs/Milestones/002/evidence/SB-110-YYYY-MM-DD.md`.

---

## Evidence record

Create one file per execution at `docs/Milestones/002/evidence/SB-110-YYYY-MM-DD.md` using the template below. Fill in each field before closing the milestone. Never record real token values or the text of personal captures.

```markdown
# SB-110 Evidence — YYYY-MM-DD

## Environment

- Docker version: <output of: docker version --format '{{.Server.Version}}'>
- Docker Compose version: <output of: docker compose version>
- Image ID: <output of: docker inspect --format '{{.Id}}' second-brain-capture-service>

## Automated suite

- Commit: <git sha>
- Command: `uv run pytest -q`
- Result: <N> passed, 0 failed, 0 errors

## Local Docker regression

- Image built from commit: <git sha>
- CAPTURE_DATA_DIR: /tmp/sb-local-docker/data
- Step 3a: internal health endpoint: passed
- Step 3a: internal API host publication: `{"8000/tcp":null}`
- Step 3c: Discord capture received — message ID: <id>, capture ID: <SB-xxx>
- Step 3d: sensitive test rejection: plaintext absent from logs and mounted data directory
- Step 3e: status exit code: 0, health: HEALTHY
- Step 3f: row count before recreation: <N>, row count after: <N>
- Step 3g: offline message ID: <id>, persisted exactly once
- Step 3h: container exited code 1 with message "persistent EBS volume marker missing", no ledger created
- Step 3i: container exit code after SIGTERM: 0, capture_service_state: STOPPED

## EC2 reboot persistence

- Instance ID: <EC2 instance ID>
- EBS volume ID: <id>
- EBS DeleteOnTermination: false
- Security group ID: <id>
- IMDSv2: required
- SSH password authentication: disabled
- deploy/verify.sh before reboot: passed
- Row count before reboot: <N>
- Row count after reboot: <N>
- deploy/verify.sh after reboot: passed
- Post-reboot Discord capture: <message ID>

## Deferred criteria

- Downstream delivery (retry, n8n): no live adapter; covered by automated reaper tests
```
