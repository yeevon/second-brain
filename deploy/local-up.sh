#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export CAPTURE_SERVICE_ENV_FILE="${CAPTURE_SERVICE_ENV_FILE:-$ROOT_DIR/.env}"
export CAPTURE_DATA_SOURCE="${CAPTURE_DATA_SOURCE:-second-brain-local-data}"

cd "$ROOT_DIR"

# Build the image first so it can be used to initialize the volume below
docker compose -f compose.yaml -f compose.local.yaml build capture-service

# Create the named volume if it does not already exist
docker volume create second-brain-local-data >/dev/null

# Initialize the sentinel marker and set ownership using the just-built image
docker run \
  --rm \
  --user 0:0 \
  --entrypoint /bin/sh \
  -v second-brain-local-data:/var/lib/second-brain \
  second-brain-capture-service:local \
  -lc '
    touch /var/lib/second-brain/.second-brain-ebs-volume
    chown -R 10001:10001 /var/lib/second-brain
    chmod 755 /var/lib/second-brain
  '

docker compose -f compose.yaml -f compose.local.yaml up -d capture-service

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
