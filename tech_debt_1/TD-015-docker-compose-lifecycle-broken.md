# TD-015: Plain Docker lifecycle commands are broken without shell exports

**Status:** Open (subset of TD-014 — tracked separately for implementation clarity)
**Priority:** High — blocking routine development workflow

## Problem

`compose.yaml` uses `:?` error guards for all path variables:

```yaml
env_file:
  - ${CAPTURE_SERVICE_ENV_FILE:?CAPTURE_SERVICE_ENV_FILE must be set}

volumes:
  - ${CAPTURE_DATA_SOURCE:?CAPTURE_DATA_SOURCE must be set}:/var/lib/second-brain
```

Docker Compose evaluates variable substitution before merging override files. Any invocation without these variables set (including `docker compose down`) fails immediately with:

```
variable "CAPTURE_SERVICE_ENV_FILE" is not set. Defaulting to a blank string.
```

The n8n overlay (`compose.n8n.yaml`) also uses `:?` guards for `N8N_IMAGE_TAG`, `N8N_ENV_FILE`, `N8N_DATA_SOURCE`, `N8N_ENCRYPTION_KEY_FILE`, and `WRITER_STUB_ENV_FILE`. Since this overlay is not auto-loaded, n8n and writer-stub are also missing from plain `docker compose up`.

Currently working around this issue requires:
- Running `deploy/local-stack-up.sh` (exports all variables + sets `COMPOSE_FILE`)
- Running `deploy/local-stack-down.sh` (exports all variables + stops services)

## Root cause

- `compose.yaml`: `:?` guards instead of `:-` defaults
- No `compose.override.yaml` for auto-loaded local defaults
- n8n and writer-stub live in `compose.n8n.yaml` which is not auto-loaded

## Fix

1. Replace `:?` with `:-` in `compose.yaml`:
   - `CAPTURE_SERVICE_ENV_FILE` → default `.env`
   - `CAPTURE_DATA_SOURCE` → default `second-brain-local-data`
2. Create `compose.override.yaml` (auto-loaded) with:
   - Named volume declarations
   - Sentinel creation in entrypoint override
   - n8n service definition with `:-` defaults
   - writer-stub service definition with `:-` defaults
3. Production `deploy.sh` keeps explicit `COMPOSE_FILE=compose.yaml` — override is never loaded in production.

## Acceptance criteria

- `docker compose up -d` starts all three services without any shell exports.
- `docker compose down` stops all services without shell exports.
- `docker compose logs -f` and `docker compose ps` work without shell exports.
- `deploy.sh` and all production scripts are unaffected.
