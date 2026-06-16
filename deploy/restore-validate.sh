#!/usr/bin/env bash
# SB-119: Weekly restore validation — decrypts and validates a backup into a
# temporary location without touching live volumes.
#
# Required environment variables:
#   LEDGER_PATH     - absolute path to the live SQLite ledger (for writing timestamp)
#   BACKUP_DEST     - directory containing the encrypted backups
#   GPG_KEY_FILE    - path to the GPG private key file for decryption
#                     (or set GPG_PASSPHRASE for symmetric decryption)
#
# Optional:
#   BACKUP_DATE     - YYYYMMDD prefix to pick a specific backup; defaults to latest

set -euo pipefail

LEDGER_PATH="${LEDGER_PATH:?LEDGER_PATH is required}"
BACKUP_DEST="${BACKUP_DEST:?BACKUP_DEST is required}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

RESTORE_DIR="$WORK_DIR/restore"
mkdir -p "$RESTORE_DIR"

# ── Find the latest (or specified) encrypted ledger backup ───────────────────
if [[ -n "${BACKUP_DATE:-}" ]]; then
    LEDGER_ENCRYPTED="$(ls -1t "${BACKUP_DEST}/ledger-${BACKUP_DATE}"*.gpg 2>/dev/null | head -1)"
else
    LEDGER_ENCRYPTED="$(ls -1t "${BACKUP_DEST}/ledger-"*.gpg 2>/dev/null | head -1)"
fi

if [[ -z "$LEDGER_ENCRYPTED" ]]; then
    echo "[restore-validate] ERROR: no encrypted ledger backup found in $BACKUP_DEST" >&2
    exit 1
fi

echo "[restore-validate] validating: $(basename "$LEDGER_ENCRYPTED")"

# ── Decrypt into temp dir ─────────────────────────────────────────────────────
DECRYPTED_LEDGER="$RESTORE_DIR/ledger-restored.sqlite3"
if [[ -n "${GPG_KEY_FILE:-}" ]]; then
    gpg --batch --yes \
        --import "$GPG_KEY_FILE" 2>/dev/null || true
fi
gpg --batch --yes \
    --output "$DECRYPTED_LEDGER" \
    --decrypt "$LEDGER_ENCRYPTED"

# ── Integrity check: sqlite3 integrity_check ─────────────────────────────────
echo "[restore-validate] running sqlite3 integrity_check…"
RESULT="$(sqlite3 "$DECRYPTED_LEDGER" "PRAGMA integrity_check;")"
if [[ "$RESULT" != "ok" ]]; then
    echo "[restore-validate] FAIL: integrity_check returned: $RESULT" >&2
    exit 1
fi
echo "[restore-validate] integrity_check: ok"

# ── Schema smoke test: verify expected tables exist ──────────────────────────
echo "[restore-validate] verifying schema…"
TABLES="$(sqlite3 "$DECRYPTED_LEDGER" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;" | tr '\n' ',')"
for REQUIRED in captures capture_events system_state corrections; do
    if [[ "$TABLES" != *"$REQUIRED"* ]]; then
        echo "[restore-validate] FAIL: required table '$REQUIRED' not found" >&2
        exit 1
    fi
done
echo "[restore-validate] schema: ok (tables: $TABLES)"

# ── Row count smoke test ──────────────────────────────────────────────────────
CAPTURE_COUNT="$(sqlite3 "$DECRYPTED_LEDGER" "SELECT COUNT(*) FROM captures;")"
echo "[restore-validate] captures in restored ledger: $CAPTURE_COUNT"

# ── Record success timestamp in live ledger ───────────────────────────────────
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
sqlite3 "$LEDGER_PATH" \
    "INSERT INTO system_state (key, value, updated_at) VALUES ('last_successful_restore_validation_at', '${NOW_ISO}', '${NOW_ISO}') ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;"

echo "[restore-validate] complete at ${NOW_ISO}"
