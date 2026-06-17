# Milestone 9: EC2 Production Deployment and Operations Hardening

Deploys the full system to EC2 production after V3 (Milestone 7) and tech-debt cleanup (Milestone 8) are complete. The system must be stable and hardened before continuous production operation begins.

---

## SB-126 — Deploy V3/M8-hardened system to EC2 with security hardening

**Branch:** `release/production`

See [SB-126.md](SB-126.md) for the full spec.

---

## SB-127 — Configure n8n private access layer

**Branch:** `feature/n8n-private-access`

See [SB-127.md](SB-127.md) for the full spec.

---

## SB-128 — Schedule encrypted nightly backups on EC2

**Branch:** `feature/ec2-backup-schedule`

See [SB-128.md](SB-128.md) for the full spec.

---

## SB-129 — Production smoke test and operations runbook

**Branch:** `release/production`

See [SB-129.md](SB-129.md) for the full spec.

---

## Do not implement in this milestone

- New features — all feature work is complete by this point.
- V3 deferred backlog (S3 attachments, two-way Obsidian sync, vector search) — these are post-production decisions.
