from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .alerts import collection_failed, collection_recovered, evaluate_snapshot
from .collector import SSHCollector
from .metrics import store_metrics
from .experience import update_operations
from .models import CurrentState, Metric, Server, Snapshot
from .parser import build_snapshot
from .settings_store import get_int

_db_write_lock = threading.Lock()


def _pool_map(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not snapshot:
        return {}
    return {
        str(pool.get("name")): pool
        for pool in snapshot.get("pools", [])
        if pool.get("name")
    }


def _mark_smart_stale(smart: dict[str, Any]) -> dict[str, Any]:
    result = dict(smart)
    result["stale"] = True
    return result


def _merge_previous_subsystems(
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None,
) -> None:
    if not previous:
        return

    collection = snapshot.setdefault("collection", {})
    errors = {
        str(item.get("subsystem"))
        for item in collection.get("errors", [])
        if item.get("subsystem")
    }
    stale = set(collection.get("stale_subsystems", []))
    freshness = dict(previous.get("collection", {}).get("freshness", {}))
    freshness.update(collection.get("freshness", {}))
    if not snapshot.get("snapshot_inventory", {}).get("fresh"):
        inventory = dict(previous.get("snapshot_inventory", {}))
        inventory["fresh"] = False
        inventory["error"] = snapshot.get("snapshot_inventory", {}).get("error")
        snapshot["snapshot_inventory"] = inventory
    prev_system = previous.get("system", {})
    system = snapshot.setdefault("system", {})

    field_groups = {
        "system.hostname": ("hostname",),
        "system.os": ("os",),
        "system.kernel": ("kernel",),
        "system.uptime": ("uptime_seconds",),
        "system.load": ("load1", "load5", "load15"),
        "system.memory": ("memory",),
        "zfs.version": ("zfs_version",),
        "zfs.services": ("services",),
    }
    for subsystem, fields in field_groups.items():
        if subsystem in errors:
            for field in fields:
                if field in prev_system:
                    system[field] = prev_system[field]
            stale.add(subsystem)

    if "zfs.arc" in errors:
        snapshot["arc"] = previous.get("arc", snapshot.get("arc", {}))
        stale.add("zfs.arc")
    if "zfs.datasets" in errors:
        snapshot["datasets"] = previous.get("datasets", [])
        stale.add("zfs.datasets")

    prev_datasets = {
        str(dataset.get("name")): dataset
        for dataset in previous.get("datasets", [])
        if dataset.get("name")
    }
    if "zfs.dataset_properties" in errors:
        for dataset in snapshot.get("datasets", []):
            old = prev_datasets.get(str(dataset.get("name") or ""))
            if old:
                dataset["properties"] = old.get("properties", [])
        stale.add("zfs.dataset_properties")

    prev_pools = _pool_map(previous)
    for pool in snapshot.get("pools", []):
        name = str(pool.get("name") or "")
        old = prev_pools.get(name)
        if not old:
            continue
        status_key = f"pool.status:{name}"
        if status_key in errors:
            pool["status"] = old.get("status", pool.get("status", {}))
            stale.add(status_key)
        if "zfs.iostat" in errors:
            pool["io"] = old.get("io", pool.get("io", {}))
            stale.add("zfs.iostat")
        if "zfs.datasets" in errors:
            pool["compression_ratio"] = old.get(
                "compression_ratio",
                pool.get("compression_ratio"),
            )
        properties_key = f"pool.properties:{name}"
        if properties_key in errors:
            pool["properties"] = old.get("properties", [])
            stale.add(properties_key)

    prev_drives = previous.get("drives", [])
    if "drives.inventory" in errors:
        snapshot["drives"] = []
        for old in prev_drives:
            carried = dict(old)
            if old.get("smart"):
                carried["smart"] = _mark_smart_stale(old["smart"])
            snapshot["drives"].append(carried)
        stale.add("drives.inventory")
    else:
        old_by_path = {d.get("path"): d for d in prev_drives if d.get("path")}
        old_by_serial = {d.get("serial"): d for d in prev_drives if d.get("serial")}
        smart_sampled = bool(collection.get("smart_sampled"))
        for disk in snapshot.get("drives", []):
            old = old_by_serial.get(disk.get("serial")) if disk.get("serial") else None
            old = old or old_by_path.get(disk.get("path"))
            if not old or not old.get("smart"):
                continue

            current = disk.get("smart") or {}
            if not smart_sampled:
                disk["smart"] = _mark_smart_stale(old["smart"])
                continue
            if not current.get("data_available"):
                carried = _mark_smart_stale(old["smart"])
                carried["data_available"] = False
                carried["attempted_at"] = current.get("sampled_at")
                carried["command_exit"] = current.get("command_exit")
                carried["exit_findings"] = current.get("exit_findings", [])
                disk["smart"] = carried
                stale.add(f"smart:{disk.get('serial') or disk.get('path')}")

    collection["freshness"] = freshness
    collection["stale_subsystems"] = sorted(stale)


def _expected_pools(server: Server) -> set[str]:
    try:
        value = json.loads(server.expected_pools_json or "[]")
    except json.JSONDecodeError:
        value = []
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if isinstance(item, str) and item}


def _update_expected_pools(server: Server, snapshot: dict[str, Any]) -> None:
    current = {
        str(pool.get("name"))
        for pool in snapshot.get("pools", [])
        if pool.get("name")
    }
    expected = _expected_pools(server)
    expected.update(current)
    server.expected_pools_json = json.dumps(sorted(expected))
    collection = snapshot.setdefault("collection", {})
    collection["expected_pools"] = sorted(expected)
    collection["missing_pools"] = sorted(expected - current)


def collect_server(db: Session, server_id: int) -> dict[str, Any]:
    server = db.get(Server, server_id)
    if server is None:
        raise RuntimeError(f"Server {server_id} not found")

    now = datetime.utcnow()
    include_smart = (
        server.last_smart_at is None
        or now - server.last_smart_at
        >= timedelta(minutes=max(1, server.smart_interval_minutes))
    )
    previous = latest_snapshot(db, server.id)

    try:
        with SSHCollector(server) as collector:
            raw = collector.collect(include_smart=include_smart)
        snapshot = build_snapshot(raw, captured_at=now)
        _merge_previous_subsystems(snapshot, previous)
        _update_expected_pools(server, snapshot)

        capability = snapshot.get("capabilities", {}).get("zpool_status_json")
        if capability is not None:
            server.zpool_status_json_supported = bool(capability)

        payload = json.dumps(snapshot, separators=(",", ":"))
        with _db_write_lock:
            _write_current_state(db, server.id, now, payload)
            store_metrics(db, server.id, now, snapshot)

            snapshot_interval = max(
                1, min(1440, get_int(db, "full_snapshot_interval_minutes", 15))
            )
            full_snapshot_due = (
                server.last_full_snapshot_at is None
                or now - server.last_full_snapshot_at
                >= timedelta(minutes=snapshot_interval)
            )
            if full_snapshot_due:
                db.add(
                    Snapshot(
                        server_id=server.id,
                        captured_at=now,
                        payload_json=payload,
                    )
                )
                server.last_full_snapshot_at = now

            server.last_poll_at = now
            server.last_error = None
            server.consecutive_failures = 0
            server.last_collection_state = (
                "partial" if snapshot.get("collection", {}).get("partial") else "ok"
            )
            if server.last_collection_state == "ok":
                server.last_ok_at = now
            if include_smart and snapshot.get("collection", {}).get("smart_inventory_ok"):
                attempted = int(
                    snapshot.get("collection", {}).get("smart_attempted_count") or 0
                )
                if attempted > 0 or not snapshot.get("drives"):
                    server.last_smart_at = now

            collection_recovered(db, server)
            evaluate_snapshot(db, server, snapshot)
            update_operations(db, server, snapshot)
            db.commit()
        return snapshot
    except Exception as exc:
        db.rollback()
        with _db_write_lock:
            server = db.get(Server, server_id)
            if server is None:
                raise
            server.last_poll_at = now
            server.last_error = str(exc)
            server.last_collection_state = "failed"
            server.consecutive_failures = (server.consecutive_failures or 0) + 1
            collection_failed(db, server, str(exc))
            db.commit()
        raise


def _write_current_state(
    db: Session,
    server_id: int,
    captured_at: datetime,
    payload_json: str,
) -> None:
    row = db.get(CurrentState, server_id)
    if row is None:
        db.add(
            CurrentState(
                server_id=server_id,
                captured_at=captured_at,
                payload_json=payload_json,
            )
        )
    else:
        row.captured_at = captured_at
        row.payload_json = payload_json


def latest_snapshot(db: Session, server_id: int) -> dict[str, Any] | None:
    row = db.get(CurrentState, server_id)
    if row is None:
        return None
    try:
        return json.loads(row.payload_json)
    except json.JSONDecodeError:
        return None


def latest_snapshot_row(db: Session, server_id: int) -> CurrentState | None:
    return db.get(CurrentState, server_id)


def prune_history(db: Session, now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.utcnow()
    metric_days = max(1, min(3650, get_int(db, "metric_retention_days", 90)))
    snapshot_days = max(1, min(3650, get_int(db, "snapshot_retention_days", 30)))
    with _db_write_lock:
        metric_result = db.execute(
            delete(Metric).where(Metric.captured_at < now - timedelta(days=metric_days))
        )
        snapshot_result = db.execute(
            delete(Snapshot).where(
                Snapshot.captured_at < now - timedelta(days=snapshot_days)
            )
        )
        db.commit()
    return int(metric_result.rowcount or 0), int(snapshot_result.rowcount or 0)
