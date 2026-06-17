import asyncio
import json
from types import SimpleNamespace

from secondbrain.discord_capture import should_capture_message
from secondbrain.worker import run_capture_worker


async def ingest_if_allowed(message, settings, handler):
    if should_capture_message(message, settings):
        await handler(message)


async def drain_worker(app, *, timeout=1.0):
    worker = asyncio.create_task(
        run_capture_worker(
            settings=app.settings,
            ledger=app.ledger,
            queue=app.queue,
            vault_writer=app.vault_writer,
            classifier_client=app.classifier,
            receipt_client=app.discord,
        )
    )
    try:
        await asyncio.wait_for(app.queue.join(), timeout=timeout)
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


def make_app(settings, ledger, queue, vault_writer, classifier, discord):
    return SimpleNamespace(
        settings=settings,
        ledger=ledger,
        queue=queue,
        vault_writer=vault_writer,
        classifier=classifier,
        discord=discord,
    )


def note_files(vault_path):
    return sorted(path for path in vault_path.rglob("*.md") if "99_log" not in path.parts)


def audit_events(vault_path):
    path = vault_path / "99_log" / "events.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def ledger_rows(ledger):
    return ledger._connection.execute("SELECT * FROM captures ORDER BY id").fetchall()


def event_types(ledger, capture_id):
    rows = ledger._connection.execute(
        "SELECT event_type FROM capture_events WHERE capture_id = ? ORDER BY id",
        (capture_id,),
    ).fetchall()
    return [row["event_type"] for row in rows]
