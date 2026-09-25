from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .alerts import collection_failed, collection_recovered, evaluate_snapshot
from .collector import SSHCollector
from .metrics import store_metrics
from .models import CurrentState, Metric, Server, Snapshot
from .parser import build_snapshot
from .settings_store import get_int


def _merge_previous_smart(snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if snapshot.get("collection", {}).get("smart_sampled"):
        return
    if not previous:
        return

    old_by_path = {d.get("path"): d for d in previous.get("drives", []) if d.get("path")}
    old_by_serial = {d.get("serial"): d for d in previous.get("drives", []) if d.get("serial")}
    for disk in snapshot.get("drives", []):
        old = old_by_serial.get(disk.get("serial")) if disk.get("serial") else None
        old = old or old_by_path.get(disk.get("path"))
        if old and old.get("smart"):
            carried = dict(old["smart"])
            carried["stale"] = True
            disk["smart"] = carried


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
        _merge_previous_smart(snapshot, previous)

        payload = json.dumps(snapshot, separators=(",", ":"))
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
        if include_smart:
            # Mark the attempt time even when one drive fails to answer so a bad
            # device is not hammered continuously on every lightweight poll.
            server.last_smart_at = now

        collection_recovered(db, server)
        evaluate_snapshot(db, server, snapshot)
        db.commit()
        return snapshot
    except Exception as exc:
        db.rollback()
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
