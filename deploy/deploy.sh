#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/second-brain/app}"

cd "$APP_DIR"

docker compose config >/dev/null
docker compose build
docker compose up -d
docker compose ps
