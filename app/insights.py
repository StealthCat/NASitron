"""Read-only insights and explicitly authorized monitoring controls."""

from datetime import datetime, timedelta
import gzip
import io
import json
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .db import SessionLocal
from .experience import forecast, server_state, operation_status
from .models import (
    Alert,
    Server,
    Metric,
    DriveLabel,
    MaintenanceWindow,
    MaintenanceAction,
)
from .security import require_csrf
from .service import latest_snapshot
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
            ).distinct()).all()
            for scope in historical:
                disks.setdefault(scope, {"path": scope, "model": "Historical device"})
        selected = identity if identity in disks else next(iter(disks), "")
        return templates.TemplateResponse(request=request, name="disk_io.html", context={
            "servers": servers, "server": server, "disks": disks, "identity": selected,
            "snapshot": snapshot, "freshness": server_state(server, snapshot) if server else None,
        })

    @app.get("/operations")
    def operations(request: Request, db: Session = Depends(session)):
        rows = []
        servers = db.scalars(select(Server).order_by(Server.name)).all()
        for server in servers:
            snapshot = latest_snapshot(db, server.id)
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
            select(MaintenanceAction)
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
        rows, inventories = [], []
        for server in db.scalars(select(Server).order_by(Server.name)):
            snapshot = latest_snapshot(db, server.id) or {}
            inventory = snapshot.get("snapshot_inventory", {})
            inventories.append({"server": server, "inventory": inventory})
            for row in inventory.get("rows", []):
                rows.append({"server": server, **row})
        return templates.TemplateResponse(
            request=request,
            name="snapshots.html",
            context={"rows": rows, "inventories": inventories},
        )

    @app.get("/forecasts")
    def forecasts(request: Request, db: Session = Depends(session)):
        rows = []
        warning = get_int(db, "pool_capacity_warning", 80)
        critical = get_int(db, "pool_capacity_critical", 90)
        for server in db.scalars(select(Server).order_by(Server.name)):
            snapshot = latest_snapshot(db, server.id) or {}
            for pool in snapshot.get("pools", []):
                # Aggregate in SQL to keep a month of high-frequency samples bounded.
                points = db.execute(
                    select(func.min(Metric.captured_at), func.avg(Metric.value))
                    .where(
                        Metric.server_id == server.id,
                        Metric.name == "pool.capacity_pct",
                        Metric.scope == pool["name"],
                        Metric.captured_at >= datetime.utcnow() - timedelta(days=30),
                    )
                    .group_by(func.date(Metric.captured_at))
                    .order_by(func.min(Metric.captured_at))
                ).all()
                rows.append(
                    {
                        "server": server,
                        "pool": pool,
                        "warning": forecast(points, warning),
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
