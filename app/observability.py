"""Read-only fleet diagnostics, physical labels and a bounded event timeline."""

from datetime import datetime, timedelta
from pathlib import Path
import re
from urllib.parse import quote

from fastapi import Depends, Query, Request
from sqlalchemy import select, func, literal, union_all, text
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
)
from .service import current_states
from .settings_store import get_int, get_setting
from .experience import server_state, smart_state


def install(app, templates):
    @app.get("/diagnostics")
    def diagnostics(request: Request, db=Depends(session)):
        servers = db.scalars(select(Server).order_by(Server.name)).all()
        states = current_states(db, servers)
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
                }
            )
        return templates.TemplateResponse(
            request=request, name="diagnostics.html", context={"rows": rows}
        )

    @app.get("/drive-bays")
    def bays(request: Request, server: str = "", db=Depends(session)):
        server = int(server) if server.isdigit() else None
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
                        "label": labels.get((s.id, identity), "") or "Unlabeled",
                        "health": smart_state(d.get("smart")),
                        "url": f"/servers/{s.id}/drive?identity="
                        + quote(identity, safe=""),
                    }
                )
            disks.sort(
                key=lambda d: (
                    d["label"] == "Unlabeled",
                    tuple(
                        (0, int(part)) if part.isdigit() else (1, part.casefold())
                        for part in re.split(r"(\d+)", d["label"])
                    ),
                    d["identity"],
                )
            )
            groups.append(
                {
                    "server": s,
                    "disks": disks,
                    "state": server_state(s, states.get(s.id)),
                }
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
        page: int = Query(1, ge=1, le=100000),
        db=Depends(session),
    ):
        server = int(server) if server.isdigit() else None
        sources = [
            select(
                MonitorEvent.id.label("id"),
                MonitorEvent.server_id.label("server_id"),
                MonitorEvent.captured_at.label("at"),
                MonitorEvent.kind.label("kind"),
                MonitorEvent.severity.label("severity"),
                MonitorEvent.message.label("message"),
            ),
            select(
                Alert.id,
                Alert.server_id,
                Alert.first_seen,
                literal("alert"),
                Alert.severity,
                Alert.title,
            ),
            select(
                Alert.id,
                Alert.server_id,
                Alert.resolved_at,
                literal("resolved"),
                literal("good"),
                Alert.title,
            ).where(Alert.resolved_at.is_not(None)),
            select(
                MaintenanceAction.id,
                MaintenanceAction.server_id,
                MaintenanceAction.created_at,
                literal("replacement"),
                literal("info"),
                MaintenanceAction.pool + ": " + MaintenanceAction.state,
            ),
            select(
                MaintenanceWindow.id,
                MaintenanceWindow.server_id,
                MaintenanceWindow.starts_at,
                literal("window"),
                literal("info"),
                MaintenanceWindow.reason,
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
        rows = db.execute(
            query.order_by(events.c.at.desc(), events.c.kind, events.c.id.desc())
            .offset((page - 1) * 50)
            .limit(51)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="timeline.html",
            context={
                "rows": rows[:50],
                "has_next": len(rows) > 50,
                "page": page,
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
            "raw_days": 7,
            "hourly_days": 30,
            "retention": get_int(db, "metric_retention_days", 90),
            "snapshot_retention": get_int(db, "snapshot_retention_days", 30),
            "housekeeping": get_setting(db, "history_housekeeping_at", "Not run yet"),
            "result": get_setting(
                db, "history_housekeeping_result", "No housekeeping result yet"
            ),
        }
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
