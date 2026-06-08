# Milestone 5: Add user-facing correction and recovery features

This is where the system becomes more than an automated filing toy.

---

## SB-116 — Implement clarification handling

**Branch:**```feature/clarification-flow```

For low-confidence classification:

- File the derived note into 00_inbox/.
- Ask a clarification question through the original receipt or a thread.
- Keep the raw accepted capture immutable.
- Preserve unanswered items in Inbox.
- Add clarification timeout behavior.
- Show unresolved clarifications in status output.

**Done when:** an ambiguous note remains safely available without the classifier force-fitting it into a folder.

---

## SB-117 — Implement targeted fix: corrections

**Branch:** ```feature/fix-corrections```

Support:

```init
reply to a receipt:
fix: this belongs under Learning, not Projects.

or:
fix SB-20260607-0042: this belongs under Learning.
```

Reject an unthreaded standalone message such as:

```init
fix: move this to Learning
```

Do not guess the most-recent capture. The architecture explicitly requires unambiguous correction targeting.

Persist immutable correction history, update or ```git mv``` the derived note, retain the same ```capture_id```, and store the old path, new path, and Git commit hash.

**Done when:** a second correction after a previous move still targets the current note path and never creates a duplicate.

---

## SB-118 — Add encrypted off-host backups and restore validation

**Branch:** ```feature/encrypted-backups```

### Back up

- SQLite ledger using SQLite-safe tooling.
- EC2 vault clone.
- n8n data volume.
- Service configuration without plaintext secrets where practical.

### Schedule

- Nightly encrypted snapshot.
- Weekly restore test or validation.

The architecture treats GitHub as a remote replica, not the only backup.

**Done when:** a restore script can validate a backup into a temporary location without damaging the live deployment.

---
