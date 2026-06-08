# Milestone 4: Extract the Git-backed writer

This is the next substantial architecture change. Your existing deterministic writer becomes the reference behavior.

---

## SB-113 — Create writer-service with a local-only vault

**Branch:** ```feature/writer-service-api```

Extract deterministic Markdown generation into a dedicated process with:

```init
GET  /health
POST /internal/notes/file
POST /internal/notes/update
GET  /internal/notes/by-capture/:capture_id
```

Keep the initial implementation local to its filesystem. Do not add Git push in the first commit.

### Preserve

- Schema validation.
- Folder allowlist.
- Title and project-slug sanitization.
- Path traversal protection.
- Deterministic filenames and frontmatter.
- Idempotency by capture_id.
- 99_log/events.ndjson.

Only ```writer-service``` should eventually mutate the EC2-side clone.

**Done when:** the n8n workflow files a note through the service API with the same Markdown output your MVP already generated.

---

## SB-114 — Add GitHub vault sync and serialized Git writes

**Branch:** ```feature/writer-git-sync```

Create the private vault repository and EC2-side clone at:

```init
/opt/second-brain/vault
```

Implement this locked sequence:

```init
acquire OS advisory flock
git fetch origin
git merge --ff-only origin/main
check whether capture_id already exists
render or update note
append audit event
git add
git commit
git push
release flock
return note path and commit hash
```

Do not use “lock file exists” as the locking rule. Hold a real kernel-managed advisory ```flock``` on an open file descriptor.

**Done when:** two near-simultaneous notes each produce exactly one committed file, and killing the writer while it holds the lock does not leave an immortal application lock.

---

## SB-115 — Add Git conflict and stale-lock failure handling

**Branch:** ```feature/writer-safe-failure```

Add explicit behavior for:

- Non-fast-forward merge failure.
- Rejected push.
- Existing .git/index.lock.
- Duplicate capture_id.
- More than one vault note containing the same capture_id.
- Path escape attempt.

Fail visibly. Do not overwrite, auto-resolve, or blindly delete Git-internal lock files.

**Done when:** every injected Git failure preserves the raw SQLite capture and creates a readable failure state.

---
