# Vault structure

Your vault lives in your own private GitHub repo. This documents the folder structure to use.

## Folder structure

```
vault/
├── People/
├── Projects/
├── Ideas/
├── Learning/
├── Admin/
└── _log/
```

## Folder definitions

| Folder | Purpose |
|--------|---------|
| `People/` | Contacts, relationships, context about a person |
| `Projects/` | Active work, goals, tasks, next actions |
| `Ideas/` | Sparks, concepts, things to explore |
| `Learning/` | Notes, summaries, things learned or to learn |
| `Admin/` | Logistics, decisions, scheduling, housekeeping |
| `_log/` | Traceability — one markdown file per transaction |

## _log/ entry format

Every message processed by the system writes a log entry:

```markdown
---
input_raw: the original Discord message
timestamp: 2026-06-06T10:23:00Z
discord_msg_id: 123456789
folder_assigned: Learning
tags: [rust, cli, tooling]
confidence_score: 87
prompt_version: 1.0.0
outcome: filed | clarified | rejected
note_link: "[[Learning/rust-vs-python-for-cli-tools]]"
fix_applied: false
fix_text: null
fix_timestamp: null
---
```

## Note on _registry/

A `_registry/` folder will be added to this structure once `project-memory` is set up. It will store pointers to per-project `.brain/` folders so the MCP server can pull project context on demand.