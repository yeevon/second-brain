# Second Brain EC2 Capture Service

This deployment runs only the durable Discord intake service on EC2. Classification, Markdown filing, n8n, Git sync, and vault writes remain disabled for SB-104.

## EC2 Shape

Use one Ubuntu LTS instance with:

- IMDSv2 required.
- SSH key-pair access.
- encrypted root volume.
- encrypted EBS data volume mounted at `/opt/second-brain/data`.
- security group inbound SSH from your public IP as `/32`.
- no inbound rules for `8000`, `5678`, `80`, or `443`.
- outbound internet enabled for Discord Gateway access.

### Verify EBS DeleteOnTermination

After launch, confirm the data volume will survive instance termination.
Use the EC2 attachment name (e.g. `/dev/sdf`) — not the in-instance NVMe path (e.g. `/dev/nvme1n1`):

```bash
aws ec2 describe-instances \
  --instance-ids <INSTANCE_ID> \
  --query "Reservations[].Instances[].BlockDeviceMappings[?DeviceName=='<DATA_ATTACHMENT_DEVICE>'].Ebs.DeleteOnTermination"
```

Expected: `false`. If not, update it:

```bash
aws ec2 modify-instance-attribute \
  --instance-id <INSTANCE_ID> \
  --block-device-mappings '[{"DeviceName":"<DATA_ATTACHMENT_DEVICE>","Ebs":{"DeleteOnTermination":false}}]'
```

This is separate from reboot persistence. `/etc/fstab` handles remounting after reboot. `DeleteOnTermination=false` protects the data volume if the EC2 instance itself is terminated and replaced.

### Verify IMDSv2 enforcement

```bash
aws ec2 describe-instances \
  --instance-ids <INSTANCE_ID> \
  --query 'Reservations[].Instances[].MetadataOptions.HttpTokens'
```

Expected: `"required"`. If not:

```bash
aws ec2 modify-instance-metadata-options \
  --instance-id <INSTANCE_ID> \
  --http-tokens required
```

### Verify SSH hardening

After logging in, confirm password authentication and root login are disabled:

```bash
sudo sshd -T | grep -E '^(passwordauthentication|pubkeyauthentication|permitrootlogin) '
```

Expected:

```text
passwordauthentication no
pubkeyauthentication yes
permitrootlogin no
```

`permitrootlogin prohibit-password` is acceptable on a standard Ubuntu image if documented.

### Verify security group

Confirm inbound rules allow only SSH from your intentional `/32` source and nothing else:

```bash
aws ec2 describe-security-groups \
  --group-ids <SG_ID> \
  --query 'SecurityGroups[].IpPermissions'
```

Expected: one rule — TCP port 22 from your `/32` IP. No rules for ports `8000`, `5678`, `80`, or `443`.

Do not run the desktop listener while this EC2 service owns Discord intake.

## Host Provisioning

Copy or clone this repository onto the EC2 host, then run:

```bash
deploy/provision-host.sh
```

Create and mount the encrypted data volume manually. Inspect device names first:

```bash
lsblk
sudo mkfs.ext4 /dev/<DATA_DEVICE>
sudo mount /dev/<DATA_DEVICE> /opt/second-brain/data
sudo blkid /dev/<DATA_DEVICE>
```

Add the UUID to `/etc/fstab`:

```text
UUID=<DATA_VOLUME_UUID> /opt/second-brain/data ext4 defaults,nofail 0 2
```

Verify the mount and set container-user ownership:

```bash
sudo umount /opt/second-brain/data
sudo mount -a
findmnt /opt/second-brain/data
sudo chown -R 10001:10001 /opt/second-brain/data
```

Only after `findmnt` confirms the EBS filesystem is mounted, create the n8n data subdirectory on the EBS volume (n8n container runs as UID 1000):

```bash
sudo mkdir -p /opt/second-brain/data/n8n
sudo chown -R 1000:1000 /opt/second-brain/data/n8n
```

Do not create this directory before the EBS mount is confirmed. If created on the root filesystem first, the mount point hides it and `deploy/deploy.sh` will exit with `n8n data directory missing`.

Only after `findmnt` confirms the EBS filesystem is mounted, create the sentinel file on the EBS volume:

```bash
sudo touch /opt/second-brain/data/.second-brain-ebs-volume
sudo chown 10001:10001 /opt/second-brain/data/.second-brain-ebs-volume
sudo chmod 600 /opt/second-brain/data/.second-brain-ebs-volume
```

The container entrypoint refuses to start if this file is absent. When the EBS mount is missing on reboot, the file is not present in the fallback root-volume directory, and the container exits immediately rather than writing to the wrong filesystem.

## Environment

Create the real environment file on the EC2 host (the deploy user owns `/opt/second-brain/config` after provisioning):

```bash
install -m 600 deploy/capture-service.env.example /opt/second-brain/config/capture-service.env
nano /opt/second-brain/config/capture-service.env
```

Generate the internal token with:

```bash
openssl rand -hex 32
```

Never commit the real Discord bot token or internal API token.

## Deploy

From `/opt/second-brain/app`:

```bash
deploy/deploy.sh
```

`deploy/deploy.sh` exports `CAPTURE_SERVICE_ENV_FILE`, `CAPTURE_DATA_SOURCE`, and `COMPOSE_FILE=compose.yaml`, then verifies the EBS mount and sentinel before building and starting the container.

## Verify

```bash
deploy/verify.sh
```

`deploy/verify.sh` confirms container running, `unless-stopped` restart policy, non-root user, port 8000 not published to host, sentinel present, ledger present, and container health `healthy`.

Expected output: `capture-service deployment checks passed`.

For subsequent direct `docker compose` operations in this shell:

```bash
export CAPTURE_SERVICE_ENV_FILE=/opt/second-brain/config/capture-service.env
export CAPTURE_DATA_SOURCE=/opt/second-brain/data
export COMPOSE_FILE=compose.yaml
```

## Manual Acceptance Checks

1. Stop the desktop listener and confirm `pgrep -af "secondbrain"` does not show a local listener.
2. Post a phone message to the capture channel and confirm the durable-capture receipt appears from EC2.
3. Confirm `/opt/second-brain/data/ledger.sqlite3` exists after the first capture.
4. Run `deploy/deploy.sh` (redeploy) and confirm the prior capture remains in SQLite.
5. Reboot EC2, then confirm Docker restarts the container and a second phone capture works.
6. Stop the container (`docker compose stop capture-service`), post a message, run `deploy/deploy.sh`, and confirm startup reconciliation persists it once.
7. Post a test-only fake secret and confirm the plaintext value is absent from the SQLite dump.

---

## n8n Orchestration Layer (SB-111+)

n8n is added alongside capture-service as of SB-111. Key facts about the foundation deployment:

- **Persistent** — workflows, credentials, and the owner account survive container restarts. State lives on EBS at `/opt/second-brain/data/n8n`.
- **Single instance** — one n8n process with `N8N_CONCURRENCY_PRODUCTION_LIMIT=1`. No workers, no queue mode.
- **SQLite during foundation phase** — n8n uses its own internal SQLite database. PostgreSQL migration is future work, required before adding horizontal scaling or queue workers.
- **Explicit encryption key** — n8n credentials are encrypted with a key stored at `/opt/second-brain/config/n8n-encryption-key`. Generate once with `openssl rand -hex 32`. Losing the key loses all stored credentials.
- **Private SSH-tunnel access** — the UI is accessible only through an SSH tunnel. Port 5678 is bound to `127.0.0.1` only; no public security-group rule is added.
- **Execution payloads not retained globally** — `EXECUTIONS_DATA_SAVE_ON_ERROR=none` and `EXECUTIONS_DATA_SAVE_ON_SUCCESS=none` are the global defaults. Raw capture text must never appear in n8n storage.
- **Error Trigger workflow** — `Second Brain - Error Handler` is bootstrapped once via `deploy/bootstrap-n8n.sh`. It normalizes safe metadata only; never retains capture text, stack traces, or raw exception messages.

### Access the n8n editor from the desktop

```bash
deploy/open-n8n-tunnel.sh <EC2_HOST>
```

Then open `http://127.0.0.1:5678` in the browser. The tunnel must remain open while using the editor.

### Bootstrap workflows after first login

After creating the owner account in the UI, run on EC2:

```bash
deploy/bootstrap-n8n.sh
```

This is idempotent — running it again when `Second Brain - Error Handler` already exists exits cleanly. Imported workflows start in **inactive** state; activate them manually after binding credentials in the UI.

### n8n environment file

Create `/opt/second-brain/config/n8n.env` from `deploy/n8n.env.example`. Do not weaken the execution retention settings for debugging — use synthetic captures instead.

### n8n encryption key

```bash
install -m 600 /dev/null /opt/second-brain/config/n8n-encryption-key
nano /opt/second-brain/config/n8n-encryption-key
```

Populate only the generated value. Do not print, log, or commit the key.

### Additional verify.sh checks (SB-111)

`deploy/verify.sh` now confirms all of the following in addition to the capture-service checks:

```text
second-brain-n8n container exists and is running
restart policy = unless-stopped
container user is not root
image tag is pinned (not latest, not next)
host binding is 127.0.0.1:5678 only
/home/node/.n8n is mounted from /opt/second-brain/data/n8n
n8n data directory exists on EBS
n8n encryption key file exists, is non-empty, and has 600 permissions
n8n responds on EC2 loopback
n8n can reach capture-service /health over backend network
```

Expected final output:

```text
capture-service deployment checks passed
n8n foundation deployment checks passed
```

### If HTTPS access is added later

SSH-tunnel mode is the SB-111 private-access implementation. If a reverse proxy or private HTTPS layer is added, update:

```dotenv
N8N_PROTOCOL=https
N8N_SECURE_COOKIE=true
N8N_EDITOR_BASE_URL=https://<N8N_HOSTNAME>/
WEBHOOK_URL=https://<N8N_HOSTNAME>/
N8N_PROXY_HOPS=1
```

The final proxy must pass `X-Forwarded-For`, `X-Forwarded-Host`, and `X-Forwarded-Proto`. Do not add these values prematurely in tunnel mode.

### Direct compose operations after deploy.sh

```bash
export CAPTURE_SERVICE_ENV_FILE=/opt/second-brain/config/capture-service.env
export CAPTURE_DATA_SOURCE=/opt/second-brain/data
export N8N_IMAGE_TAG=<pinned-version>
export N8N_ENV_FILE=/opt/second-brain/config/n8n.env
export N8N_ENCRYPTION_KEY_FILE=/opt/second-brain/config/n8n-encryption-key
export N8N_DATA_SOURCE=/opt/second-brain/data/n8n
export COMPOSE_FILE=compose.yaml:compose.n8n.yaml
```
