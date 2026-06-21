# SB-135 — Production Verification Checklist

These steps require a live EC2 environment with `GIT_SYNC_ENABLED=true` and a
real GitHub deploy key. They cannot be automated with unit or architecture tests.
Run this checklist before closing SB-135.

---

## Prerequisites

- `writer-service` deployed to EC2 via `docker compose -f docker-compose.n8n.yaml`
- Docker secret `vault_deploy_key` populated with a deploy key that has write
  access to the vault repository
- Docker secret `github_known_hosts` populated (see `deploy/github_known_hosts`)
- `GIT_SYNC_ENABLED=true` in writer-service environment

---

## Checklist

### 1. Service health

```bash
curl -f http://<ec2-host>:8001/health
# Expected: {"status":"ok"}
```

- [ ] `/health` returns 200 with `{"status":"ok"}`
- [ ] No `Permission denied` or `Host key verification failed` errors in container logs

### 2. Deploy key and known_hosts access

```bash
docker exec second-brain-writer-service ls -la /home/writeruser/.ssh/
# Expected: id_deploy (0600, writeruser), known_hosts (0644, writeruser)
```

- [ ] `/home/writeruser/.ssh/id_deploy` exists, permissions `0600`, owned by `writeruser`
- [ ] `/home/writeruser/.ssh/known_hosts` exists, owned by `writeruser`
- [ ] `GIT_SSH_COMMAND` is set to use `/home/writeruser/.ssh/id_deploy` with `StrictHostKeyChecking=yes`

### 3. git fetch / fast-forward

Trigger a note filing via n8n or direct API call and observe container logs:

```bash
docker logs second-brain-writer-service --follow
```

- [ ] `git fetch` completes without error
- [ ] `git merge --ff-only` (or equivalent fast-forward) succeeds
- [ ] No `Permission denied (publickey)` in logs

### 4. Commit and push

After filing a note with `GIT_SYNC_ENABLED=true`:

- [ ] A new commit appears in the remote vault repository
- [ ] Commit author is `Second Brain Writer <writer@second-brain.local>`
- [ ] `delivery_commit_hash` is populated on the capture row (verify via `/internal/captures/<id>`)

### 5. Fail-fast behavior (missing secret)

Deploy with the `vault_deploy_key` secret removed:

- [ ] Container exits immediately with a clear error message referencing the missing key
- [ ] No silent failure or indefinite hang

### 6. Local fake-remote mode (no deploy key)

With `GIT_SYNC_ENABLED=false` or using `docker-compose.override.yaml` (local
named-volume mode):

- [ ] Service starts and files notes without requiring `vault_deploy_key`
- [ ] No SSH errors in logs

---

## Sign-off

Complete this checklist on EC2 and update `SB-135.md` acceptance criteria
(`[ ] EC2 writer-service starts healthy` and `[ ] git fetch/fast-forward/commit/push`)
before closing the ticket.
