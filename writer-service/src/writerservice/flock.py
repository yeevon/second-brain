from __future__ import annotations

import contextlib
import fcntl
import pathlib
from typing import Generator


@contextlib.contextmanager
def vault_write_lock(lock_path: pathlib.Path) -> Generator[None, None, None]:
    lock_path.touch(exist_ok=True)
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
