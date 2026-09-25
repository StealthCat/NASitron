from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .models import Metric

METRIC_NAMES = {
    "system.load1",
    "system.memory_used_pct",
    "system.memory_used_bytes",
    "arc.hit_rate_pct",
    "arc.size_bytes",
    "arc.target_bytes",
    "arc.l2_hit_rate_pct",
    "arc.l2_size_bytes",
    "pool.capacity_pct",
    "pool.alloc_bytes",
    "pool.free_bytes",
    "pool.fragmentation_pct",
    "pool.read_iops",
    "pool.write_iops",
    "pool.read_bps",
    "pool.write_bps",
    "drive.temperature_c",
    "drive.smart_passed",
}


def _add(db: Session, server_id: int, captured_at: datetime, name: str, value: Any, scope: str = "") -> None:
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    db.add(Metric(server_id=server_id, captured_at=captured_at, name=name, scope=scope, value=number))


def store_metrics(db: Session, server_id: int, captured_at: datetime, snapshot: dict[str, Any]) -> None:
    system = snapshot.get("system", {})
    memory = system.get("memory", {})
    arc = snapshot.get("arc", {})
    errors = {
        item.get("subsystem")
        for item in snapshot.get("collection", {}).get("errors", [])
    }

    if "system.load" not in errors:
        _add(db, server_id, captured_at, "system.load1", system.get("load1"))
    if "system.memory" not in errors:
        _add(db, server_id, captured_at, "system.memory_used_pct", memory.get("used_pct"))
        _add(db, server_id, captured_at, "system.memory_used_bytes", memory.get("used_bytes"))

    if "zfs.arc" not in errors:
        _add(db, server_id, captured_at, "arc.hit_rate_pct", arc.get("hit_rate_pct"))
        _add(db, server_id, captured_at, "arc.size_bytes", arc.get("size_bytes"))
        _add(db, server_id, captured_at, "arc.target_bytes", arc.get("target_bytes"))
        _add(db, server_id, captured_at, "arc.l2_hit_rate_pct", arc.get("l2_hit_rate_pct"))
        _add(db, server_id, captured_at, "arc.l2_size_bytes", arc.get("l2_size_bytes"))

    io_ok = "zfs.iostat" not in errors
    for pool in snapshot.get("pools", []):
        scope = pool.get("name", "")
        _add(db, server_id, captured_at, "pool.capacity_pct", pool.get("capacity_pct"), scope)
        _add(db, server_id, captured_at, "pool.alloc_bytes", pool.get("alloc_bytes"), scope)
        _add(db, server_id, captured_at, "pool.free_bytes", pool.get("free_bytes"), scope)
        _add(db, server_id, captured_at, "pool.fragmentation_pct", pool.get("fragmentation_pct"), scope)
        if io_ok:
            io = pool.get("io", {})
            for key in ("read_iops", "write_iops", "read_bps", "write_bps"):
                _add(db, server_id, captured_at, f"pool.{key}", io.get(key), scope)

    for disk in snapshot.get("drives", []):
        smart = disk.get("smart") or {}
        # SMART data is sampled on a slower cadence. Do not duplicate carried
        # values into every one-minute metric interval.
        if smart.get("stale") or not smart.get("data_available"):
            continue
        scope = disk.get("serial") or disk.get("path") or disk.get("name") or ""
        _add(db, server_id, captured_at, "drive.temperature_c", smart.get("temperature_c"), scope)
        if smart.get("smart_passed") is not None:
            _add(db, server_id, captured_at, "drive.smart_passed", 1 if smart.get("smart_passed") else 0, scope)
