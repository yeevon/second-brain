# SB-110A — Fix capture-service container packaging

The Milestone 2 Docker exit regression exposed defects in the container packaging.

Do not add more manual setup steps to the operator runbook. Fix the Docker image and Compose workflow so local execution is reproducible through a supported script.

---

## Goal

Support two explicit deployment paths:

```text
local Docker validation
    → deploy/local-up.sh

EC2 deployment
    → deploy/deploy.sh
```

Both paths must use the same capture-service image and the same `compose.yaml`.

The operator must not need to manually:

```text
copy environment templates
create sentinel markers
initialize Docker volumes
run chown against container UIDs
override runtime paths
remember Compose variable exports
```

---

## 1. Fix the Docker Python version

The project requires:

```toml
requires-python = ">=3.13"
```

The Dockerfile currently builds from Python 3.12.

Update the Dockerfile base image:

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim
```

Add:

```dockerfile
ENV UV_NO_MANAGED_PYTHON=1
```

This prevents `uv` from silently downloading a root-owned managed Python interpreter that the non-root runtime user cannot execute.

The image must continue running as:

```dockerfile
USER secondbrain
```

Do not solve this by running the application as root.

---

## 2. Enforce container-specific runtime invariants in Compose

The container must always run as the durable intake service.

Add explicit service-level overrides in `compose.yaml`:

```yaml
environment:
  CAPTURE_PROCESSING_MODE: capture-only
  CAPTURE_API_HOST: 0.0.0.0
  CAPTURE_API_PORT: "8000"
  LEDGER_PATH: /var/lib/second-brain/ledger.sqlite3
```

These values must override desktop-local values loaded from `.env`.

Do not require the operator to edit the existing local `.env` file.

The existing desktop `.env` must remain usable for:

```text
uv run secondbrain run
```

in `local-full` development mode.

---

## 3. Make Compose require explicit external paths

Replace silent EC2-oriented defaults with explicit variables:

```yaml
env_file:
  - ${CAPTURE_SERVICE_ENV_FILE:?CAPTURE_SERVICE_ENV_FILE must be set}

volumes:
  - ${CAPTURE_DATA_SOURCE:?CAPTURE_DATA_SOURCE must be set}:/var/lib/second-brain
```

Use:

```text
CAPTURE_DATA_SOURCE
```

rather than:

```text
CAPTURE_DATA_DIR
```

because the source may be either:

```text
Docker-managed named volume
host bind-mounted directory
```

Declare the local Docker-managed volume:

```yaml
volumes:
  second-brain-local-data:
    name: second-brain-local-data
```

---

## 4. Add a supported local startup script

Create:

```text
deploy/local-up.sh
```

The script must:

```text
use the repo-root .env file by default
create the Docker-managed local named volume if missing
initialize the local sentinel marker inside that volume
set volume ownership to UID:GID 10001:10001
export the required Compose variables
build the image
start capture-service
wait for container health
print a concise success message
```

Example implementation shape:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.." &&
  pwd
)"

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-$ROOT_DIR/.env}"
export CAPTURE_DATA_SOURCE="${CAPTURE_DATA_SOURCE:-second-brain-local-data}"

docker volume create second-brain-local-data >/dev/null

docker run \
  --rm \
  --user 0:0 \
  --entrypoint /bin/sh \
  -v second-brain-local-data:/var/lib/second-brain \
  second-brain-capture-service:local \
  -lc '
    touch /var/lib/second-brain/.second-brain-ebs-volume
    chown -R 10001:10001 /var/lib/second-brain
    chmod 775 /var/lib/second-brain
  '

docker compose build capture-service
docker compose up -d capture-service

health=""

for _ in $(seq 1 45); do
  health="$(
    docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
      second-brain-capture-service \
      2>/dev/null || true
  )"

  if [ "$health" = "healthy" ]; then
    break
  fi

  sleep 2
done

test "$health" = "healthy"

echo "capture-service local container is healthy"
```

Adjust the ordering so the image exists before using it to initialize the volume. A lightweight base image may be used for local volume initialization if preferred.

The supported local command must be:

```bash
deploy/local-up.sh
```

---

## 5. Add supported local shutdown and reset scripts

Create:

```text
deploy/local-down.sh
deploy/local-reset.sh
```

`local-down.sh` must stop the local container without deleting durable local test data.

`local-reset.sh` must:

```text
stop the container
delete the local Docker named volume
recreate the local Docker named volume
require an explicit confirmation flag
```

Example invocation:

```bash
deploy/local-reset.sh --confirm-delete-local-test-data
```

Do not delete data implicitly.

---

## 6. Keep EC2 fail-closed behavior

Update:

```text
deploy/deploy.sh
deploy/verify.sh
```

to export:

```bash
CAPTURE_SERVICE_ENV_FILE=/opt/second-brain/config/capture-service.env
CAPTURE_DATA_SOURCE=/opt/second-brain/data
```

The EC2 path must remain a host bind mount.

The EC2 deployment script must continue refusing startup when:

```text
/opt/second-brain/data/.second-brain-ebs-volume
```

is missing.

Do not auto-create the EC2 sentinel marker inside `deploy/deploy.sh`.

Local Docker initialization and EC2 production validation are intentionally different workflows.

---

## 7. Keep secrets out of the image

Restore and preserve:

```text
.env
.env.*
```

inside `.dockerignore`.

The Dockerfile must never copy `.env`.

Compose may load the host `.env` file at runtime through `env_file`, but it must not include it in the image build context.

---

## 8. Add Docker packaging regressions

Add tests or shell checks for:

### Image runtime user can execute Python

```bash
docker run \
  --rm \
  --user 10001:10001 \
  --entrypoint /app/.venv/bin/python \
  second-brain-capture-service:local \
  --version
```

Expected:

```text
Python 3.13.x
```

### Container runtime invariants override desktop `.env`

Given a test env containing:

```dotenv
CAPTURE_PROCESSING_MODE=local-full
LEDGER_PATH=.runtime/ledger.sqlite3
```

the resolved Compose container configuration must still contain:

```text
CAPTURE_PROCESSING_MODE=capture-only
LEDGER_PATH=/var/lib/second-brain/ledger.sqlite3
```

### Local startup script

```bash
deploy/local-up.sh
```

must produce:

```text
healthy capture-service container
writable named volume
sentinel marker present
ledger.sqlite3 created inside the named volume
internal port 8000 not published to the host
```

### EC2 missing-volume fail-closed check

A bind-mounted directory without:

```text
.second-brain-ebs-volume
```

must cause container startup to fail before SQLite is created.

---

## 9. Update the SB-110 runbook

Replace the manual local Docker preparation section with:

```bash
deploy/local-up.sh
```

Use:

```bash
deploy/local-down.sh
```

for shutdown.

Use:

```bash
deploy/local-reset.sh --confirm-delete-local-test-data
```

only when a clean local volume is required.

Document that:

```text
local Docker uses a Docker-managed named volume
EC2 uses /opt/second-brain/data bind-mounted from persistent EBS
```

Remove manual local instructions for:

```text
mkdir
touch sentinel
chown
CAPTURE_DATA_DIR
CAPTURE_SERVICE_ENV_FILE exports
Compose override files
```

---

## Done when

From a clean checkout with an existing repo-root `.env`, this command works:

```bash
deploy/local-up.sh
```

Without manual intervention, it must:

```text
build the image
start the non-root container
initialize writable local persistent storage
create ledger.sqlite3
reach healthy state
connect to Discord
capture a test message durably
```

The operator must not need to patch Compose commands manually during the SB-110 exit regression.
