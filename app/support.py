from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

SENSITIVE_ZFS_PROPERTIES = {"keylocation", "keystatus"}

_lock_guard = threading.Lock()
_bundle_locks: dict[int, threading.Lock] = {}


@contextmanager
def support_bundle_lock(server_id: int) -> Iterator[None]:
    with _lock_guard:
        lock = _bundle_locks.setdefault(server_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("A support bundle is already being generated for this server.")
    try:
        yield
    finally:
        # Retain the per-server lock object. Deleting it after release can
        # race with a thread that already fetched the old lock and allow a
        # second lock object for the same server to be acquired concurrently.
        lock.release()


def _sanitize_property_output(text: str) -> str:
    output: list[str] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            prop = parts[1].strip()
            if prop in SENSITIVE_ZFS_PROPERTIES:
                parts[2] = "<redacted>"
            elif ":" in prop:
                parts[2] = "<redacted:user-property>"
            line = "\t".join(parts)
        output.append(line)
    return "\n".join(output)


def sanitize_diagnostics(raw: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and {"stdout", "stderr", "exit"} <= set(value):
            item = dict(value)
            if key in {"zfs_get_all", "zpool_get_all"}:
                item["stdout"] = _sanitize_property_output(str(item.get("stdout", "")))
            safe[key] = item
        elif isinstance(value, dict):
            safe[key] = sanitize_diagnostics(value)
        else:
            safe[key] = value
    return safe
