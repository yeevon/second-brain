from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from secondbrain.heartbeat import mark_task_not_applicable, run_capture_service_heartbeat, _check_background_task_liveness
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


# ---------------------------------------------------------------------------
# SB-137: per-task status — not_applicable, running, degraded
# ---------------------------------------------------------------------------

class _StateLedger:
    """Minimal ledger stub supporting get/set system_state and heartbeat."""

    def __init__(self, initial: dict | None = None):
        self._state: dict[str, str] = initial or {}
        self._hb_returns = True

    def get_system_state(self, key: str) -> str | None:
        return self._state.get(key)

    def set_system_state(self, key: str, value: str) -> None:
        self._state[key] = value

    def record_capture_service_heartbeat(self, *, instance_id, now):
        return self._hb_returns


def test_mark_task_not_applicable_writes_status(tmp_path):
    ledger = make_ledger(tmp_path)
    mark_task_not_applicable(ledger, "classifier")
    assert ledger.get_system_state("classifier_task_status") == "not_applicable"


def test_not_applicable_task_never_reported_as_degraded():
    stub = _StateLedger({"classifier_task_status": "not_applicable"})
    stub.set_system_state("classifier_last_heartbeat_at", (datetime.now(UTC) - timedelta(hours=10)).isoformat())
    _check_background_task_liveness(
        ledger=stub,
        reaper_liveness_threshold_s=300,
        reconcile_liveness_threshold_s=300,
        classifier_liveness_threshold_s=300,
    )
    assert stub.get_system_state("classifier_task_status") == "not_applicable"


def test_healthy_task_shows_running_status():
    now = datetime.now(UTC)
    stub = _StateLedger({
        "reaper_last_heartbeat_at": (now - timedelta(seconds=30)).isoformat(),
    })
    _check_background_task_liveness(
        ledger=stub,
        reaper_liveness_threshold_s=300,
        reconcile_liveness_threshold_s=300,
    )
    assert stub.get_system_state("reaper_task_status") == "running"


def test_stale_task_shows_degraded_status():
    now = datetime.now(UTC)
    stub = _StateLedger({
        "reaper_last_heartbeat_at": (now - timedelta(seconds=600)).isoformat(),
    })
    _check_background_task_liveness(
        ledger=stub,
        reaper_liveness_threshold_s=300,
        reconcile_liveness_threshold_s=300,
    )
    assert stub.get_system_state("reaper_task_status") == "degraded"


def _run_task_to_done(coro) -> asyncio.Task:
    """Run an async coroutine to completion in a fresh loop and return the done Task."""
    loop = asyncio.new_event_loop()
    task = loop.create_task(coro)
    try:
        loop.run_until_complete(task)
    except (Exception, asyncio.CancelledError):
        pass
    finally:
        loop.close()
    return task


def test_done_task_handle_sets_completed_unexpectedly():
    """A task handle that has exited with an exception sets status to completed_unexpectedly."""
    async def _crash():
        raise RuntimeError("boom")

    task = _run_task_to_done(_crash())
    stub = _StateLedger()
    _check_background_task_liveness(
        ledger=stub,
        reaper_liveness_threshold_s=300,
        reconcile_liveness_threshold_s=300,
        task_handles={"reaper": task},
    )
    assert stub.get_system_state("reaper_task_status") == "completed_unexpectedly"
    assert stub.get_system_state("reaper_last_error_type") == "RuntimeError"


def test_cancelled_task_handle_sets_completed_unexpectedly():
    """A cancelled task handle sets status to completed_unexpectedly."""
    async def _forever():
        await asyncio.sleep(9999)

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(_forever())
        task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
    finally:
        loop.close()

    stub = _StateLedger()
    _check_background_task_liveness(
        ledger=stub,
        reaper_liveness_threshold_s=300,
        reconcile_liveness_threshold_s=300,
        task_handles={"reaper": task},
    )
    assert stub.get_system_state("reaper_task_status") == "completed_unexpectedly"


@pytest.mark.asyncio
async def test_heartbeat_loop_respects_not_applicable_task():
    """not_applicable tasks must not be overwritten to running/degraded by the heartbeat loop."""
    now = datetime.now(UTC)
    stub = _StateLedger({
        "classifier_task_status": "not_applicable",
        "classifier_last_heartbeat_at": (now - timedelta(seconds=10)).isoformat(),
    })
    stub._hb_returns = False  # exits after first tick

    task = asyncio.create_task(
        run_capture_service_heartbeat(
            ledger=stub,
            instance_id=_INSTANCE_A,
            interval_seconds=0,
            classifier_liveness_threshold_s=300,
        )
    )
    await task
    assert stub.get_system_state("classifier_task_status") == "not_applicable"
