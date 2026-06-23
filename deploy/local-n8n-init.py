#!/usr/bin/env python3
"""
Automated local-dev n8n setup.

Runs once as the local-n8n-init Compose service after n8n is healthy.
Uses n8n's internal /rest/ API (N8N_PUBLIC_API_DISABLED=true in local env).

Auth: n8n 2.x uses cookie-based sessions. We POST /rest/login and replay
the Set-Cookie header automatically via a CookieJar opener.

Steps:
  1. Set up owner account (idempotent — skips if owner already exists)
  2. Login (session cookie stored automatically)
  3. Create four HTTP-header-auth credentials (idempotent)
  4. Import Second Brain - Error Handler (patch credential IDs)
  5. Import Second Brain - Intake (patch credential IDs + error-workflow ref)
  6. Activate the Intake workflow (registers the production webhook)
  7. Verify /webhook/second-brain-intake responds non-404
"""
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request


# ── Environment ───────────────────────────────────────────────────────────────

def _require(key, default=None):
    val = os.environ.get(key, default)
    if not val:
        print(f"ERROR: required env var {key!r} is not set", file=sys.stderr)
        sys.exit(1)
    return val


N8N_URL        = os.environ.get("N8N_URL", "http://n8n:5678")
LOCAL_EMAIL    = os.environ.get("N8N_LOCAL_EMAIL", "admin@second-brain.local")
LOCAL_PASSWORD = _require("N8N_LOCAL_PASSWORD")
LOCAL_FIRST    = os.environ.get("N8N_LOCAL_FIRST_NAME", "Local")
LOCAL_LAST     = os.environ.get("N8N_LOCAL_LAST_NAME", "Dev")
CAPTURE_TOKEN  = _require("CAPTURE_SERVICE_INTERNAL_TOKEN")
WRITER_TOKEN   = _require("WRITER_SERVICE_TOKEN")
INTAKE_TOKEN   = _require("N8N_INTAKE_WEBHOOK_TOKEN")
GEMINI_KEY     = _require("GEMINI_API_KEY")

with open("/workflows/second-brain-error-handler.json") as f:
    ERROR_HANDLER_WF = json.load(f)
with open("/workflows/second-brain-intake.json") as f:
    INTAKE_WF = json.load(f)
with open("/workflows/second-brain-daily-digest.json") as f:
    DAILY_DIGEST_WF = json.load(f)
with open("/workflows/second-brain-weekly-review.json") as f:
    WEEKLY_REVIEW_WF = json.load(f)


# ── HTTP helpers (cookie-aware) ───────────────────────────────────────────────

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookie_jar)
)


def _api(method, path, body=None, *, ok_statuses=(200, 201)):
    url = f"{N8N_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"_raw": raw.decode(errors="replace")[:300]}
        if ok_statuses and exc.code not in ok_statuses:
            raise RuntimeError(
                f"{method} {path} → HTTP {exc.code}: {json.dumps(parsed)[:300]}"
            ) from None
        return exc.code, parsed


def _unwrap(body):
    """n8n wraps most responses as {data: ...}."""
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _retry(fn, *, attempts=None, delay_s=None, label=""):
    _attempts = attempts if attempts is not None else int(os.environ.get("N8N_INIT_RETRY_ATTEMPTS", "5"))
    _delay = delay_s if delay_s is not None else int(os.environ.get("N8N_INIT_RETRY_DELAY_S", "3"))
    for attempt in range(1, _attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == _attempts:
                raise
            print(f"  {label or 'step'} attempt {attempt}/{_attempts} failed ({exc}), retrying in {_delay}s…")
            time.sleep(_delay)


# ── Owner setup ───────────────────────────────────────────────────────────────

def setup_owner():
    status, body = _api("POST", "/rest/owner/setup", {
        "firstName": LOCAL_FIRST,
        "lastName": LOCAL_LAST,
        "email": LOCAL_EMAIL,
        "password": LOCAL_PASSWORD,
    }, ok_statuses=None)
    if status == 200:
        print(f"  Owner created: {LOCAL_EMAIL}")
    elif status == 400:
        print(f"  Owner already configured — skipping setup")
    elif status == 404:
        raise RuntimeError(
            "n8n REST API not ready: /rest/owner/setup returned 404. "
            "The healthcheck should have prevented this — check n8n startup logs."
        )
    else:
        raise RuntimeError(
            f"Unexpected status from /rest/owner/setup: HTTP {status} — {json.dumps(body)[:200]}"
        )


# ── Auth ──────────────────────────────────────────────────────────────────────

def login():
    status, body = _api("POST", "/rest/login", {
        "emailOrLdapLoginId": LOCAL_EMAIL,
        "password": LOCAL_PASSWORD,
    }, ok_statuses=None)
    if status == 401:
        print(
            "\nERROR: n8n login returned 401 — owner account credentials do not match.\n"
            "\nLikely causes:\n"
            "  1. Stale n8n data volume from a previous session with different credentials.\n"
            "     Fix: docker compose down && docker volume rm second-brain-local-n8n-data\n"
            "          then re-run docker compose up -d\n"
            "  2. N8N_LOCAL_EMAIL or N8N_LOCAL_PASSWORD changed since the volume was created.\n"
            "     Fix: align the env vars with the credentials stored in the volume,\n"
            "          or wipe the volume as above.\n"
            "  3. Owner was never created (setup step skipped).\n"
            "     Fix: open http://localhost:5678 and complete the owner setup in the UI.\n"
            "\nDo not include passwords in bug reports.",
            file=sys.stderr,
        )
        sys.exit(1)
    if status not in (200, 201):
        raise RuntimeError(
            f"POST /rest/login → HTTP {status}: {json.dumps(body)[:300]}"
        )
    data = _unwrap(body)
    if not isinstance(data, dict) or "email" not in data:
        raise RuntimeError(f"Login response did not contain user object: {body}")
    print(f"  Logged in as {data['email']}")


# ── Credentials ───────────────────────────────────────────────────────────────

def _find_credential(name):
    _, body = _api("GET", "/rest/credentials", ok_statuses=(200,))
    items = _unwrap(body)
    if isinstance(items, list):
        for c in items:
            if c.get("name") == name:
                return str(c["id"])
    return None


def create_or_find_credential(name, header_name, header_value):
    existing = _find_credential(name)
    if existing:
        print(f"  Credential exists:  {name!r} (id={existing})")
        return existing
    _, body = _api("POST", "/rest/credentials", {
        "name": name,
        "type": "httpHeaderAuth",
        "data": {"name": header_name, "value": header_value},
    })
    data = _unwrap(body)
    cred_id = str(data.get("id") if isinstance(data, dict) else body.get("id"))
    print(f"  Credential created: {name!r} (id={cred_id})")
    return cred_id


# ── Workflows ─────────────────────────────────────────────────────────────────

def _find_workflow(name):
    _, body = _api("GET", "/rest/workflows", ok_statuses=(200,))
    items = _unwrap(body)
    if isinstance(items, list):
        for wf in items:
            if wf.get("name") == name:
                return str(wf["id"])
    return None


def import_or_update_workflow(wf_json):
    name = wf_json["name"]
    existing = _find_workflow(name)
    if existing:
        update_json = dict(wf_json)
        update_json["id"] = existing
        update_json.pop("versionId", None)
        _api(
            "PATCH",
            f"/rest/workflows/{existing}?forceSave=true",
            update_json,
            ok_statuses=(200,),
        )
        print(f"  {name}: updated in place")
        return existing

    _, body = _api("POST", "/rest/workflows", wf_json)
    data = _unwrap(body)
    wf_id = str(data.get("id") if isinstance(data, dict) else body.get("id"))
    print(f"  {name}: imported")
    return wf_id


def activate_workflow(wf_id):
    # n8n 2.x requires versionId in the activate body to prevent race conditions.
    _, body = _api("GET", f"/rest/workflows/{wf_id}")
    data = _unwrap(body)
    version_id = data.get("versionId") if isinstance(data, dict) else None
    if not version_id:
        raise RuntimeError(f"Workflow {wf_id} has no versionId: {body}")
    _api("POST", f"/rest/workflows/{wf_id}/activate", {"versionId": version_id})
    print(f"  Activated workflow id={wf_id}")


# ── Patching ──────────────────────────────────────────────────────────────────

def patch_json(wf, replacements):
    """Replace placeholder strings in the serialised workflow JSON."""
    text = json.dumps(wf)
    for placeholder, real in replacements.items():
        text = text.replace(f'"{placeholder}"', f'"{real}"')
    return json.loads(text)


# ── Webhook verification ──────────────────────────────────────────────────────

def verify_webhook():
    url = f"{N8N_URL}/webhook/second-brain-intake"
    for attempt in range(15):
        req = urllib.request.Request(
            url, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Second-Brain-Intake-Token": INTAKE_TOKEN,
            },
            data=b'{"capture_id":"SB-00000000-0000","delivery_attempt":1}',
        )
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        if status in (401, 403):
            raise RuntimeError(
                f"Intake webhook returned HTTP {status} — "
                "credential binding or Intake token configuration is broken"
            )
        if status != 404:
            print(f"  Webhook verified (HTTP {status})")
            return
        print(f"  Waiting for webhook registration (attempt {attempt + 1}/15)…")
        time.sleep(2)
    raise RuntimeError("Intake webhook still returns 404 after 30 s")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== local-n8n-init: configuring n8n for local dev ===")

    print("Setting up owner account…")
    setup_owner()

    print("Logging in…")
    login()

    print("Creating credentials…")
    cs_id     = create_or_find_credential(
        "Capture Service Token",
        "X-Second-Brain-Internal-Token", CAPTURE_TOKEN,
    )
    ws_id     = create_or_find_credential(
        "Second Brain - Writer Service Header",
        "X-Second-Brain-Writer-Token", WRITER_TOKEN,
    )
    intake_id = create_or_find_credential(
        "Intake Webhook Token",
        "X-Second-Brain-Intake-Token", INTAKE_TOKEN,
    )
    gemini_id = create_or_find_credential(
        "Gemini API Key",
        "x-goog-api-key", GEMINI_KEY,
    )

    cred_patches = {
        "PLACEHOLDER_CAPTURE_SERVICE_TOKEN": cs_id,
        "PLACEHOLDER_WRITER_SERVICE_TOKEN":  ws_id,
        "PLACEHOLDER_INTAKE_WEBHOOK_TOKEN":  intake_id,
        "PLACEHOLDER_GEMINI_API_KEY":        gemini_id,
    }

    print("Importing Error Handler workflow…")
    eh_json = patch_json(ERROR_HANDLER_WF, cred_patches)
    eh_id   = _retry(lambda: import_or_update_workflow(eh_json), label="import workflow")

    print("Activating Error Handler workflow…")
    _retry(lambda: activate_workflow(eh_id), label="activate workflow")

    print("Importing Intake workflow…")
    intake_patches = dict(cred_patches)
    intake_patches["PLACEHOLDER_SECOND_BRAIN_ERROR_HANDLER"] = eh_id
    intake_json  = patch_json(INTAKE_WF, intake_patches)
    intake_wf_id = _retry(lambda: import_or_update_workflow(intake_json), label="import workflow")

    print("Activating Intake workflow…")
    _retry(lambda: activate_workflow(intake_wf_id), label="activate workflow")

    print("Verifying webhook registration…")
    _retry(lambda: verify_webhook(), label="verify webhook")

    print("Importing Daily Digest workflow…")
    daily_digest_json = patch_json(DAILY_DIGEST_WF, cred_patches)
    daily_digest_id   = _retry(lambda: import_or_update_workflow(daily_digest_json), label="import workflow")

    print("Importing Weekly Review workflow…")
    weekly_review_json = patch_json(WEEKLY_REVIEW_WF, cred_patches)
    weekly_review_id   = _retry(lambda: import_or_update_workflow(weekly_review_json), label="import workflow")

    print("=== local-n8n-init complete ===")
    print(f"  Error Handler  id={eh_id} (active)")
    print(f"  Intake         id={intake_wf_id} (active)")
    print(f"  Webhook        POST {N8N_URL}/webhook/second-brain-intake")
    print(f"  Daily Digest   id={daily_digest_id} (inactive — activate manually after setting DISCORD_DIGEST_WEBHOOK_URL)")
    print(f"  Weekly Review  id={weekly_review_id} (inactive — activate manually after setting DISCORD_DIGEST_WEBHOOK_URL)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
