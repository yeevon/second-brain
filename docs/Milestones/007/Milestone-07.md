# Milestone 7: V2 Production Release

The V2 codebase is feature-complete on the `staging` branch. This milestone gets it into continuous operation on EC2.

All six implementation milestones are done. What remains is: merge to main, secure the host, put n8n behind private access, schedule encrypted backups, and validate the deployed system end-to-end.

---

## SB-125 — Merge V2 staging to main and review production configuration

**Branch:** `release/v2-production`

See [SB-125.md](SB-125.md) for the full spec.

---

## SB-126 — Deploy V2 to EC2 with security hardening

**Branch:** `release/v2-production`

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

**Branch:** `release/v2-production`

See [SB-129.md](SB-129.md) for the full spec.

---

## Do not implement in this milestone

- Tech debt items from `tech_debt_1` — deferred to Milestone 8.
- V3 proposal/approval vault updates — deferred to Milestone 9.
- S3 attachment archive, two-way Obsidian sync, vector search — deferred backlog.
