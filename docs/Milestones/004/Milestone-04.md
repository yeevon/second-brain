# Milestone 4: Extract the Git-backed writer

At this point, classification runs in n8n and the stub writer processes every capture. The vault remains unwritten. Milestone 4 extracts the Markdown generation logic into a real `writer-service` that writes notes to a local filesystem first, then adds the Git-backed vault sync.

---

## SB-114 — Create writer-service with a local-only vault

**Branch:** `feature/writer-service-api`

Extract deterministic Markdown generation into a dedicated HTTP service with:

```text
GET  /health
POST /internal/notes/file
POST /internal/notes/update
GET  /internal/notes/by-capture/:capture_id
```

Keep the initial implementation local to its filesystem. Do not add Git push in the first commit.

Preserve all existing Markdown generation behavior: schema validation, folder allowlist, title and project-slug sanitization, path traversal protection, deterministic filenames and frontmatter, idempotency by `capture_id`, and the `99_log/events.ndjson` audit trail.

**Done when:** the n8n workflow files a note through the writer-service API with the same Markdown output the MVP already generated, and the writer-stub container can be removed from the stack.

---

## SB-115 — Add GitHub vault sync and serialized Git writes

**Branch:** `feature/writer-git-sync`

Create the private vault repository and EC2-side clone at `/opt/second-brain/vault`. Add a kernel-managed advisory `flock` around every write so concurrent processes never corrupt the repository:

```text
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

Do not use a lock-file existence check as the locking rule. Hold a real kernel-managed advisory `flock` on an open file descriptor.

**Done when:** two near-simultaneous notes each produce exactly one committed file, and killing the writer while it holds the lock does not leave an immortal application lock.

---

## SB-116 — Add Git conflict and stale-lock failure handling

**Branch:** `feature/writer-safe-failure`

Add explicit failure behavior for every bad state the Git-backed write sequence can reach:

```text
non-fast-forward merge failure
rejected push
existing .git/index.lock
duplicate capture_id
more than one vault note containing the same capture_id
path escape attempt
```

Fail visibly. Do not overwrite, auto-resolve, or blindly delete Git-internal lock files.

**Done when:** every injected Git failure preserves the raw SQLite capture and creates a readable failure state that the operator can inspect and resolve manually.
