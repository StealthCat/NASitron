from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO

from .config import DATA_DIR


class InstanceLock:
    def __init__(self) -> None:
        self.path = Path(DATA_DIR) / "nasitron.instance.lock"
        self.handle: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(
                "Another NASitron process is already using this data directory. "
                "Run exactly one application worker/replica."
            ) from exc
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
