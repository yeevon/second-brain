# Milestone 6: Add review and local-query features

These are useful, but they should not distract from durable intake, retries, and backups.

---

## SB-119 — Add daily digest workflow

**Branch:** ```feature/daily-digest```

### Include

- New captures.
- Filed notes.
- Inbox backlog.
- Awaiting clarification.
- Open tasks.
- Failed and retried captures.
- Sensitive-rejection counts only.
- Attachment warnings.

Send to ```#brain-digest``` or DM.

---

## SB-120 — Add weekly review workflow

**Branch:** ```feature/weekly-review```

### Include

- Explicitly completed actions.
- Explicitly created tasks.
- Outstanding tasks.
- Decisions.
- Inbox backlog.
- Corrections.
- Failures and retries.
- A clearly labeled AI-generated priorities section.

Do not infer completion from vague prose. Base progress claims on explicit ```task:```, ```done:```, ```decision:```, ```note:```, and ```fix:``` state changes.

---

## SB-121 — Add local pull-only Obsidian sync wrapper

**Branch:** ```feature/local-vault-pull```

### Implement

```init
git fetch origin
git merge --ff-only origin/main
verify clean worktree
fail visibly on dirty tree or conflict
```

Keep Obsidian pull-only for version one. The architecture intentionally avoids two writers until you design a conflict policy.

---

## SB-122 — Add the local read-only MCP server

**Branch:** ```feature/read-only-mcp```

Implement only after the rest of the pipeline is stable:

```init
search_notes(query, folder?, project?, tags?, limit?)
read_note(note_path)
list_recent_notes(days?, folder?, limit?)
list_open_tasks(project?, limit?)
get_sync_status()
```

Add path-root enforcement, result limits, no shell execution, no mutation tools, and a sync preflight before queries.

---

## Do not implement these yet

Leave these in the backlog until the simpler design proves insufficient:

- S3-compatible attachment archive.
- Two-way Obsidian sync.
- Writable MCP tools.
- Vector search and embeddings.
- Redis or n8n queue mode.
- Separate Bouncer model call.
- Automatic prompt tuning.
- Multi-user capture.

The canonical architecture deliberately defers each of these until there is evidence that the simpler implementation is inadequate.


## What I would implement next

Start with this exact sequence:

```init
SB-101  Regression suite
SB-102  Internal capture-service boundary
SB-103  capture-service HTTP API
SB-104  EC2 deployment
SB-105  SQLite service hardening
SB-106  Periodic reconciliation
SB-107  Delivery leases and retry state
SB-108  Stale-lease reaper
SB-109  Expanded status command
```

Do not start n8n until those are working. The next architectural risk is not classification quality. It is making sure the always-on intake service can survive downtime, dropped events, restarts, and stuck work without silently losing a thought.
