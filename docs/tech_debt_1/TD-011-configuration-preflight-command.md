# TD-011: Add a configuration preflight command

**Status:** Open
**Priority:** Medium — required before EC2 deployment becomes routine

## Problem

New required configuration variables are added each milestone. Existing `.env` files do not automatically inherit additions from `.env.example`. This causes a previously working local environment to fail silently or with confusing errors after pulling a new milestone.

Current new required values that could be missing from older `.env` files:

```
CAPTURE_SERVICE_INTERNAL_TOKEN
CAPTURE_API_HOST
CAPTURE_API_PORT
DOWNSTREAM_DELIVERY_ENABLED
N8N_INTAKE_WEBHOOK_URL
N8N_INTAKE_WEBHOOK_TOKEN
```

## Acceptance criteria

- Add a subcommand: `uv run secondbrain config-check`
- The command reports:
  - Missing required variables (by name, no values)
  - Variables renamed since the previous version (old name → new name)
  - Which env template (`capture-service.env.example`, `n8n.env.example`, etc.) covers each missing variable
  - Whether the process is running in `local-full` vs `capture-only` mode and which variables each mode requires
  - Token minimum-length violations
  - Numeric range violations (e.g., negative timeouts)
- The command never prints secret values.
- The command exits 0 on success, non-zero on any failure.
- Keep startup fail-closed behavior — the preflight command is an operator convenience.

## Do not

- Do not replace runtime validation with the preflight command.
- Do not print any variable values — report only names and whether they are present/valid.
