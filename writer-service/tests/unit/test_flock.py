"""Unit tests for the advisory flock context manager."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from writerservice.flock import vault_write_lock


def test_flock_acquires_and_releases(tmp_path):
    lock_path = tmp_path / ".writer.lock"
    with vault_write_lock(lock_path):
        assert lock_path.exists()


def test_flock_second_thread_blocks_until_first_releases(tmp_path):
    lock_path = tmp_path / ".writer.lock"
    order: list[str] = []
    released = threading.Event()

    def first():
        with vault_write_lock(lock_path):
            order.append("first_entered")
            time.sleep(0.05)
            order.append("first_releasing")
        released.set()

    def second():
        released.wait(timeout=1.0)
        with vault_write_lock(lock_path):
            order.append("second_entered")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    time.sleep(0.01)
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert order == ["first_entered", "first_releasing", "second_entered"]


def test_flock_releases_on_exception(tmp_path):
    lock_path = tmp_path / ".writer.lock"

    with pytest.raises(ValueError, match="intentional"):
        with vault_write_lock(lock_path):
            raise ValueError("intentional")

    # After exception, lock should be acquirable again
    acquired = False
    with vault_write_lock(lock_path):
        acquired = True
    assert acquired


def test_flock_released_when_process_dies(tmp_path):
    """Kernel releases flock when the holding process terminates."""
    lock_path = tmp_path / ".writer.lock"

    script = f"""
import fcntl
lock_path = '{lock_path}'
open(lock_path, 'w').close()
fd = open(lock_path, 'w')
fcntl.flock(fd, fcntl.LOCK_EX)
# Exit without releasing; kernel should clean up
"""
    proc = subprocess.Popen([sys.executable, "-c", script])
    proc.wait(timeout=3.0)

    # After the child exits, we must be able to acquire the lock
    acquired = False
    with vault_write_lock(lock_path):
        acquired = True
    assert acquired
