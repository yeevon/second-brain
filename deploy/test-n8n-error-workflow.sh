#!/usr/bin/env bash
# Local regression script for the SB-113 n8n error workflow.
#
# Usage (no arguments required):
#   deploy/test-n8n-error-workflow.sh
#
# Prerequisites:
#   1. Local stack running:      deploy/local-stack-up.sh
#   2. n8n one-time setup done:  deploy/setup-local-n8n.sh
#   3. Credentials bound in UI + Error Harness activated (see setup-local-n8n.sh output)
#
# The script creates and manages its own synthetic test capture — no manual
# capture ID, delivery attempt, or token input required.
#
# API calls to capture-service use docker exec (no published port required).
# Webhook calls to n8n use the published 127.0.0.1:5678 port.
#
# DO NOT run in EC2/staging/production.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

N8N_CONTAINER="${N8N_CONTAINER:-second-brain-n8n}"
CS_CONTAINER="${CS_CONTAINER:-second-brain-capture-service}"
N8N_URL="${N8N_URL:-http://127.0.0.1:5678}"
ENV_FILE="${ROOT_DIR}/.env"
TEST_ENV_FILE="${ROOT_DIR}/n8n-test.local.env"
ERRORS=0

# Per-run unique ID avoids re-use collision with prior incomplete runs.
RUN_ID="$(date +%s)"
DISCORD_MSG_ID="SB-TEST-ERR-WF-${RUN_ID}"
CAPTURE_ID=""
DELIVERY_ATTEMPT=""

# ── Load tokens ───────────────────────────────────────────────────────────────

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  echo "Run deploy/local-stack-up.sh first." >&2
  exit 1
fi
if [[ ! -f "$TEST_ENV_FILE" ]]; then
  echo "Missing test env file: $TEST_ENV_FILE" >&2
  echo "Run deploy/setup-local-n8n.sh first." >&2
  exit 1
fi

CAPTURE_SERVICE_INTERNAL_TOKEN="$(grep '^CAPTURE_SERVICE_INTERNAL_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)"
if [[ -z "$CAPTURE_SERVICE_INTERNAL_TOKEN" ]]; then
  echo "CAPTURE_SERVICE_INTERNAL_TOKEN not found in $ENV_FILE" >&2; exit 1
fi

TEST_HARNESS_TOKEN="$(grep '^TEST_HARNESS_TOKEN=' "$TEST_ENV_FILE" | cut -d= -f2- || true)"
if [[ -z "$TEST_HARNESS_TOKEN" ]]; then
  echo "TEST_HARNESS_TOKEN not found in $TEST_ENV_FILE" >&2
  echo "Run deploy/setup-local-n8n.sh to generate it." >&2
  exit 1
fi

# ── Helper: call capture-service via backend network ─────────────────────────
# Uses Node fetch() (built-in since Node 18). curl is not guaranteed in n8n image.
# Body is encoded via jq so no host-Python dependency for escaping.

cs_fetch() {
  local url="$1"
  local method="${2:-GET}"
  local body="${3:-}"
  local token="$CAPTURE_SERVICE_INTERNAL_TOKEN"

  local node_body_js=""
  if [[ -n "$body" ]]; then
    # jq -Rs '.' produces a JSON string literal (with escaping) from raw text.
    local json_literal
    json_literal="$(jq -Rs '.' <<< "$body")"
    node_body_js="opts.body = ${json_literal};"
  fi

  docker exec "$N8N_CONTAINER" node -e "
    const opts = {
      method: '${method}',
      headers: {
        'X-Second-Brain-Internal-Token': '${token}',
        'Content-Type': 'application/json',
      },
    };
    ${node_body_js}
    fetch('${url}', opts)
      .then(r => r.text().then(t => {
        if (!r.ok) { process.stderr.write('HTTP ' + r.status + ': ' + t + '\n'); process.exit(1); }
        process.stdout.write(t);
      }))
      .catch(e => { process.stderr.write(String(e) + '\n'); process.exit(1); });
  " 2>/dev/null
}

# ── Cleanup: mark synthetic capture DELIVERY_FAILED via direct SQL ────────────
# The production workflow-error API cannot be used for cleanup: it rejects a
# terminal disposition after a prior retryable report for the same attempt as
# ignored_conflicting_replay. Direct SQL bypasses that guard safely because the
# synthetic capture was itself created via direct SQL (local-only test helper).

cleanup() {
  if [[ -n "$CAPTURE_ID" ]]; then
    docker exec -i \
      -e "CLEANUP_CAPTURE_ID=$CAPTURE_ID" \
      "$CS_CONTAINER" python3 - <<'PYEOF' >/dev/null 2>&1 || true
import sqlite3, os, json
from datetime import UTC, datetime

ledger_path = os.environ.get('LEDGER_PATH', '/var/lib/second-brain/ledger.sqlite3')
capture_id = os.environ['CLEANUP_CAPTURE_ID']
now_iso = datetime.now(UTC).isoformat()

with sqlite3.connect(ledger_path, timeout=10) as conn:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """UPDATE captures
           SET status='FAILED', delivery_status='DELIVERY_FAILED',
               processing_lease_until=NULL, next_attempt_at=NULL,
               last_error='test_cleanup', updated_at=?
           WHERE capture_id=?""",
        (now_iso, capture_id),
    )
    conn.execute(
        """INSERT INTO capture_events
               (capture_id, event_type, event_payload_json, created_at)
           VALUES (?, 'TEST_CLEANUP', ?, ?)""",
        (capture_id, json.dumps({"reason": "test_cleanup"}), now_iso),
    )
    conn.commit()
PYEOF
  fi
  rm -f "$TMP_WF"
}

TMP_WF="$(mktemp)"
trap cleanup EXIT

echo "=== SB-113 n8n error workflow regression ==="
echo ""

# ── Pre-flight: container health ──────────────────────────────────────────────

echo "--- Pre-flight: health ---"

for container in "$N8N_CONTAINER" "$CS_CONTAINER"; do
  h="$(docker inspect \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
    "$container" 2>/dev/null || true)"
  if [[ "$h" != "healthy" ]]; then
    echo "  FAIL: $container is not healthy (status: $h)" >&2
    echo "  Run deploy/local-stack-up.sh and wait for healthy status." >&2
    exit 1
  fi
  echo "  $container: healthy"
done

n8n_resp="$(curl -sf "$N8N_URL/healthz" 2>/dev/null || true)"
if ! echo "$n8n_resp" | grep -qiE '"status":"ok"|"ok"'; then
  echo "  FAIL: n8n not reachable at $N8N_URL" >&2; exit 1
fi
echo "  n8n HTTP: reachable"

# ── Pre-flight: required workflows present, Error Harness active ──────────────

echo "--- Pre-flight: workflows ---"

docker exec "$N8N_CONTAINER" \
  n8n export:workflow --all --output=/tmp/sb-test-pre.json >/dev/null 2>&1
docker cp "$N8N_CONTAINER:/tmp/sb-test-pre.json" "$TMP_WF" 2>/dev/null
docker exec --user root "$N8N_CONTAINER" rm -f /tmp/sb-test-pre.json

for wf_name in "Second Brain - Error Handler" "Second Brain - Error Harness"; do
  if ! jq -r '.[].name' "$TMP_WF" 2>/dev/null | grep -qxF "$wf_name"; then
    echo "  FAIL: '$wf_name' not found" >&2
    echo "  Run: deploy/setup-local-n8n.sh" >&2
    exit 1
  fi
  echo "  $wf_name: present"
done

harness_active="$(jq -r '.[] | select(.name=="Second Brain - Error Harness") | .active' \
  "$TMP_WF" 2>/dev/null || echo "false")"
if [[ "$harness_active" != "true" ]]; then
  echo "  FAIL: Second Brain - Error Harness is not active" >&2
  echo "  Bind credentials and activate the harness in the n8n UI." >&2
  echo "  See: deploy/setup-local-n8n.sh output for the exact steps." >&2
  exit 1
fi
echo "  Second Brain - Error Harness: active"

# ── Step 1: Create synthetic test capture and advance to FORWARDING ───────────

echo ""
echo "--- Step 1: create synthetic test capture ---"

# Runs entirely inside the capture-service container via its Python environment.
# No test endpoint added to production code.
#
# Uses a single atomic SQLite transaction that inserts the capture directly in
# FORWARDING state — there is no PENDING_FORWARD intermediate, so the
# dispatcher cannot race between insert and claim.
#
# The -i flag on docker exec is required so the heredoc reaches python3 stdin.
create_result="$(
  docker exec -i \
    -e "DISCORD_MSG_ID=$DISCORD_MSG_ID" \
    "$CS_CONTAINER" python3 - <<'PYEOF'
import sys, os, json, sqlite3
from datetime import UTC, datetime, timedelta

ledger_path = os.environ.get('LEDGER_PATH', '/var/lib/second-brain/ledger.sqlite3')
msg_id = os.environ['DISCORD_MSG_ID']
now = datetime.now(UTC)
now_iso = now.isoformat()
lease_iso = (now + timedelta(hours=2)).isoformat()
delivery_attempts = 1

with sqlite3.connect(ledger_path, timeout=15) as conn:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")

    if conn.execute(
        "SELECT 1 FROM captures WHERE discord_message_id=?", (msg_id,)
    ).fetchone():
        print(f'ERROR: discord_message_id {msg_id} already exists', file=sys.stderr)
        sys.exit(1)

    # Generate capture_id matching ledger pattern: SB-YYYYMMDD-NNNN
    prefix = f"SB-{now.strftime('%Y%m%d')}-"
    row = conn.execute(
        "SELECT capture_id FROM captures WHERE capture_id LIKE ? ORDER BY capture_id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    next_number = (int(row["capture_id"].rsplit("-", 1)[1]) + 1) if row else 1
    capture_id = f"{prefix}{next_number:04d}"

    # Insert directly in FORWARDING state — atomic, no dispatcher race
    conn.execute(
        """INSERT INTO captures (
               capture_id, discord_message_id, discord_channel_id, discord_guild_id,
               discord_author_id, raw_text, is_sensitive, has_attachments,
               attachment_metadata_json, received_at, status, delivery_status,
               delivery_attempts, processing_lease_until, updated_at
           ) VALUES (?, ?, '0', '0', '0', ?, 0, 0, '[]', ?, 'RECEIVED', 'FORWARDING', ?, ?, ?)""",
        (capture_id, msg_id,
         'SB-113 error workflow regression test — safe to delete',
         now_iso, delivery_attempts, lease_iso, now_iso),
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
)"

if [[ -z "$create_result" || "$create_result" == ERROR* ]]; then
  echo "  FAIL: could not create synthetic capture" >&2
  echo "  $create_result" >&2
  exit 1
fi

CAPTURE_ID="${create_result%%:*}"
DELIVERY_ATTEMPT="${create_result##*:}"
echo "  capture_id=${CAPTURE_ID}  delivery_attempt=${DELIVERY_ATTEMPT}  status=FORWARDING"

# ── Step 2: Trigger gemini_timeout via Error Harness ─────────────────────────

echo ""
echo "--- Step 2: trigger gemini_timeout via Error Harness ---"
curl -sf \
  --request POST \
  --header "Content-Type: application/json" \
  --header "X-Second-Brain-Webhook-Token: $TEST_HARNESS_TOKEN" \
  --data "{\"capture_id\":\"$CAPTURE_ID\",\"delivery_attempt\":${DELIVERY_ATTEMPT},\"test_case\":\"gemini_timeout\"}" \
  "$N8N_URL/webhook/second-brain-error-harness" >/dev/null 2>&1 \
  || echo "  (harness webhook returned non-2xx; n8n execution may still complete asynchronously)"
echo "  harness triggered — waiting 5s for error workflow to complete..."
sleep 5

# ── Step 3: Verify RETRY_WAIT state ──────────────────────────────────────────

echo "--- Step 3: verify RETRY_WAIT ---"
state="$(cs_fetch "http://capture-service:8000/internal/captures/$CAPTURE_ID" || true)"

if echo "$state" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["delivery_status"] == "RETRY_WAIT", \
    f"expected RETRY_WAIT, got {d[\"delivery_status\"]}"
assert d["retry_attempts"] >= 1, \
    f"expected retry_attempts >= 1, got {d[\"retry_attempts\"]}"
assert d["next_attempt_at"] is not None, "expected next_attempt_at to be set"
assert d["raw_text"] is not None, "raw_text must not be null"
print(f"  delivery_status=RETRY_WAIT  retry_attempts={d[\"retry_attempts\"]}  PASS")
' 2>/dev/null; then
  :
else
  echo "  FAIL: capture not in expected RETRY_WAIT state" >&2
  echo "  state: $state" >&2
  ERRORS=$((ERRORS + 1))
fi

# ── Step 4: Replay — idempotency ──────────────────────────────────────────────

echo "--- Step 4: replay idempotency ---"
replay="$(cs_fetch \
  "http://capture-service:8000/internal/captures/$CAPTURE_ID/delivery/report-workflow-error" \
  "POST" \
  "{\"delivery_attempt\":${DELIVERY_ATTEMPT},\"disposition\":\"retryable\",\"error_type\":\"gemini_timeout\",\"reason_type\":\"workflow_error\",\"workflow_id\":\"test_workflow\",\"workflow_name\":\"second_brain_intake\",\"stage\":\"gemini\"}" \
  || true)"

if echo "$replay" | python3 -c '
import sys, json
d = json.load(sys.stdin)
ok = ("ignored_retry_already_scheduled", "ignored_already_terminal", "ignored_stale_attempt")
assert d["outcome"] in ok, f"unexpected outcome: {d[\"outcome\"]}"
print(f"  outcome={d[\"outcome\"]}  PASS")
' 2>/dev/null; then
  :
else
  echo "  FAIL: replay did not return an ignored outcome" >&2
  echo "  response: $replay" >&2
  ERRORS=$((ERRORS + 1))
fi

# ── Step 5: raw_text preserved ────────────────────────────────────────────────

echo "--- Step 5: raw_text preserved ---"
raw_text="$(
  cs_fetch "http://capture-service:8000/internal/captures/$CAPTURE_ID" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("raw_text",""))' \
    || true
)"
if [[ -n "$raw_text" ]]; then
  echo "  raw_text present  PASS"
else
  echo "  FAIL: raw_text is null or missing" >&2
  ERRORS=$((ERRORS + 1))
fi

# ── Step 6: Orphan error does not mutate the test capture ─────────────────────

echo "--- Step 6: orphan error ---"
curl -sf \
  --request POST \
  --header "Content-Type: application/json" \
  --header "X-Second-Brain-Webhook-Token: $TEST_HARNESS_TOKEN" \
  --data '{"test_case":"orphan_unhandled_exception"}' \
  "$N8N_URL/webhook/second-brain-error-harness" >/dev/null 2>&1 || true
sleep 2

orphan_state="$(cs_fetch "http://capture-service:8000/internal/captures/$CAPTURE_ID" || true)"
if echo "$orphan_state" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["delivery_status"] == "RETRY_WAIT", \
    f"capture status changed unexpectedly: {d[\"delivery_status\"]}"
print("  orphan did not mutate test capture  PASS")
' 2>/dev/null; then
  :
else
  echo "  FAIL: orphan error mutated the test capture unexpectedly" >&2
  echo "  state: $orphan_state" >&2
  ERRORS=$((ERRORS + 1))
fi

# ── Log leak check ────────────────────────────────────────────────────────────

echo "--- Log leak check ---"
if docker logs "$N8N_CONTAINER" --since=30s 2>&1 \
    | grep -qi "password\|api_key\|raw_text\|\.stack\|Traceback"; then
  echo "  FAIL: potential secret or raw capture content found in n8n logs" >&2
  ERRORS=$((ERRORS + 1))
else
  echo "  n8n logs clean  PASS"
fi

# ── Result ────────────────────────────────────────────────────────────────────

echo ""
if [[ $ERRORS -eq 0 ]]; then
  echo "All tests passed."
else
  echo "$ERRORS test(s) FAILED." >&2
  exit 1
fi
