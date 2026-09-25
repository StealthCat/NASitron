from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .alerts import collection_failed, collection_recovered, evaluate_snapshot
from .collector import SSHCollector
from .metrics import store_metrics
from .models import Metric, Server, Snapshot
from .parser import build_snapshot
from .settings_store import get_int


def _merge_previous_smart(snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if not previous:
        return
    old_by_path = {d.get("path"): d for d in previous.get("drives", []) if d.get("path")}
    old_by_serial = {d.get("serial"): d for d in previous.get("drives", []) if d.get("serial")}
    for disk in snapshot.get("drives", []):
        if disk.get("smart"):
            continue
        old = old_by_serial.get(disk.get("serial")) if disk.get("serial") else None
        old = old or old_by_path.get(disk.get("path"))
        if old and old.get("smart"):
            disk["smart"] = old["smart"]


def collect_server(db: Session, server_id: int) -> dict[str, Any]:
    server = db.get(Server, server_id)
    if server is None:
        raise RuntimeError(f"Server {server_id} not found")

    now = datetime.utcnow()
    server.last_poll_at = now
    include_smart = (
        server.last_smart_at is None
        or now - server.last_smart_at >= timedelta(minutes=max(1, server.smart_interval_minutes))
    )
    previous = latest_snapshot(db, server.id)
    try:
        with SSHCollector(server) as collector:
            raw = collector.collect(include_smart=include_smart)
        snapshot = build_snapshot(raw)
        _merge_previous_smart(snapshot, previous)
        db.add(Snapshot(server_id=server.id, captured_at=now, payload_json=json.dumps(snapshot, separators=(",", ":"))))
        store_metrics(db, server.id, now, snapshot)
        server.last_ok_at = now
        server.last_error = None
        server.consecutive_failures = 0
        if include_smart:
            server.last_smart_at = now
        collection_recovered(db, server)
        evaluate_snapshot(db, server, snapshot)
        _prune(db, now)
        db.commit()
        return snapshot
    except Exception as exc:
        server.last_error = str(exc)
        server.consecutive_failures = (server.consecutive_failures or 0) + 1
        collection_failed(db, server, str(exc))
        db.commit()
        raise


def latest_snapshot(db: Session, server_id: int) -> dict[str, Any] | None:
    row = db.scalar(
        select(Snapshot).where(Snapshot.server_id == server_id).order_by(Snapshot.captured_at.desc()).limit(1)
    )
    return json.loads(row.payload_json) if row else None


def latest_snapshot_row(db: Session, server_id: int) -> Snapshot | None:
    return db.scalar(
        select(Snapshot).where(Snapshot.server_id == server_id).order_by(Snapshot.captured_at.desc()).limit(1)
    )


def _prune(db: Session, now: datetime) -> None:
    metric_days = max(1, get_int(db, "metric_retention_days", 90))
    snapshot_days = max(1, get_int(db, "snapshot_retention_days", 7))
    db.execute(delete(Metric).where(Metric.captured_at < now - timedelta(days=metric_days)))
    db.execute(delete(Snapshot).where(Snapshot.captured_at < now - timedelta(days=snapshot_days)))
