from __future__ import annotations

from datetime import datetime
import math
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from .models import Metric
from .disk_io import IO_KEYS

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
    "drive.reallocated_sectors",
    "drive.pending_sectors",
    "drive.media_errors",
    "drive.percentage_used",
}

METRIC_NAMES.update(f"drive.io.{key}" for key in IO_KEYS)


def _row(
    rows: list[dict[str, Any]],
    server_id: int,
    captured_at: datetime,
    name: str,
    value: Any,
    scope: str = "",
) -> None:
    if value is None:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(number):
        return
    rows.append(
        {
            "server_id": server_id,
            "captured_at": captured_at,
            "name": name,
            "scope": scope,
            "value": number,
        }
    )


def store_metrics(
    db: Session,
    server_id: int,
    captured_at: datetime,
    snapshot: dict[str, Any],
) -> None:
    rows: list[dict[str, Any]] = []
    system = snapshot.get("system", {})
    memory = system.get("memory", {})
    arc = snapshot.get("arc", {})
    stale = set(snapshot.get("collection", {}).get("stale_subsystems", []))

    if "system.load" not in stale:
        _row(rows, server_id, captured_at, "system.load1", system.get("load1"))
    if "system.memory" not in stale:
        _row(rows, server_id, captured_at, "system.memory_used_pct", memory.get("used_pct"))
        _row(rows, server_id, captured_at, "system.memory_used_bytes", memory.get("used_bytes"))

    if "zfs.arc" not in stale:
        _row(rows, server_id, captured_at, "arc.hit_rate_pct", arc.get("hit_rate_pct"))
        _row(rows, server_id, captured_at, "arc.size_bytes", arc.get("size_bytes"))
        _row(rows, server_id, captured_at, "arc.target_bytes", arc.get("target_bytes"))
        _row(rows, server_id, captured_at, "arc.l2_hit_rate_pct", arc.get("l2_hit_rate_pct"))
        _row(rows, server_id, captured_at, "arc.l2_size_bytes", arc.get("l2_size_bytes"))

    io_stale = "zfs.iostat" in stale
    for pool in snapshot.get("pools", []):
        scope = pool.get("name", "")
        _row(rows, server_id, captured_at, "pool.capacity_pct", pool.get("capacity_pct"), scope)
        _row(rows, server_id, captured_at, "pool.alloc_bytes", pool.get("alloc_bytes"), scope)
        _row(rows, server_id, captured_at, "pool.free_bytes", pool.get("free_bytes"), scope)
        _row(rows, server_id, captured_at, "pool.fragmentation_pct", pool.get("fragmentation_pct"), scope)
        if not io_stale:
            io = pool.get("io", {})
            for key in ("read_iops", "write_iops", "read_bps", "write_bps"):
                _row(rows, server_id, captured_at, f"pool.{key}", io.get(key), scope)

    for disk in snapshot.get("drives", []):
        scope = disk.get("serial") or disk.get("path") or disk.get("name") or ""
        if not {"drives.io", "drives.inventory"}.intersection(stale):
            for key in IO_KEYS:
                _row(rows, server_id, captured_at, f"drive.io.{key}",
                     (disk.get("io") or {}).get(key), scope)
        smart = disk.get("smart") or {}
        if smart.get("stale") or not smart.get("data_available"):
            continue
        scope = disk.get("serial") or disk.get("path") or disk.get("name") or ""
        _row(rows, server_id, captured_at, "drive.temperature_c", smart.get("temperature_c"), scope)
        for indicator in ("reallocated_sectors", "pending_sectors", "media_errors", "percentage_used"):
            _row(rows, server_id, captured_at, f"drive.{indicator}", smart.get(indicator), scope)
        if smart.get("smart_passed") is not None:
            _row(
                rows,
                server_id,
                captured_at,
                "drive.smart_passed",
                1 if smart.get("smart_passed") else 0,
                scope,
            )

    if rows:
        db.execute(insert(Metric), rows)
