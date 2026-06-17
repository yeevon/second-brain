# Second Brain Operations Runbook

This runbook covers day-to-day operation of the Second Brain system deployed on EC2 via Docker Compose.

---

## Service management

| Command | Effect |
|---------|--------|
| `docker compose up -d` | Start all services in detached mode |
| `docker compose down` | Stop all services; named volumes preserved |
| `docker compose down -v` | **Destructive** — stops services and deletes all volume data |
| `docker compose up -d --build` | Rebuild images and restart all services |
| `docker compose restart <service>` | Restart a single service |
| `docker compose logs -f <service>` | Stream logs for a service |
| `docker compose ps` | Show running state and health of all services |

Services: `capture-service`, `writer-service`, `n8n`.

---

## Health checks

Run these from the EC2 host:

```bash
curl http://localhost:8000/health     # capture-service
curl http://localhost:8001/health     # writer-service
uv run python -m secondbrain status   # full operational status (requires .env)
```

`secondbrain status` reports:
- Capture counts (total, today, filed, inbox, failed)
- Delivery backlog and stale leases
- Discord reconciliation state
- Capture-service health (HEALTHY / STALE / STOPPED / UNKNOWN)
- Background task liveness (reaper, reconcile)
- Backup timestamps (last successful backup, last restore validation)

Exit codes: 0 = success, 1 = invocation error, 2 = ledger not found.

---

## Manual retry

Retry a failed capture by its ID:

```bash
uv run python -m secondbrain retry SB-YYYYMMDD-NNNN
```

Exit code 0 = queued for redelivery. Exit code 1 = not found or not in FAILED state.

---

## Accessing n8n

n8n is accessible via SSH tunnel only — port 5678 is not published to the public internet.

**Open a tunnel:**
```bash
ssh -N -L 5678:127.0.0.1:5678 ubuntu@<EC2_HOST>
```

Then open `http://127.0.0.1:5678` in your browser.

Port 5678 must not appear in the EC2 security group inbound rules. Verify with:
```bash
aws ec2 describe-security-groups --group-ids <sg-id> \
  --query 'SecurityGroups[*].IpPermissions[?ToPort==`5678`]'
```

---

## Backup and restore

### Nightly backup

Backups run automatically via cron (`/etc/cron.d/second-brain-backup`) at 02:00 UTC nightly. Each run encrypts and writes:

- SQLite ledger (`ledger-TIMESTAMP.sqlite3.gpg`)
- Vault git bundle (`vault-TIMESTAMP.bundle.gpg`)
- n8n data volume (`n8n-data-TIMESTAMP.tar.gz.gpg`, if `N8N_DATA_DIR` set)

Cron output is logged to `/var/log/second-brain-backup.log`.

Manual backup run:
```bash
set -a && . /opt/second-brain/config/backup.env && set +a
/opt/second-brain/app/deploy/backup.sh
```

### Restore validation

Restore validation runs weekly (Sundays 03:00 UTC) and decrypts a recent backup into a temporary directory without touching live volumes.

Manual restore validation:
```bash
set -a && . /opt/second-brain/config/backup.env && set +a
/opt/second-brain/app/deploy/restore-validate.sh
```

A successful run writes `last_successful_restore_validation_at` into the ledger, visible via `secondbrain status`.

**Never run restore validation against live volumes.** The script uses `mktemp -d` and cleans up on exit.

---

## Common failure modes

| Symptom | Likely cause | Resolution |
|---------|-------------|------------|
| No receipt after Discord message | capture-service down or Discord token invalid | Check `docker compose logs capture-service`; verify `DISCORD_BOT_TOKEN` |
| Receipt stuck at ⏳ | n8n down or webhook misconfigured | Check n8n health via SSH tunnel; verify `N8N_INTAKE_WEBHOOK_URL` in capture-service env |
| Filing failed after classification | writer-service down or Git push rejected | Check `docker compose logs writer-service`; run `secondbrain retry <ID>` |
| Stale `.git/index.lock` in vault | writer-service crashed mid-commit | writer-service will refuse to start; investigate before removing; see below |
| `secondbrain status` exits 2 | SQLite ledger not found | Verify `LEDGER_PATH` env var and the volume mount in `compose.yaml` |
| Backup log shows GPG error | GPG key not imported on host | Import key: `gpg --import /path/to/key.asc` |
| Discord message not captured after restart | Reconciliation gap exceeded | Run `secondbrain status` to check last reconciled message ID; reprocess window may need extending |

### Stale `.git/index.lock` repair

A stale lock means a write was interrupted. Do not delete the lock blindly without first verifying no writer-service process is holding it:

```bash
docker compose ps writer-service          # confirm it is stopped
ls -la /opt/second-brain/vault/.git/index.lock
# If writer-service is stopped and lock is old (>60s), safe to remove:
rm /opt/second-brain/vault/.git/index.lock
docker compose start writer-service
```

---

## Credential isolation

| Credential | Service | Env var |
|-----------|---------|---------|
| Discord bot token | capture-service only | `DISCORD_BOT_TOKEN` |
| GitHub deploy key / PAT | writer-service only | mounted as SSH key; `GIT_SYNC_ENABLED=true` |
| Gemini API key | n8n only | n8n credential store |
| Internal API token | capture-service + n8n | `CAPTURE_SERVICE_INTERNAL_TOKEN` / `N8N_INTAKE_WEBHOOK_TOKEN` |
| Writer service token | writer-service + capture-service | `WRITER_SERVICE_TOKEN` |

The SQLite ledger is mounted only into capture-service. Git credentials are not mounted into n8n or capture-service.

---

## EC2 security checklist

Run periodically to verify the production security posture:

```bash
# SSH password auth must be disabled
grep PasswordAuthentication /etc/ssh/sshd_config     # expect: no

# No internal service ports published publicly
docker compose ps --format json | python3 -c "
import sys, json
for svc in json.load(sys.stdin):
    ports = svc.get('Publishers', [])
    for p in ports:
        if p.get('PublishedPort') and p['URL'] == '0.0.0.0':
            print(f'WARNING: {svc[\"Name\"]} publishes {p[\"PublishedPort\"]} to 0.0.0.0')
"
```

---

## Deployment steps (initial or re-deploy)

```bash
# On EC2 host
cd /opt/second-brain/app
git pull origin main

# Rebuild and restart
docker compose down
docker compose up -d --build

# Verify health
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8001/health
```

---

## Log retention

Container logs use `json-file` driver with `max-size: 10m` and `max-file: 3`, capping each service at ~30 MB. Adjust in `compose.yaml` under each service's `logging` block if needed.

Backup logs: `/var/log/second-brain-backup.log` and `/var/log/second-brain-restore-validate.log`. Rotate with `logrotate` if needed.
