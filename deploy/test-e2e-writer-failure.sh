#!/usr/bin/env bash
# E2E regression for writer-service failure durability (SB-116 done condition).
#
# Requires:
#   - docker compose up -d
#   - deploy/bootstrap-n8n.sh has imported workflows
#   - Intake workflow active
#   - required n8n credentials bound
#
# Tests the real n8n → writer-service → capture-service chain.
#
# Verified scenarios:
#  1. Normal write: synthetic capture → FORWARDING → n8n → writer-service → COMPLETE.
#  2. Index lock: capture reaches RETRY_WAIT; raw_text survives in SQLite.
#  3. Lock removal: capture auto-retries and eventually reaches COMPLETE.
#  4. Duplicate capture ID: second attempt reaches terminal FAILED in SQLite.
#
# Note on push-rejection: testing a real remote race requires a writable GitHub
# remote and is not safe to automate without --skip-push.  That scenario is
# covered at the unit level by test_git_failure_handling.py::test_push_rejected_*.
# The writer-service integration test validates rollback and RETRY_WAIT routing;
# this E2E script focuses on the SQLite durability properties that are only
# observable at the capture-service layer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

CS_CONTAINER="${CS_CONTAINER:-second-brain-capture-service}"
WRITER_CONTAINER="${WRITER_CONTAINER:-second-brain-writer-service}"
N8N_CONTAINER="${N8N_CONTAINER:-second-brain-n8n}"
N8N_URL="${N8N_URL:-http://127.0.0.1:5678}"
LEDGER_PATH="${LEDGER_PATH:-/var/lib/second-brain/ledger.sqlite3}"

PASS=0
FAIL=0
SKIP=0

_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $1" >&2; FAIL=$((FAIL + 1)); }
_skip() { echo "  [SKIP] $1"; SKIP=$((SKIP + 1)); }

echo "=== E2E writer-service failure durability regression (SB-116) ==="
echo ""

# ── Load tokens ───────────────────────────────────────────────────────────────

_env_var() {
  local key="$1" val=""
  val="${!key:-}"
  if [[ -z "$val" ]] && [[ -f "$ENV_FILE" ]]; then
    val="$(grep "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  fi
  echo "$val"
}

INTAKE_TOKEN="$(_env_var N8N_INTAKE_WEBHOOK_TOKEN)"
if [[ -z "$INTAKE_TOKEN" ]]; then
  echo "ERROR: N8N_INTAKE_WEBHOOK_TOKEN not set in env or .env file" >&2
  exit 1
fi

# ── Pre-flight: containers healthy ───────────────────────────────────────────

for ctr in "$CS_CONTAINER" "$WRITER_CONTAINER" "$N8N_CONTAINER"; do
  running="$(docker inspect --format '{{.State.Running}}' "$ctr" 2>/dev/null || echo false)"
  if [[ "$running" != "true" ]]; then
    echo "ERROR: container not running: $ctr" >&2
    exit 1
  fi
done
echo "Pre-flight: all containers running."

# ── Git-sync preflight ────────────────────────────────────────────────────────
# The index-lock and vault-clean tests only make sense when the writer-service
# is running with GIT_SYNC_ENABLED=true against a real Git repo at /opt/vault.
# Fail fast with a clear message rather than producing misleading results.
echo "Checking writer-service Git-sync configuration..."
if ! docker exec "$WRITER_CONTAINER" sh -c '
  test "$(printenv GIT_SYNC_ENABLED)" = "true" || {
    echo "GIT_SYNC_ENABLED is not true inside '"$WRITER_CONTAINER"'" >&2; exit 1
  }
  git -C /opt/vault rev-parse --is-inside-work-tree || {
    echo "writer-service cannot use /opt/vault as a Git repository" >&2
    exit 1
  }
  git -C /opt/vault status --porcelain || {
    echo "git status failed inside writer-service /opt/vault" >&2
    exit 1
  }
  grep -qxF ".writer.lock" /opt/vault/.gitignore 2>/dev/null || {
    echo ".writer.lock not in vault .gitignore" >&2; exit 1
  }
' ; then
  echo ""
  echo "ERROR: writer-service Git-sync preflight failed." >&2
  echo "  Local dev should be initialized by plain: docker compose up -d" >&2
  echo "  Inspect init logs with: docker logs second-brain-local-vault-init" >&2
  echo "  Then verify: docker exec second-brain-writer-service git -C /opt/vault status" >&2
  exit 1
fi
echo "Git-sync preflight passed."
echo ""

# ── Helpers ───────────────────────────────────────────────────────────────────

# Create a synthetic capture in FORWARDING state directly in SQLite,
# matching the same pattern used by test-n8n-error-workflow.sh.
# Prints "capture_id:delivery_attempt" on stdout.
_create_synthetic() {
  local tag="$1"
  local raw_text="${2:-E2E regression — ${tag} — safe to delete}"
  docker exec -i \
    -e "LEDGER_PATH=$LEDGER_PATH" \
    -e "TAG=$tag" \
    -e "RAW_TEXT=$raw_text" \
    "$CS_CONTAINER" python3 - <<'PYEOF'
import sys, os, json, sqlite3
from datetime import UTC, datetime, timedelta

ledger_path = os.environ.get('LEDGER_PATH', '/var/lib/second-brain/ledger.sqlite3')
tag = os.environ.get('TAG', 'e2e')
raw_text = os.environ.get('RAW_TEXT', 'E2E regression note')
now = datetime.now(UTC)
now_iso = now.isoformat()
lease_iso = (now + timedelta(hours=2)).isoformat()
delivery_attempts = 1

with sqlite3.connect(ledger_path, timeout=15) as conn:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")

    prefix = f"SB-{now.strftime('%Y%m%d')}-"
    row = conn.execute(
        "SELECT capture_id FROM captures WHERE capture_id LIKE ? ORDER BY capture_id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    next_number = (int(row["capture_id"].rsplit("-", 1)[1]) + 1) if row else 1
    capture_id = f"{prefix}{next_number:04d}"

    conn.execute(
        """INSERT INTO captures (
               capture_id, discord_message_id, discord_channel_id, discord_guild_id,
               discord_author_id, raw_text, is_sensitive, has_attachments,
               attachment_metadata_json, received_at, status, delivery_status,
               delivery_attempts, processing_lease_until, updated_at
           ) VALUES (?, ?, '0', '0', '0', ?, 0, 0, '[]', ?, 'RECEIVED', 'FORWARDING', ?, ?, ?)""",
        (capture_id, f"e2e-{tag}-{now.strftime('%f')}",
         raw_text, now_iso, delivery_attempts, lease_iso, now_iso),
    )
    conn.execute(
        "INSERT INTO capture_events (capture_id, event_type, event_payload_json, created_at) VALUES (?, ?, ?, ?)",
        (capture_id, 'CAPTURE_RECEIVED', json.dumps({"status": "RECEIVED"}), now_iso),
    )
    conn.execute(
        "INSERT INTO capture_events (capture_id, event_type, event_payload_json, created_at) VALUES (?, ?, ?, ?)",
        (capture_id, 'DELIVERY_ATTEMPT_CLAIMED',
         json.dumps({"delivery_attempt": delivery_attempts, "lease_until": lease_iso}), now_iso),
    )
    conn.commit()

print(f'{capture_id}:{delivery_attempts}')
PYEOF
}

# Query delivery_status from the capture-service internal API inside the CS container.
_delivery_status() {
  local cid="$1"
  docker exec -i \
    -e "LEDGER_PATH=$LEDGER_PATH" \
    -e "CAPTURE_ID=$cid" \
    "$CS_CONTAINER" python3 - <<'PYEOF'
import os, sqlite3
ledger_path = os.environ.get('LEDGER_PATH', '/var/lib/second-brain/ledger.sqlite3')
cid = os.environ['CAPTURE_ID']
with sqlite3.connect(ledger_path, timeout=10) as conn:
    row = conn.execute(
        'SELECT delivery_status FROM captures WHERE capture_id=?', (cid,)
    ).fetchone()
print(row[0] if row else 'NOT_FOUND')
PYEOF
}

# Check raw_text is present (non-empty) for the given capture_id.
_raw_text_present() {
  local cid="$1"
  docker exec -i \
    -e "LEDGER_PATH=$LEDGER_PATH" \
    -e "CAPTURE_ID=$cid" \
    "$CS_CONTAINER" python3 - <<'PYEOF'
import os, sqlite3
ledger_path = os.environ.get('LEDGER_PATH', '/var/lib/second-brain/ledger.sqlite3')
cid = os.environ['CAPTURE_ID']
with sqlite3.connect(ledger_path, timeout=10) as conn:
    row = conn.execute(
        'SELECT raw_text FROM captures WHERE capture_id=?', (cid,)
    ).fetchone()
print('yes' if row and row[0] else 'no')
PYEOF
}

# Check the Intake webhook route is registered before creating captures.
# Sends a known-invalid payload; 404 = workflow missing/inactive, any other
# response proves the route exists.
_preflight_intake_webhook_registered() {
  local status body_file
  body_file="$(mktemp)"

  status="$(
    curl -sS \
      --output "$body_file" \
      --write-out "%{http_code}" \
      --request POST \
      --header "Content-Type: application/json" \
      --header "X-Second-Brain-Intake-Token: $INTAKE_TOKEN" \
      --data '{"capture_id":"SB-00000000-0000","delivery_attempt":1}' \
      "$N8N_URL/webhook/second-brain-intake" || true
  )"

  if [[ "$status" == "404" ]]; then
    echo "ERROR: n8n intake webhook is not registered." >&2
    echo "  The Intake workflow is missing or inactive." >&2
    echo "  If you ran docker compose down -v, n8n workflows/credentials were wiped." >&2
    echo "  Run deploy/bootstrap-n8n.sh, bind credentials, set Error Workflow, and activate Intake." >&2
    echo "" >&2
    cat "$body_file" >&2 || true
    rm -f "$body_file"
    exit 1
  fi

  rm -f "$body_file"
}

# Trigger delivery through n8n intake webhook (same as the real dispatcher does).
# Returns non-zero and prints the response body on non-2xx.
_trigger_intake() {
  local cid="$1" attempt="$2"
  local status body_file
  body_file="$(mktemp)"

  status="$(
    curl -sS \
      --output "$body_file" \
      --write-out "%{http_code}" \
      --request POST \
      --header "Content-Type: application/json" \
      --header "X-Second-Brain-Intake-Token: $INTAKE_TOKEN" \
      --data "{\"capture_id\":\"$cid\",\"delivery_attempt\":$attempt}" \
      "$N8N_URL/webhook/second-brain-intake"
  )"

  if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
    echo "n8n intake webhook failed with HTTP $status" >&2
    cat "$body_file" >&2 || true
    rm -f "$body_file"
    return 1
  fi

  rm -f "$body_file"
}

# Poll delivery_status until it matches expected or deadline passes.
_poll_status() {
  local cid="$1" expected="$2" timeout_s="${3:-30}"
  local deadline=$(( $(date +%s) + timeout_s ))
  local actual=""
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    actual="$(_delivery_status "$cid" 2>/dev/null || echo ERROR)"
    [[ "$actual" == "$expected" ]] && echo "$actual" && return 0
    sleep 2
  done
  echo "$actual"
  return 1
}

_vault_clean() {
  local result
  result="$(docker exec "$WRITER_CONTAINER" sh -c 'git -C /opt/vault status --porcelain 2>/dev/null' || true)"
  [[ -z "$result" ]]
}

# ── n8n intake webhook preflight ─────────────────────────────────────────────
echo "Checking n8n Intake webhook is registered..."
_preflight_intake_webhook_registered
echo "n8n Intake webhook preflight passed."
echo ""

# ── Test 1: normal write → COMPLETE ──────────────────────────────────────────

echo "--- Test 1: normal write ---"

result1="$(_create_synthetic "t1-normal" 2>&1)"
if [[ "$result1" == ERROR* ]] || [[ -z "$result1" ]]; then
  _fail "create synthetic capture: $result1"
else
  CID1="${result1%%:*}"
  ATT1="${result1##*:}"
  echo "  capture_id=$CID1  delivery_attempt=$ATT1"

  if ! _trigger_intake "$CID1" "$ATT1"; then
    _fail "trigger intake webhook failed"
  else
    echo "  intake triggered — polling for COMPLETE (up to 40s)..."
    status1="$(_poll_status "$CID1" "COMPLETE" 40 || true)"
    if [[ "$status1" == "COMPLETE" ]]; then
      _pass "normal write reaches delivery_status=COMPLETE"
    else
      _fail "normal write delivery_status=$status1 after 40s (expected COMPLETE)"
    fi
  fi
fi

echo ""

# ── Test 2: index lock → RETRY_WAIT, raw_text survives ───────────────────────

echo "--- Test 2: index lock → RETRY_WAIT, raw_text preserved ---"

docker exec "$WRITER_CONTAINER" sh -c 'touch /opt/vault/.git/index.lock' 2>/dev/null || true

result2="$(_create_synthetic "t2-indexlock" "Index lock durability — SB-116 regression" 2>&1)"
if [[ "$result2" == ERROR* ]] || [[ -z "$result2" ]]; then
  _fail "create synthetic capture for index lock test: $result2"
else
  CID2="${result2%%:*}"
  ATT2="${result2##*:}"
  echo "  capture_id=$CID2  delivery_attempt=$ATT2"

  if ! _trigger_intake "$CID2" "$ATT2"; then
    _fail "trigger intake webhook failed"
  else
    echo "  intake triggered — polling for RETRY_WAIT (up to 25s)..."
    status2="$(_poll_status "$CID2" "RETRY_WAIT" 25 || true)"
    if [[ "$status2" == "RETRY_WAIT" ]]; then
      _pass "index lock → delivery_status=RETRY_WAIT"
    else
      _fail "index lock → delivery_status=$status2 after 25s (expected RETRY_WAIT)"
    fi
  fi

  raw_ok="$(_raw_text_present "$CID2" 2>/dev/null || echo no)"
  if [[ "$raw_ok" == "yes" ]]; then
    _pass "raw_text present in SQLite during RETRY_WAIT"
  else
    _fail "raw_text missing from SQLite during RETRY_WAIT"
  fi

  lock_still="$(docker exec "$WRITER_CONTAINER" sh -c 'test -f /opt/vault/.git/index.lock && echo yes || echo no' 2>/dev/null || echo no)"
  if [[ "$lock_still" == "yes" ]]; then
    _pass "writer did not auto-delete .git/index.lock"
  else
    _fail "writer deleted .git/index.lock (must never auto-delete)"
  fi
fi

echo ""

# ── Test 3: remove lock → auto-retry → COMPLETE ──────────────────────────────

echo "--- Test 3: lock removal → auto-retry → COMPLETE ---"

docker exec "$WRITER_CONTAINER" sh -c 'rm -f /opt/vault/.git/index.lock' 2>/dev/null || true

if [[ -n "${CID2:-}" ]] && [[ "$result2" != ERROR* ]]; then
  echo "  polling for COMPLETE on $CID2 after lock removal (up to 90s)..."
  status3="$(_poll_status "$CID2" "COMPLETE" 90 || true)"
  if [[ "$status3" == "COMPLETE" ]]; then
    _pass "after lock removal capture eventually reaches COMPLETE"
  else
    _fail "capture delivery_status=$status3 after 90s (expected COMPLETE after lock removal)"
  fi

  if _vault_clean; then
    _pass "vault working tree clean after lock-removal retry"
  else
    _fail "vault working tree dirty after lock-removal retry"
  fi
else
  _skip "skipping lock-removal test (test 2 did not create capture)"
fi

echo ""

# ── Test 4: duplicate capture_id → terminal FAILED ───────────────────────────
# Strategy: inject two vault files with the same capture_id frontmatter, then
# trigger the same capture_id through n8n.  writer-service returns 409
# capture_id_duplicate; n8n routes to terminal Acknowledge Failed.

echo "--- Test 4: duplicate vault capture_id → FAILED ---"

# Build a fresh synthetic capture to get a real capture_id
result4="$(_create_synthetic "t4-dup" "Duplicate capture ID test — SB-116" 2>&1)"
if [[ "$result4" == ERROR* ]] || [[ -z "$result4" ]]; then
  _fail "create synthetic capture for duplicate test: $result4"
else
  CID4="${result4%%:*}"
  ATT4="${result4##*:}"
  echo "  capture_id=$CID4  delivery_attempt=$ATT4"

  # Inject two vault files with the same capture_id frontmatter so the writer
  # finds a duplicate before writing (without touching the real note).
  docker exec "$WRITER_CONTAINER" sh -c "
    mkdir -p /opt/vault/20_projects/dup-a /opt/vault/20_projects/dup-b
    printf '---\ncapture_id: \"${CID4}\"\n---\n\n# Dup A\n' > /opt/vault/20_projects/dup-a/dup.md
    printf '---\ncapture_id: \"${CID4}\"\n---\n\n# Dup B\n' > /opt/vault/20_projects/dup-b/dup.md
  " 2>/dev/null

  if ! _trigger_intake "$CID4" "$ATT4"; then
    _fail "trigger intake webhook failed"
  else
    # delivery_status column stores "FAILED" (the string constant DELIVERY_FAILED = "FAILED")
    echo "  intake triggered — polling for FAILED (up to 30s)..."
    status4="$(_poll_status "$CID4" "FAILED" 30 || true)"
    if [[ "$status4" == "FAILED" ]]; then
      _pass "duplicate capture_id → delivery_status=FAILED (terminal)"
    else
      _fail "duplicate capture_id → delivery_status=$status4 after 30s (expected FAILED)"
    fi
  fi

  raw_dup="$(_raw_text_present "$CID4" 2>/dev/null || echo no)"
  if [[ "$raw_dup" == "yes" ]]; then
    _pass "raw_text preserved even after terminal failure"
  else
    _fail "raw_text missing after terminal failure"
  fi

  # Clean up injected dup files to keep vault tidy
  docker exec "$WRITER_CONTAINER" sh -c "
    rm -f /opt/vault/20_projects/dup-a/dup.md /opt/vault/20_projects/dup-b/dup.md
    rmdir /opt/vault/20_projects/dup-a /opt/vault/20_projects/dup-b 2>/dev/null || true
  " 2>/dev/null || true
fi

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────

echo "=== Results: $PASS passed, $FAIL failed, $SKIP skipped ==="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
