#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${CONTAINER:-second-brain-capture-service}"
DATA_DIR="${DATA_DIR:-/opt/second-brain/data}"

docker inspect "$CONTAINER" >/dev/null

running="$(docker inspect --format '{{.State.Running}}' "$CONTAINER")"
if [[ "$running" != "true" ]]; then
  echo "container is not running" >&2
  exit 1
fi

restart_policy="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER")"
if [[ "$restart_policy" != "unless-stopped" ]]; then
  echo "unexpected restart policy: $restart_policy" >&2
  exit 1
fi

user="$(docker inspect --format '{{.Config.User}}' "$CONTAINER")"
if [[ "$user" == "" || "$user" == "0" || "$user" == "root" ]]; then
  echo "container is not configured with a non-root user" >&2
  exit 1
fi

ports="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "$CONTAINER")"
if [[ "$ports" != *'"8000/tcp":null'* ]]; then
  echo "internal API port appears to be published: $ports" >&2
  exit 1
fi

if ! mountpoint -q "$DATA_DIR"; then
  echo "persistent data volume is not mounted at: $DATA_DIR" >&2
  exit 1
fi

MARKER="$DATA_DIR/.second-brain-ebs-volume"

if [[ ! -f "$MARKER" ]]; then
  echo "persistent EBS marker missing: $MARKER" >&2
  exit 1
fi

mount_source="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/var/lib/second-brain"}}{{.Source}}{{end}}{{end}}' \
    "$CONTAINER"
)"
if [[ "$mount_source" != "$DATA_DIR" ]]; then
  echo "unexpected ledger bind mount source: $mount_source" >&2
  exit 1
fi

if [[ ! -f "$DATA_DIR/ledger.sqlite3" ]]; then
  echo "ledger file missing from persistent data volume" >&2
  exit 1
fi

health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$CONTAINER")"
if [[ "$health" != "healthy" ]]; then
  echo "container health is not healthy: $health" >&2
  exit 1
fi

echo "capture-service deployment checks passed"
