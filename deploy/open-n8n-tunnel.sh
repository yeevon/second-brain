#!/usr/bin/env bash
set -euo pipefail

HOST="${1:?Usage: deploy/open-n8n-tunnel.sh <EC2_HOST> [SSH_USER]}"
SSH_USER="${2:-ubuntu}"

exec ssh \
  -N \
  -L 5678:127.0.0.1:5678 \
  "$SSH_USER@$HOST"
