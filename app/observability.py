"""Read-only fleet diagnostics, physical labels and a bounded event timeline."""

from .devices import is_zvol

from datetime import datetime, timedelta
from pathlib import Path
import re
from urllib.parse import quote

from fastapi import Depends, Query, Request, HTTPException
from sqlalchemy import select, func, literal, union_all, text, case, and_, or_
from .db import engine
from .insights import session
from .models import (
    Server,
    DriveLabel,
    Alert,
    MaintenanceAction,
    MonitorEvent,
    MaintenanceWindow,
    Metric,
    MetricRollup,
    Enclosure,
    BayAssignment,
)
from .service import current_states
from .settings_store import get_int, get_setting
from .experience import server_state, smart_state


def server_filter(value):
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{1,18}", value) or int(value) < 1:
        raise HTTPException(400, "Choose a valid server ID.")
    return int(value)


def install(app, templates):
    @app.get("/diagnostics")
    def diagnostics(request: Request, db=Depends(session)):
        servers = db.scalars(select(Server).order_by(Server.name)).all()
        states = current_states(db, servers)
        from .scheduler import collector_states

        live = collector_states()
        rows = []
        now = datetime.utcnow()
        for s in servers:
            snapshot = states.get(s.id, {})
            due = (
                s.last_poll_at + timedelta(seconds=s.poll_interval_seconds)
                if s.last_poll_at
                else now
            )
            rows.append(
                {
                    "server": s,
                    "state": server_state(s, snapshot),
                    "collection": snapshot.get("collection", {}),
                    "due": due,
                    "overdue": s.enabled and due < now,
                    "worker": (
                        {"state": "overdue", "at": live.get(s.id, {}).get("at")}
                        if s.enabled
                        and due < now
                        and live.get(s.id, {}).get("state") not in {"queued", "running"}
                        else live.get(s.id, {"state": "idle"})
                    ),
                }
            )
        return templates.TemplateResponse(
            request=request, name="diagnostics.html", context={"rows": rows}
        )

    @app.get("/drive-bays")
    def bays(request: Request, server: str = "", db=Depends(session)):
        server = server_filter(server)
        servers = db.scalars(select(Server).order_by(Server.name)).all()
        selected = [s for s in servers if server is None or s.id == server]
        states = current_states(db, selected)
        labels = {
            (x.server_id, x.identity): x.label
            for x in db.scalars(
                select(DriveLabel).where(
                    DriveLabel.server_id.in_([s.id for s in selected])
                )
            )
        }
        groups = []
        for s in selected:
            disks = []
            for d in states.get(s.id, {}).get("drives", []):
                identity = d.get("serial") or d.get("path") or ""
                disks.append(
                    {
                        "disk": d,
                        "identity": identity,
                        "label": "ZFS virtual volume (zvol)" if is_zvol(d) else (labels.get((s.id, identity), "") or "Unlabeled"),
                        "health": ("unknown", "SMART not applicable") if is_zvol(d) else smart_state(d.get("smart")),
                        "url": f"/servers/{s.id}/drive?identity="
                        + quote(identity, safe=""),
                    }
                )
            groups.append(
                {
                    "server": s,
                    "disks": disks,
                    "state": server_state(s, states.get(s.id)),
                }
            )
        enclosures = db.scalars(
            select(Enclosure)
            .where(Enclosure.server_id.in_([s.id for s in selected]))
            .order_by(Enclosure.name)
        ).all()
        assignments = {
            (a.enclosure_id, a.slot): a.identity
            for a in db.scalars(
                select(BayAssignment).where(
                    BayAssignment.enclosure_id.in_([e.id for e in enclosures])
                )
            )
        }
        for group in groups:
            disks = {d["identity"]: d for d in group["disks"]}
            group["enclosures"] = []
            for enclosure in enclosures:
                if enclosure.server_id != group["server"].id:
                    continue
                slots = []
                for number in range(1, enclosure.rows * enclosure.columns + 1):
                    identity = assignments.get((enclosure.id, number), "")
                    if identity in disks:
                        drive = disks[identity]
                        drive["location"] = f"{enclosure.name} · Bay {number}"
                        if drive["label"] == "Unlabeled":
                            drive["label"] = drive["location"]
                    slots.append(
                        {
                            "number": number,
                            "identity": identity,
                            "drive": disks.get(identity),
                        }
                    )
                group["enclosures"].append({"config": enclosure, "slots": slots})
            group["available_disks"] = [
                d for d in group["disks"] if d["disk"].get("serial") and not is_zvol(d["disk"]) and not d.get("location")
            ]
            group["disks"].sort(
                key=lambda d: (
                    d["label"] == "Unlabeled",
                    tuple(
                        (0, int(part)) if part.isdigit() else (1, part.casefold())
                        for part in re.split(r"(\d+)", d["label"])
                    ),
                    d["identity"],
                )
            )
        return templates.TemplateResponse(
            request=request,
            name="drive_bays.html",
            context={"groups": groups, "servers": servers, "selected": server},
        )

    @app.get("/timeline")
    def timeline(
        request: Request,
        server: str = "",
        kind: str = "",
        before: str = Query("", max_length=160),
        db=Depends(session),
    ):
        server = server_filter(server)
        sources = [
            select(
                MonitorEvent.id.label("id"),
                MonitorEvent.server_id.label("server_id"),
                MonitorEvent.captured_at.label("at"),
                MonitorEvent.kind.label("kind"),
                MonitorEvent.severity.label("severity"),
                MonitorEvent.message.label("message"),
                literal("monitor").label("source"),
            ),
            select(
                Alert.id,
                Alert.server_id,
                Alert.first_seen,
                literal("alert"),
                Alert.severity,
                Alert.title,
                literal("alert"),
            ),
            select(
                Alert.id,
                Alert.server_id,
                Alert.resolved_at,
                literal("resolved"),
                literal("good"),
                Alert.title,
                literal("alert"),
            ).where(Alert.resolved_at.is_not(None)),
            select(
                MaintenanceAction.id,
                MaintenanceAction.server_id,
                MaintenanceAction.created_at,
                literal("replacement"),
                case((MaintenanceAction.success.is_(False), "critical"), else_="info"),
                MaintenanceAction.pool
                + case(
                    (MaintenanceAction.success.is_(False), ": command failed"),
                    else_=": command accepted",
                ),
                literal("action"),
            ),
            select(
                MaintenanceWindow.id,
                MaintenanceWindow.server_id,
                MaintenanceWindow.starts_at,
                literal("window"),
                literal("info"),
                MaintenanceWindow.reason,
                literal("window"),
            ),
        ]
        events = union_all(*sources).subquery()
        query = (
            select(events, Server.name.label("server_name"))
            .join(Server, Server.id == events.c.server_id)
            .where(events.c.at <= datetime.utcnow())
        )
        if server is not None:
            query = query.where(events.c.server_id == server)
        if kind in {"collection", "scan", "alert", "resolved", "replacement", "window"}:
            query = query.where(events.c.kind == kind)
        if before:
            try:
                at, event_kind, event_id, event_source = before.split("|")
                at = datetime.fromisoformat(at)
                event_id = int(event_id)
                if (
                    at.tzinfo is not None
                    or event_id < 1
                    or event_id > 9223372036854775807
                ):
                    raise ValueError()
            except (ValueError, TypeError):
                raise HTTPException(400, "Invalid timeline cursor.")
            query = query.where(
                or_(
                    events.c.at < at,
                    and_(events.c.at == at, events.c.kind > event_kind),
                    and_(
                        events.c.at == at,
                        events.c.kind == event_kind,
                        events.c.id < event_id,
                    ),
                    and_(
                        events.c.at == at,
                        events.c.kind == event_kind,
                        events.c.id == event_id,
                        events.c.source > event_source,
                    ),
                )
            )
        rows = db.execute(
            query.order_by(
                events.c.at.desc(), events.c.kind, events.c.id.desc(), events.c.source
            ).limit(51)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="timeline.html",
            context={
                "rows": rows[:50],
                "has_next": len(rows) > 50,
                "cursor": (
                    rows[49].at.isoformat()
                    + "|"
                    + rows[49].kind
                    + "|"
                    + str(rows[49].id)
                    + "|"
                    + rows[49].source
                )
                if len(rows) > 50
                else "",
                "kind": kind,
                "selected": server,
                "servers": db.scalars(select(Server).order_by(Server.name)).all(),
            },
        )

    @app.get("/settings/database")
    def database(request: Request, db=Depends(session)):
        # Exact counts are opt-in; normal page loads never scan the history table.
        now = datetime.utcnow()
        stats = {
            "raw_days": get_int(db, "raw_history_days", 7),
            "hourly_days": get_int(db, "hourly_history_days", 30),
            "retention": get_int(db, "metric_retention_days", 90),
            "snapshot_retention": get_int(db, "snapshot_retention_days", 30),
            "housekeeping": get_setting(db, "history_housekeeping_at", "Not run yet"),
            "result": get_setting(
                db, "history_housekeeping_result", "No housekeeping result yet"
            ),
        }
        import math

        for key in ["state", "error", "seconds", "attempt"]:
            stats[key] = get_setting(db, "history_housekeeping_" + key, "Not recorded")
        oldest = db.scalar(select(func.min(Metric.captured_at)))
        oldest_hourly = db.scalar(
            select(func.min(MetricRollup.captured_at)).where(
                MetricRollup.resolution == 3600
            )
        )
        raw_windows = (
            max(
                0,
                math.ceil(
                    ((now - timedelta(days=stats["raw_days"])) - oldest).total_seconds()
                    / 3600
                ),
            )
            if oldest
            else 0
        )
        daily_windows = (
            max(
                0,
                math.ceil(
                    (
                        (now - timedelta(days=stats["hourly_days"])) - oldest_hourly
                    ).total_seconds()
                    / 86400
                ),
            )
            if oldest_hourly
            else 0
        )
        stats["backlog"] = raw_windows + daily_windows
        stats["catchup"] = max(
            math.ceil(raw_windows / 48), math.ceil(daily_windows / 48)
        )
        if engine.dialect.name == "sqlite":
            stats["database_bytes"] = (
                db.execute(text("PRAGMA page_count")).scalar()
                * db.execute(text("PRAGMA page_size")).scalar()
            )
            stats["reusable_bytes"] = (
                db.execute(text("PRAGMA freelist_count")).scalar()
                * db.execute(text("PRAGMA page_size")).scalar()
            )
            path = Path(engine.url.database or "")
            wal = Path(str(path) + "-wal")
            stats["wal_bytes"] = wal.stat().st_size if wal.is_file() else 0
        if request.query_params.get("analyze") == "1":
            stats["raw_rows"] = db.scalar(select(func.count()).select_from(Metric))
            stats["rollup_rows"] = db.scalar(
                select(func.count()).select_from(MetricRollup)
            )
            stats["daily_rows"] = db.scalar(
                select(func.count())
                .select_from(Metric)
                .where(Metric.captured_at >= now - timedelta(days=1))
            )
        return templates.TemplateResponse(
            request=request, name="database.html", context={"stats": stats}
        )
