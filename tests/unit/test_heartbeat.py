from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from secondbrain.heartbeat import run_capture_service_heartbeat
from secondbrain.ledger import Ledger


_NOW = datetime(2026, 6, 9, 15, 0, 0, tzinfo=UTC)
_INSTANCE_A = "instance-a-uuid"
_INSTANCE_B = "instance-b-uuid"


def make_ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite3")


def _read_state(ledger: Ledger, key: str) -> str | None:
    return ledger.get_system_state(key)


# ---------------------------------------------------------------------------
# record_capture_service_start
# ---------------------------------------------------------------------------

def test_capture_service_start_records_starting_state(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record_capture_service_start(instance_id=_INSTANCE_A, now=_NOW)

    assert _read_state(ledger, "capture_service_state") == "STARTING"
    assert _read_state(ledger, "capture_service_instance_id") == _INSTANCE_A
    assert _read_state(ledger, "capture_service_started_at") is not None


def test_capture_service_start_clears_stopped_at(tmp_path):
    ledger = make_ledger(tmp_path)
    # First lifecycle: start → ready → stop
    ledger.record_capture_service_start(instance_id=_INSTANCE_A, now=_NOW)
    ledger.record_capture_service_ready(instance_id=_INSTANCE_A, now=_NOW + timedelta(seconds=5))
    ledger.record_capture_service_stop(instance_id=_INSTANCE_A, now=_NOW + timedelta(seconds=10))
    assert _read_state(ledger, "capture_service_stopped_at") is not None

    # Second lifecycle: start should clear stopped_at (stored as "" in NOT NULL column)
    ledger.record_capture_service_start(instance_id=_INSTANCE_B, now=_NOW + timedelta(seconds=20))

    assert not _read_state(ledger, "capture_service_stopped_at")


# ---------------------------------------------------------------------------
# record_capture_service_ready
# ---------------------------------------------------------------------------

def test_capture_service_ready_records_running_state(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record_capture_service_start(instance_id=_INSTANCE_A, now=_NOW)
    ledger.record_capture_service_ready(instance_id=_INSTANCE_A, now=_NOW + timedelta(seconds=5))

    assert _read_state(ledger, "capture_service_state") == "RUNNING"
    assert _read_state(ledger, "capture_service_last_heartbeat_at") is not None


def test_capture_service_ready_is_no_op_for_different_instance(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record_capture_service_start(instance_id=_INSTANCE_A, now=_NOW)
    # A different instance (e.g., stale process) tries to mark itself ready
    ledger.record_capture_service_ready(instance_id=_INSTANCE_B, now=_NOW + timedelta(seconds=5))

    assert _read_state(ledger, "capture_service_state") == "STARTING"


# ---------------------------------------------------------------------------
# record_capture_service_heartbeat
# ---------------------------------------------------------------------------

def test_capture_service_heartbeat_updates_matching_instance(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record_capture_service_start(instance_id=_INSTANCE_A, now=_NOW)
    ledger.record_capture_service_ready(instance_id=_INSTANCE_A, now=_NOW + timedelta(seconds=5))

    hb_time = _NOW + timedelta(seconds=20)
    updated = ledger.record_capture_service_heartbeat(instance_id=_INSTANCE_A, now=hb_time)

    assert updated is True
    hb_stored = _read_state(ledger, "capture_service_last_heartbeat_at")
    assert hb_stored is not None
    assert hb_time.isoformat() in hb_stored


def test_old_instance_heartbeat_cannot_overwrite_new_instance(tmp_path):
    ledger = make_ledger(tmp_path)
    # New instance is registered
    ledger.record_capture_service_start(instance_id=_INSTANCE_B, now=_NOW)
    ledger.record_capture_service_ready(instance_id=_INSTANCE_B, now=_NOW + timedelta(seconds=2))
    new_hb = _NOW + timedelta(seconds=10)
    ledger.record_capture_service_heartbeat(instance_id=_INSTANCE_B, now=new_hb)

    # Old instance tries to write a heartbeat
    old_hb = _NOW + timedelta(seconds=15)
    updated = ledger.record_capture_service_heartbeat(instance_id=_INSTANCE_A, now=old_hb)

    assert updated is False
    hb_stored = _read_state(ledger, "capture_service_last_heartbeat_at")
    assert new_hb.isoformat() in hb_stored


# ---------------------------------------------------------------------------
# record_capture_service_stop
# ---------------------------------------------------------------------------

def test_capture_service_stop_records_stopped_state(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record_capture_service_start(instance_id=_INSTANCE_A, now=_NOW)
    ledger.record_capture_service_ready(instance_id=_INSTANCE_A, now=_NOW + timedelta(seconds=5))

    stop_time = _NOW + timedelta(minutes=5)
    updated = ledger.record_capture_service_stop(instance_id=_INSTANCE_A, now=stop_time)

    assert updated is True
    assert _read_state(ledger, "capture_service_state") == "STOPPED"
    stopped_at = _read_state(ledger, "capture_service_stopped_at")
    assert stopped_at is not None
    assert stop_time.isoformat() in stopped_at


def test_old_instance_shutdown_cannot_stop_new_instance(tmp_path):
    ledger = make_ledger(tmp_path)
    # New instance starts
    ledger.record_capture_service_start(instance_id=_INSTANCE_B, now=_NOW)
    ledger.record_capture_service_ready(instance_id=_INSTANCE_B, now=_NOW + timedelta(seconds=2))

    # Old instance (A) sends a stop
    updated = ledger.record_capture_service_stop(
        instance_id=_INSTANCE_A,
        now=_NOW + timedelta(seconds=10),
    )

    assert updated is False
    assert _read_state(ledger, "capture_service_state") == "RUNNING"


# ---------------------------------------------------------------------------
# run_capture_service_heartbeat async loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_capture_service_heartbeat_calls_record_on_interval():
    calls = []

    class FakeLedger:
        def record_capture_service_heartbeat(self, *, instance_id, now):
            calls.append((instance_id, now))
            return True

    ledger = FakeLedger()
    task = asyncio.create_task(
        run_capture_service_heartbeat(
            ledger=ledger,
            instance_id=_INSTANCE_A,
            interval_seconds=0,  # zero sleep so it fires quickly
        )
    )
    # Let the loop fire a couple of times
    for _ in range(3):
        await asyncio.sleep(0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(calls) >= 2
    assert all(iid == _INSTANCE_A for iid, _ in calls)


@pytest.mark.asyncio
async def test_run_capture_service_heartbeat_swallows_exceptions():
    error_count = 0

    class FailingLedger:
        def record_capture_service_heartbeat(self, *, instance_id, now):
            nonlocal error_count
            error_count += 1
            raise RuntimeError("db unavailable")

    ledger = FailingLedger()
    task = asyncio.create_task(
        run_capture_service_heartbeat(
            ledger=ledger,
            instance_id=_INSTANCE_A,
            interval_seconds=0,
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Task should still be alive after errors (loop doesn't crash)
    assert error_count >= 1


@pytest.mark.asyncio
async def test_heartbeat_loop_exits_when_instance_is_superseded():
    calls = []

    class FakeLedger:
        def record_capture_service_heartbeat(self, *, instance_id, now):
            calls.append(instance_id)
            return False  # superseded by a newer instance

    task = asyncio.create_task(
        run_capture_service_heartbeat(
            ledger=FakeLedger(),
            instance_id=_INSTANCE_A,
            interval_seconds=0,
        )
    )
    await task  # must complete cleanly, not hang

    assert len(calls) == 1  # called once then exited
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_heartbeat_loop_logs_superseded_instance_safely(capsys):
    class FakeLedger:
        def record_capture_service_heartbeat(self, *, instance_id, now):
            return False  # superseded

    task = asyncio.create_task(
        run_capture_service_heartbeat(
            ledger=FakeLedger(),
            instance_id=_INSTANCE_A,
            interval_seconds=0,
        )
    )
    await task

    output = capsys.readouterr().out
    assert "capture_service_heartbeat_superseded" in output
    assert _INSTANCE_A in output
