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
