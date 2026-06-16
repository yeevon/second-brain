#!/usr/bin/env bash
# SB-119: Nightly encrypted off-host backup.
# Run as: ./backup.sh
#
# Required environment variables:
#   LEDGER_PATH         - absolute path to the SQLite ledger file
#   VAULT_PATH          - absolute path to the vault git clone
#   BACKUP_DEST         - destination directory for encrypted archives
#   GPG_RECIPIENT       - GPG key ID or email for encryption
#   N8N_DATA_DIR        - path to n8n data volume (optional)
#   CONFIG_DIR          - path to service config directory (optional)
#   SECOND_BRAIN_DB_URL - sqlite3:// URL for recording timestamps (optional)
#
# After each successful backup the script writes last_successful_backup_at
# into the system_state table of the SQLite ledger using sqlite3's .backup API.

set -euo pipefail

LEDGER_PATH="${LEDGER_PATH:?LEDGER_PATH is required}"
VAULT_PATH="${VAULT_PATH:?VAULT_PATH is required}"
BACKUP_DEST="${BACKUP_DEST:?BACKUP_DEST is required}"
GPG_RECIPIENT="${GPG_RECIPIENT:?GPG_RECIPIENT is required}"
N8N_DATA_DIR="${N8N_DATA_DIR:-}"
CONFIG_DIR="${CONFIG_DIR:-}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

mkdir -p "$BACKUP_DEST"

# ── 1. SQLite ledger backup (safe .backup API via sqlite3 CLI) ────────────────
echo "[backup] backing up SQLite ledger…"
LEDGER_BACKUP="$WORK_DIR/ledger-${TIMESTAMP}.sqlite3"
sqlite3 "$LEDGER_PATH" ".backup '$LEDGER_BACKUP'"

# ── 2. Vault git clone (git bundle for a complete portable replica) ───────────
echo "[backup] bundling vault git clone…"
VAULT_BUNDLE="$WORK_DIR/vault-${TIMESTAMP}.bundle"
git -C "$VAULT_PATH" bundle create "$VAULT_BUNDLE" --all

# ── 3. n8n data volume (optional) ────────────────────────────────────────────
N8N_ARCHIVE=""
if [[ -n "$N8N_DATA_DIR" && -d "$N8N_DATA_DIR" ]]; then
    echo "[backup] archiving n8n data volume…"
    N8N_ARCHIVE="$WORK_DIR/n8n-data-${TIMESTAMP}.tar.gz"
    tar -czf "$N8N_ARCHIVE" -C "$(dirname "$N8N_DATA_DIR")" "$(basename "$N8N_DATA_DIR")"
fi

# ── 4. Service config (redact plaintext secrets) ─────────────────────────────
CONFIG_ARCHIVE=""
if [[ -n "$CONFIG_DIR" && -d "$CONFIG_DIR" ]]; then
    echo "[backup] archiving service config (secrets redacted)…"
    CONFIG_ARCHIVE="$WORK_DIR/config-${TIMESTAMP}.tar.gz"
    REDACTED_DIR="$WORK_DIR/config-redacted"
    mkdir -p "$REDACTED_DIR"
    # Copy .env.example files only — never copy live .env files with plaintext secrets
    find "$CONFIG_DIR" -name "*.env.example" -exec cp --parents {} "$REDACTED_DIR/" \; 2>/dev/null || true
    find "$CONFIG_DIR" -name "*.yml" -o -name "*.yaml" | while read -r f; do
        # Strip lines containing password/token/secret/key assignments
        sed -E 's/^([[:space:]]*(password|token|secret|api_key|APIKEY|api-key)[[:space:]]*[:=][[:space:]]*)(.+)/\1<REDACTED>/' \
            "$f" > "$REDACTED_DIR/$(basename "$f")" || true
    done
    tar -czf "$CONFIG_ARCHIVE" -C "$REDACTED_DIR" .
fi

# ── 5. Encrypt all artefacts with GPG before writing to BACKUP_DEST ──────────
echo "[backup] encrypting artefacts…"
for FILE in "$LEDGER_BACKUP" "$VAULT_BUNDLE" $N8N_ARCHIVE $CONFIG_ARCHIVE; do
    [[ -f "$FILE" ]] || continue
    gpg --batch --yes --recipient "$GPG_RECIPIENT" \
        --output "${BACKUP_DEST}/$(basename "$FILE").gpg" \
        --encrypt "$FILE"
    echo "[backup] encrypted: $(basename "$FILE").gpg"
done

# ── 6. Record timestamp in ledger system_state ────────────────────────────────
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
sqlite3 "$LEDGER_PATH" \
    "INSERT INTO system_state (key, value, updated_at) VALUES ('last_successful_backup_at', '${NOW_ISO}', '${NOW_ISO}') ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;"

echo "[backup] complete at ${NOW_ISO}"
