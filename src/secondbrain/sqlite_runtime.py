from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from secondbrain.migrations import run_migrations


logger = logging.getLogger(__name__)

MIN_SQLITE_VERSION = (3, 24, 0)

T = TypeVar("T")

_SENTINEL = object()


class SQLiteBusyError(RuntimeError):
    pass


@dataclass
class _DatabaseJob:
    operation: Callable[[sqlite3.Connection], Any]
    future: Future
    write: bool


class SQLiteRuntime:
    def __init__(
        self,
        database_path,
        *,
        busy_timeout_ms: int = 1000,
        retry_attempts: int = 5,
        retry_base_delay_ms: int = 25,
        job_queue_maxsize: int = 10000,
    ) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError(
                f"SQLite {sqlite3.sqlite_version} is installed; "
                f"minimum required is {'.'.join(str(v) for v in MIN_SQLITE_VERSION)}"
            )

        self._database_path = str(database_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._retry_attempts = retry_attempts
        self._retry_base_delay_ms = retry_base_delay_ms
        self._closed = False
        self._close_lock = threading.Lock()
        self._queue: queue.Queue[_DatabaseJob | object] = queue.Queue(maxsize=job_queue_maxsize)

        self._started = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread = threading.Thread(target=self._worker, daemon=True, name="sqlite-worker")
        self._thread.start()

        self._started.wait()
        if self._startup_error is not None:
            raise RuntimeError(f"SQLite runtime failed to start: {self._startup_error}") from self._startup_error

    def read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return self._submit(operation, write=False)

    def write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return self._submit(operation, write=True)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(_SENTINEL)
        self._thread.join()

    def _submit(self, operation: Callable[[sqlite3.Connection], Any], *, write: bool) -> Any:
        with self._close_lock:
            if self._closed:
                raise RuntimeError("SQLite runtime is closed")
            future: Future = Future()
            job = _DatabaseJob(operation=operation, future=future, write=write)

        # Put outside the lock so a full queue doesn't deadlock close()
        self._queue.put(job)
        return future.result()

    def _worker(self) -> None:
        try:
            conn = sqlite3.connect(self._database_path)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")

            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if journal_mode != "wal":
                raise RuntimeError(
                    f"SQLite journal_mode is '{journal_mode}', expected 'wal'"
                )
            foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            if foreign_keys != 1:
                raise RuntimeError(
                    f"SQLite foreign_keys is {foreign_keys}, expected 1"
                )
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            if busy_timeout != self._busy_timeout_ms:
                raise RuntimeError(
                    f"SQLite busy_timeout is {busy_timeout}, expected {self._busy_timeout_ms}"
                )

            run_migrations(conn)

        except BaseException as exc:
            self._startup_error = exc
            self._started.set()
            return

        self._started.set()
        logger.info(
            "sqlite_runtime_started",
            extra={"event": "sqlite_runtime_started", "path": self._database_path},
        )

        while True:
            job = self._queue.get()
            if job is _SENTINEL:
                break
            assert isinstance(job, _DatabaseJob)
            try:
                result = self._execute_with_retry(conn, job)
                job.future.set_result(result)
            except BaseException as exc:
                job.future.set_exception(exc)

        conn.close()
        logger.info("sqlite_runtime_stopped", extra={"event": "sqlite_runtime_stopped"})

    def _execute_with_retry(self, conn: sqlite3.Connection, job: _DatabaseJob) -> Any:
        # Build per-attempt wait schedule: [0, base, base*2, base*4, ...]
        delays_ms: list[int] = [0]
        base = self._retry_base_delay_ms
        for i in range(1, self._retry_attempts):
            delays_ms.append(base * (2 ** (i - 1)))

        last_exc: Exception | None = None
        for attempt, delay_ms in enumerate(delays_ms):
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
            try:
                if job.write:
                    with conn:
                        return job.operation(conn)
                else:
                    return job.operation(conn)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "database is locked" in msg or "database table is locked" in msg:
                    last_exc = exc
                    retrying = attempt + 1 < len(delays_ms)
                    logger.warning(
                        "sqlite_busy_retry",
                        extra={
                            "event": "sqlite_busy_retry",
                            "attempt": attempt + 1,
                            "error_type": type(exc).__name__,
                            "retrying": retrying,
                        },
                    )
                    continue
                raise

        logger.error(
            "sqlite_busy_exhausted",
            extra={
                "event": "sqlite_busy_exhausted",
                "attempts": len(delays_ms),
                "error_type": type(last_exc).__name__ if last_exc else "Unknown",
            },
        )
        raise SQLiteBusyError(
            f"SQLite operation failed after {len(delays_ms)} attempt(s) due to lock contention"
        ) from last_exc
