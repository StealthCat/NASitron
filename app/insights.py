"""Read-only insights and explicitly authorized monitoring controls."""

from datetime import datetime, timedelta
import gzip
import io
import json
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import Session

from .db import SessionLocal
from .experience import forecast, server_state, operation_status
from .models import (
    Alert,
    Server,
    Metric,
    MetricRollup,
    SnapshotInventory, InventoryStatus,
    DriveLabel,
    MaintenanceWindow,
    MaintenanceAction,
)
from .security import require_csrf
from .service import latest_snapshot, current_states
from .support import sanitize_diagnostics
from .settings_store import get_int


def session():
    with SessionLocal() as db:
        yield db


def bundle_values(data):
    if not isinstance(data, dict) or data.get("format") != "nasitron-support-bundle":
        raise ValueError("Expected a NASitron tuning bundle")
    diagnostics = sanitize_diagnostics(data.get("diagnostics", {}))
    result = {}
    for key in ("zfs_get_all", "zpool_get_all"):
        for line in diagnostics.get(key, {}).get("stdout", "").splitlines():
            fields = line.split("\t")
            if len(fields) >= 3:
                result[f"{key}/{fields[0]}/{fields[1]}"] = " · ".join(fields[2:])
    for key in ("zfs_module_parameters", "sysctl_relevant", "lscpu"):
        for line in diagnostics.get(key, {}).get("stdout", "").splitlines():
            separator = "=" if key == "sysctl_relevant" else ":"
            if separator in line:
                name, value = line.split(separator, 1)
                result[f"{key}/{name.strip()}"] = value.strip()
    for drive in (data.get("latest_snapshot") or {}).get("drives", []):
        identity = drive.get("serial") or drive.get("path")
        result[f"hardware/{identity}"] = json.dumps(
            {k: drive.get(k) for k in ("model", "size_bytes", "transport")},
            sort_keys=True,
        )
    return result


async def read_bundle(upload):
    raw = await upload.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Bundle exceeds 8 MiB upload limit")
    if raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("Expanded bundle exceeds 32 MiB")
    return bundle_values(json.loads(raw))


def sparkline(points):
    if len(points) < 2:
        return ''
    values = [float(v) for _,v in points]
    lo,hi = min(values),max(values)
    return ' '.join(f"{i*240/(len(values)-1):.1f},{48-(v-lo)*40/(hi-lo or 1):.1f}" for i,v in enumerate(values))


def install(app, templates):
    @app.get("/disk-io")
    def disk_io_page(request: Request, server_id: int | None = None,
                     identity: str = "", db: Session = Depends(session)):
        servers = db.scalars(select(Server).order_by(Server.name)).all()
        server = db.get(Server, server_id) if server_id is not None else (servers[0] if servers else None)
        if server_id is not None and server is None:
            raise HTTPException(404)
        snapshot = latest_snapshot(db, server.id) or {} if server else {}
        disks = {d.get("serial") or d.get("path"): d for d in snapshot.get("drives", [])}
        if server:
            historical = db.scalars(select(Metric.scope).where(
                Metric.server_id == server.id, Metric.name == "drive.io.read_bps"
            ).union(select(MetricRollup.scope).where(MetricRollup.server_id == server.id, MetricRollup.name == "drive.io.read_bps"))).all()
            for scope in historical:
                disks.setdefault(scope, {"path": scope, "model": "Historical device"})
        if server:
            for label in db.scalars(select(DriveLabel).where(DriveLabel.server_id == server.id)):
                if label.identity in disks:
                    disks[label.identity]["bay_label"] = label.label
        selected = identity if identity in disks else next(iter(disks), "")
        return templates.TemplateResponse(request=request, name="disk_io.html", context={
            "servers": servers, "server": server, "disks": disks, "identity": selected,
            "busiest": sorted([d for d in snapshot.get("drives", []) if (d.get("io") or {}).get("busy_pct") is not None], key=lambda d:d["io"]["busy_pct"], reverse=True)[:10],
            "snapshot": snapshot, "freshness": server_state(server, snapshot) if server else None,
        })

    @app.get("/operations")
    def operations(request: Request, db: Session = Depends(session)):
        rows = []
        servers = db.scalars(select(Server).order_by(Server.name)).all()
        states = current_states(db, servers)
        for server in servers:
            snapshot = states.get(server.id)
            for pool in (snapshot or {}).get("pools", []):
                rows.append(
                    {
                        "server": server,
                        "pool": pool,
                        "status": operation_status(pool),
                        "freshness": server_state(server, snapshot),
                    }
                )
        windows = db.scalars(
            select(MaintenanceWindow)
            .where(MaintenanceWindow.ends_at > datetime.utcnow())
            .order_by(MaintenanceWindow.starts_at)
        ).all()
        actions = db.scalars(
            select(MaintenanceAction).options(selectinload(MaintenanceAction.server))
            .order_by(MaintenanceAction.created_at.desc())
            .limit(100)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="operations.html",
            context={
                "rows": rows,
                "windows": windows,
                "servers": servers,
                "actions": actions,
            },
        )

    @app.post(
        "/servers/{server_id}/maintenance-window", dependencies=[Depends(require_csrf)]
    )
    def window(
        server_id: int,
        request: Request,
        minutes: int = Form(...),
        delay_minutes: int = Form(0),
        reason: str = Form("Planned maintenance"),
        db: Session = Depends(session),
    ):
        if not db.get(Server, server_id):
            raise HTTPException(404)
        if (
            not 0 <= minutes <= 10080
            or not 0 <= delay_minutes <= 43200
            or len(reason) > 255
        ):
            raise HTTPException(400, "Invalid window duration or reason")
        now = datetime.utcnow()
        if minutes == 0:
            for w in db.scalars(
                select(MaintenanceWindow).where(
                    MaintenanceWindow.server_id == server_id,
                    MaintenanceWindow.ends_at > now,
                )
            ):
                w.ends_at = now
        else:
            db.add(
                MaintenanceWindow(
                    server_id=server_id,
                    starts_at=now + timedelta(minutes=delay_minutes),
                    ends_at=now + timedelta(minutes=delay_minutes + minutes),
                    reason=reason,
                    actor=request.state.current_user.username,
                )
            )
        db.commit()
        return RedirectResponse("/operations", status_code=303)

    @app.post("/alerts/{alert_id}/snooze", dependencies=[Depends(require_csrf)])
    def snooze(alert_id: int, minutes: int = Form(60), db: Session = Depends(session)):
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(404)
        if not 0 <= minutes <= 10080:
            raise HTTPException(400, "Maximum snooze is 7 days")
        alert.snoozed_until = (
            datetime.utcnow() + timedelta(minutes=minutes) if minutes else None
        )
        db.commit()
        return RedirectResponse("/alerts", status_code=303)

    @app.get("/servers/{server_id}/drive")
    def drive(
        server_id: int, request: Request, identity: str, db: Session = Depends(session)
    ):
        server = db.get(Server, server_id)
        if not server:
            raise HTTPException(404)
        snapshot = latest_snapshot(db, server_id) or {}
        disk = next(
            (
                d
                for d in snapshot.get("drives", [])
                if (d.get("serial") or d.get("path")) == identity
            ),
            None,
        )
        if not disk:
            raise HTTPException(404, "Drive is absent from the last-known inventory")
        label = db.get(DriveLabel, (server_id, identity))
        return templates.TemplateResponse(
            request=request,
            name="drive_detail.html",
            context={
                "server": server,
                "disk": disk,
                "identity": identity,
                "label": label.label if label else "",
                "freshness": server_state(server, snapshot),
            },
        )

    @app.post("/servers/{server_id}/drive-label", dependencies=[Depends(require_csrf)])
    def drive_label(
        server_id: int,
        identity: str = Form(...),
        label: str = Form(""),
        db: Session = Depends(session),
    ):
        snapshot = latest_snapshot(db, server_id) or {}
        disk = next(
            (
                d
                for d in snapshot.get("drives", [])
                if (d.get("serial") or d.get("path")) == identity
            ),
            None,
        )
        if not disk or not disk.get("serial"):
            raise HTTPException(
                400, "A stable serial number is required for a physical label"
            )
        if len(label) > 120 or len(identity) > 255:
            raise HTTPException(400, "Label or identity too long")
        row = db.get(DriveLabel, (server_id, identity))
        if row:
            row.label = label
        else:
            db.add(DriveLabel(server_id=server_id, identity=identity, label=label))
        db.commit()
        return RedirectResponse(
            f"/servers/{server_id}/drive?identity=" + quote(identity, safe=""),
            status_code=303,
        )

    @app.get("/snapshots")
    def snapshots(request: Request, db: Session = Depends(session)):
        params = request.query_params
        q = params.get("q", "")[:200].strip()
        sort = params.get("sort", "name")
        if sort not in {"name", "created", "used", "referenced"}:
            sort = "name"
        descending = params.get("direction") == "desc"
        try:
            page = max(1, min(100000, int(params.get("page", "1"))))
            size = int(params.get("size", "50"))
        except ValueError:
            page, size = 1, 50
        if size not in {25,50,100,250}:
            size = 50
        from .inventory_store import backfill_inventory
        backfill_inventory(db)
        inventories=[{"server":server,"inventory":{"captured_at":status.captured_at,"error":status.error}} for server,status in db.execute(select(Server,InventoryStatus).join(InventoryStatus)).all()]
        query=select(SnapshotInventory,Server).join(Server)
        if q:
            query=query.where(or_(SnapshotInventory.name.contains(q,autoescape=True),Server.name.contains(q,autoescape=True)))
        total=db.scalar(select(func.count()).select_from(query.subquery())) or 0
        pages=max(1,(total+size-1)//size)
        page=min(page,pages)
        column=getattr(SnapshotInventory,"created_at" if sort=="created" else sort)
        data=db.execute(query.order_by(column.desc() if descending else column.asc(),SnapshotInventory.id).offset((page-1)*size).limit(size)).all()
        rows=[{"server":server,"name":r.name,"created":r.created_at,"used":r.used,"referenced":r.referenced} for r,server in data]
        return templates.TemplateResponse(
            request=request,
            name="snapshots.html",
            context={"rows": rows, "inventories": inventories, "q":q,"sort":sort,"direction":"desc" if descending else "asc","page":page,"pages":pages,"size":size,"total":total},
        )

    @app.get("/forecasts")
    def forecasts(request: Request, db: Session = Depends(session)):
        rows = []
        warning = get_int(db, "pool_capacity_warning", 80)
        critical = get_int(db, "pool_capacity_critical", 90)
        servers = db.scalars(select(Server).order_by(Server.name)).all()
        states = current_states(db, servers)
        for server in servers:
            snapshot = states.get(server.id) or {}
            from .history import history_series
            now = datetime.utcnow()
            scopes = [p["name"] for p in snapshot.get("pools", [])]
            history = history_series(db, server, ["pool.capacity_pct"], scopes, now-timedelta(days=30), now, bucket_seconds=86400, latest_exact=False) if scopes else []
            history_by_pool = {h["scope"]: [(datetime.fromisoformat(p["t"].removesuffix("Z")), p["v"]) for p in h["points"]] for h in history}
            for pool in snapshot.get("pools", []):
                points = history_by_pool.get(pool["name"], [])
                rows.append(
                    {
                        "server": server,
                        "pool": pool,
                        "warning": forecast(points, warning),
                        "trend": sparkline(points),
                        "critical": forecast(points, critical),
                        "freshness": server_state(server, snapshot),
                    }
                )
        return templates.TemplateResponse(
            request=request,
            name="forecasts.html",
            context={
                "rows": rows,
                "warning_threshold": warning,
                "critical_threshold": critical,
            },
        )

    @app.get("/settings/tools")
    def tools_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="tools.html", context={"changes": None, "error": None}
        )

    @app.post("/settings/tools/compare", dependencies=[Depends(require_csrf)])
    async def compare(
        request: Request, before: UploadFile = File(...), after: UploadFile = File(...)
    ):
        error, changes = None, None
        try:
            old, new = await read_bundle(before), await read_bundle(after)
            changes = [
                {"key": k, "before": old.get(k, "—"), "after": new.get(k, "—")}
                for k in sorted(old.keys() | new.keys())
                if old.get(k) != new.get(k)
            ]
        except (
            ValueError,
            TypeError,
            AttributeError,
            OSError,
            EOFError,
            RecursionError,
        ):
            error = "Could not compare bundles. Use valid NASitron JSON or JSON.gz files within the size limits."
        finally:
            await before.close()
            await after.close()
        return templates.TemplateResponse(
            request=request,
            name="tools.html",
            context={"changes": changes, "error": error},
            status_code=400 if error else 200,
        )
