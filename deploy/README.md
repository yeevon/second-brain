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

Verify and set container-user ownership:

```bash
sudo umount /opt/second-brain/data
sudo mount -a
findmnt /opt/second-brain/data
sudo chown -R 10001:10001 /opt/second-brain/data
```

## Environment

Create the real environment file on the EC2 host:

```bash
sudo install -m 600 deploy/capture-service.env.example /opt/second-brain/config/capture-service.env
sudo nano /opt/second-brain/config/capture-service.env
sudo chmod 600 /opt/second-brain/config/capture-service.env
```

Generate the internal token with:

```bash
openssl rand -hex 32
```

Never commit the real Discord bot token or internal API token.

## Deploy

From `/opt/second-brain/app`:

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=200 capture-service
```

Expected log lines include:

```text
capture-service runtime mode: capture-only
downstream processing: disabled
capture-service API started on internal container port 8000
startup Discord history reconciliation complete
Discord listener ready
```

## Verify

Run:

```bash
deploy/verify.sh
```

Check the internal health endpoint from inside the container:

```bash
docker compose exec -T capture-service \
  /app/.venv/bin/python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read().decode())"
```

Confirm the API is not published to the host:

```bash
docker inspect --format '{{json .NetworkSettings.Ports}}' second-brain-capture-service
```

Expected shape:

```json
{"8000/tcp":null}
```

## Manual Acceptance Checks

1. Stop the desktop listener and confirm `pgrep -af "secondbrain"` does not show a local listener.
2. Post a phone message to the capture channel and confirm the durable-capture receipt appears from EC2.
3. Confirm `/opt/second-brain/data/ledger.sqlite3` exists after the first capture.
4. Run `docker compose down && docker compose up -d` and confirm the prior capture remains in SQLite.
5. Reboot EC2, then confirm Docker restarts the container and a second phone capture works.
6. Stop the container, post a message, start the container, and confirm startup reconciliation persists it once.
7. Post a test-only fake secret and confirm the plaintext value is absent from the SQLite dump.
