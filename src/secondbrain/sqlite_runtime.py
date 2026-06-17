from __future__ import annotations

import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from secondbrain.migrations import run_migrations
from secondbrain.observability import log_metadata


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
    operation_name: str = ""
    enqueued_at: float = 0.0


def _is_transient_lock_error(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code is not None:
        primary_code = error_code & 0xFF
        return primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    message = str(exc).lower()
    return any(
        fragment in message
        for fragment in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
        )
    )


def _set_wal_mode(
    conn: sqlite3.Connection,
    *,
    retry_base_delay_ms: int,
    retry_attempts: int,
) -> None:
    # PRAGMA journal_mode = WAL on a fresh database does not respect busy_timeout
    # (SQLite's WAL-file setup bypasses the busy handler). Retry explicitly.
    delay_ms = retry_base_delay_ms
    total_attempts = max(1, retry_attempts)
    for attempt in range(total_attempts):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if _is_transient_lock_error(exc) and attempt + 1 < total_attempts:
                time.sleep(delay_ms / 1000.0)
                delay_ms *= 2
                continue
            raise


DEFAULT_STARTUP_TIMEOUT_S = 10
DEFAULT_SHUTDOWN_TIMEOUT_S = 10


class SQLiteRuntime:
    def __init__(
        self,
        database_path,
        *,
        busy_timeout_ms: int = 1000,
        retry_attempts: int = 5,
        retry_base_delay_ms: int = 25,
        job_queue_maxsize: int = 10000,
        startup_timeout_s: int = DEFAULT_STARTUP_TIMEOUT_S,
        shutdown_timeout_s: int = DEFAULT_SHUTDOWN_TIMEOUT_S,
    ) -> None:
        # TD-007: defensive constructor validation
        if database_path is None or str(database_path).strip() == "":
            raise ValueError("database_path must not be empty or None")
        if job_queue_maxsize <= 0:
            raise ValueError(f"job_queue_maxsize must be positive, got {job_queue_maxsize}")
        if retry_attempts < 0:
            raise ValueError(f"retry_attempts must not be negative, got {retry_attempts}")

        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError(
                f"SQLite {sqlite3.sqlite_version} is installed; "
                f"minimum required is {'.'.join(str(v) for v in MIN_SQLITE_VERSION)}"
            )

        self._database_path = str(database_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._retry_attempts = retry_attempts
        self._retry_base_delay_ms = retry_base_delay_ms
        self._startup_timeout_s = startup_timeout_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._closed = False
        self._close_lock = threading.Lock()
        self._queue: queue.Queue[_DatabaseJob | object] = queue.Queue(maxsize=job_queue_maxsize)

        log_metadata(
            "sqlite_runtime_init",
            path=self._database_path,
            queue_maxsize=job_queue_maxsize,
            retry_attempts=retry_attempts,
            busy_timeout_ms=busy_timeout_ms,
        )

        self._started = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread = threading.Thread(target=self._worker, daemon=True, name="sqlite-worker")
        self._thread.start()

        # TD-004: bounded startup timeout
        if not self._started.wait(timeout=startup_timeout_s):
            log_metadata("sqlite_runtime_startup_timeout", timeout_s=startup_timeout_s)
            raise RuntimeError(
                f"SQLite runtime did not start within {startup_timeout_s}s"
            )
        if self._startup_error is not None:
            raise RuntimeError(f"SQLite runtime failed to start: {self._startup_error}") from self._startup_error

    def read(self, operation: Callable[[sqlite3.Connection], T], *, operation_name: str = "") -> T:
        return self._submit(operation, write=False, operation_name=operation_name)

    def write(self, operation: Callable[[sqlite3.Connection], T], *, operation_name: str = "") -> T:
        return self._submit(operation, write=True, operation_name=operation_name)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(_SENTINEL)
        # TD-004: bounded shutdown timeout
        self._thread.join(timeout=self._shutdown_timeout_s)
        if self._thread.is_alive():
            remaining = self._queue.qsize()
            log_metadata(
                "sqlite_runtime_shutdown_timeout",
                timeout_s=self._shutdown_timeout_s,
                remaining_jobs=remaining,
            )

    def _submit(
        self,
        operation: Callable[[sqlite3.Connection], Any],
        *,
        write: bool,
        operation_name: str = "",
    ) -> Any:
        future: Future = Future()
        enqueued_at = time.monotonic()
        job = _DatabaseJob(
            operation=operation,
            future=future,
            write=write,
            operation_name=operation_name,
            enqueued_at=enqueued_at,
        )
        with self._close_lock:
            if self._closed:
                raise RuntimeError("SQLite runtime is closed")
            queue_depth = self._queue.qsize()
            self._queue.put(job)
        log_metadata(
            "sqlite_queue_depth",
            operation_name=operation_name,
            depth=queue_depth,
        )
        return future.result()

    def _worker(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self._database_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            _set_wal_mode(conn, retry_base_delay_ms=self._retry_base_delay_ms, retry_attempts=self._retry_attempts)
            conn.execute("PRAGMA foreign_keys = ON")

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
            if conn is not None:
                conn.close()
            self._startup_error = exc
            self._started.set()
            return

        self._started.set()
        log_metadata("sqlite_runtime_started", path=self._database_path)

        while True:
            job = self._queue.get()
            if job is _SENTINEL:
                break
            assert isinstance(job, _DatabaseJob)
            dequeued_at = time.monotonic()
            wait_ms = int((dequeued_at - job.enqueued_at) * 1000) if job.enqueued_at else 0
            log_metadata(
                "sqlite_queue_wait_ms",
                operation_name=job.operation_name,
                wait_ms=wait_ms,
            )
            job_start = time.monotonic()
            try:
                result = self._execute_with_retry(conn, job)
                job.future.set_result(result)
            except BaseException as exc:
                job.future.set_exception(exc)
            finally:
                duration_ms = int((time.monotonic() - job_start) * 1000)
                log_metadata(
                    "sqlite_job_duration_ms",
                    operation_name=job.operation_name,
                    duration_ms=duration_ms,
                )

        conn.close()
        log_metadata("sqlite_runtime_stopped", path=self._database_path)

    def _execute_with_retry(self, conn: sqlite3.Connection, job: _DatabaseJob) -> Any:
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
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = job.operation(conn)
                        conn.commit()
                        return result
                    except BaseException:
                        conn.rollback()
                        raise
                else:
                    return job.operation(conn)
            except sqlite3.OperationalError as exc:
                if _is_transient_lock_error(exc):
                    last_exc = exc
                    retrying = attempt + 1 < len(delays_ms)
                    log_metadata(
                        "sqlite_busy_retry",
                        operation_name=job.operation_name,
                        attempt=attempt + 1,
                        error_type=type(exc).__name__,
                        retrying=retrying,
                    )
                    continue
                raise

        log_metadata(
            "sqlite_busy_exhausted",
            operation_name=job.operation_name,
            attempts=len(delays_ms),
            error_type=type(last_exc).__name__ if last_exc else "Unknown",
        )
        raise SQLiteBusyError(
            f"SQLite operation failed after {len(delays_ms)} attempt(s) due to lock contention"
        ) from last_exc
