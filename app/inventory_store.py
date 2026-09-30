"""Normalized snapshot inventory, refreshed only when detail telemetry changes."""

import json
from sqlalchemy import select, delete, insert
from .models import SnapshotInventory, InventoryStatus, CurrentState
from .experience import parse_time


def sync_inventory(db, server_id, inventory):
    status = db.get(InventoryStatus, server_id)
    if status is None:
        status = InventoryStatus(server_id=server_id)
        db.add(status)
    status.error = inventory.get("error")
    if not inventory.get("fresh") and status.captured_at:
        return
    stamp = str(inventory.get("captured_at") or "")
    if stamp and stamp == status.captured_at:
        return
    rows = {r["name"]: r for r in inventory.get("rows", []) if r.get("name")}
    db.execute(
        delete(SnapshotInventory).where(SnapshotInventory.server_id == server_id)
    )
    if rows:
        db.execute(
            insert(SnapshotInventory),
            [
                {
                    "server_id": server_id,
                    "name": r["name"],
                    "created_at": parse_time(r.get("created")),
                    "used": r.get("used") or 0,
                    "referenced": r.get("referenced") or 0,
                }
                for r in rows.values()
            ],
        )
    status.captured_at = stamp


def _backfill_inventory(db):
    # Only hosts missing a normalized inventory are decoded on upgrades.
    rows = db.scalars(
        select(CurrentState).where(
            ~CurrentState.server_id.in_(select(InventoryStatus.server_id))
        )
    ).all()
    for row in rows:
        try:
            inventory = json.loads(row.payload_json).get("snapshot_inventory", {})
            sync_inventory(db, row.server_id, inventory)
        except (ValueError, TypeError):
            continue
    if rows:
        db.commit()


def backfill_inventory(db):
    from .service import _db_write_lock

    with _db_write_lock:
        _backfill_inventory(db)
