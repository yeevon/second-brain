#!/usr/bin/env sh
set -eu

DATA_DIR="${SECOND_BRAIN_DATA_DIR:-/var/lib/second-brain}"
MARKER="$DATA_DIR/.second-brain-ebs-volume"

if [ ! -f "$MARKER" ]; then
  echo "persistent EBS volume marker missing: $MARKER" >&2
  exit 1
fi

exec "$@"
