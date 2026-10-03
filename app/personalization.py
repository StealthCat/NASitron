"""User display preferences, saved routes, and operator-managed enclosure slots."""

from contextvars import ContextVar
from urllib.parse import urlsplit
from zoneinfo import available_timezones
from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func, delete
from .models import WebUser, SavedView, Server, Enclosure, BayAssignment
from .db import SessionLocal
from .security import require_csrf
from .config import TIMEZONE
from .service import latest_snapshot
from .settings_store import set_setting, get_int

user_timezone = ContextVar("user_timezone", default=TIMEZONE)


def session():
    with SessionLocal() as db:
        yield db


def install(app, templates):
    @app.get("/preferences")
    def preferences(request: Request, db=Depends(session)):
        views = db.scalars(
            select(SavedView)
            .where(SavedView.user_id == request.state.current_user.id)
            .order_by(SavedView.name)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="preferences.html",
            context={
                "zones": sorted(available_timezones()),
                "views": views,
                "default_timezone": TIMEZONE,
            },
        )

    @app.post("/preferences", dependencies=[Depends(require_csrf)])
    def save_preferences(
        request: Request, timezone: str = Form(""), db=Depends(session)
    ):
        if timezone and timezone not in available_timezones():
            raise HTTPException(400, "Choose a valid timezone.")
        db.get(WebUser, request.state.current_user.id).timezone = timezone
        db.commit()
        return RedirectResponse("/preferences", 303)

    @app.post("/views", dependencies=[Depends(require_csrf)])
    def save_view(
        request: Request,
        name: str = Form(...),
        path: str = Form(...),
        db=Depends(session),
    ):
        try:
            parsed = urlsplit(path)
        except ValueError:
            raise HTTPException(400, "Invalid saved view path.")
        if (
            not name.strip()
            or len(name) > 80
            or len(path) > 2048
            or parsed.scheme
            or parsed.netloc
            or "\\" in path
            or any(ord(c) < 32 for c in path)
            or parsed.path
            not in {
                "/",
                "/servers",
                "/pools",
                "/drives",
                "/datasets",
                "/snapshots",
                "/disk-io",
                "/pool-io",
                "/timeline",
                "/forecasts",
                "/operations",
                "/drive-bays",
                "/diagnostics",
            }
        ):
            raise HTTPException(
                400,
                "Save a supported monitoring page with a name of up to 80 characters.",
            )
        uid = request.state.current_user.id
        if (
            db.scalar(
                select(func.count())
                .select_from(SavedView)
                .where(SavedView.user_id == uid)
            )
            >= 30
        ):
            raise HTTPException(400, "Maximum 30 saved views.")
        db.add(SavedView(user_id=uid, name=name.strip(), path=path))
        db.commit()
        return RedirectResponse("/preferences", 303)

    @app.post("/views/{view_id}/delete", dependencies=[Depends(require_csrf)])
    def remove_view(view_id: int, request: Request, db=Depends(session)):
        row = db.get(SavedView, view_id)
        if not row or row.user_id != request.state.current_user.id:
            raise HTTPException(404)
        db.delete(row)
        db.commit()
        return RedirectResponse("/preferences", 303)

    @app.post("/enclosures", dependencies=[Depends(require_csrf)])
    def create_enclosure(
        server_id: int = Form(...),
        name: str = Form(...),
        rows: int = Form(...),
        columns: int = Form(...),
        db=Depends(session),
    ):
        if not db.get(Server, server_id):
            raise HTTPException(404)
        if (
            not 1 <= rows <= 12
            or not 1 <= columns <= 24
            or not name.strip()
            or len(name) > 120
        ):
            raise HTTPException(
                400, "Use a name up to 120 characters, 1–12 rows and 1–24 columns."
            )
        if (
            db.scalar(
                select(func.count())
                .select_from(Enclosure)
                .where(Enclosure.server_id == server_id)
            )
            >= 10
        ):
            raise HTTPException(400, "Maximum ten enclosures per server.")
        db.add(
            Enclosure(
                server_id=server_id, name=name.strip(), rows=rows, columns=columns
            )
        )
        db.commit()
        return RedirectResponse("/drive-bays?server=" + str(server_id), 303)

    @app.post("/enclosures/{enclosure_id}/assign", dependencies=[Depends(require_csrf)])
    def assign_bay(
        enclosure_id: int,
        slot: int = Form(...),
        identity: str = Form(""),
        empty_only: bool = Form(False),
        db=Depends(session),
    ):
        enclosure = db.get(Enclosure, enclosure_id)
        if not enclosure:
            raise HTTPException(404)
        if not 1 <= slot <= enclosure.rows * enclosure.columns:
            raise HTTPException(400, "Choose a slot within this enclosure.")
        if empty_only and db.get(BayAssignment, (enclosure_id, slot)):
            raise HTTPException(409, "This bay was assigned while the page was open. Refresh the page and choose an empty bay.")
        if empty_only and not identity:
            raise HTTPException(400, "Select a drive to assign.")
        if identity:
            disks = (latest_snapshot(db, enclosure.server_id) or {}).get("drives", [])
            if not any(d.get("serial") == identity for d in disks):
                raise HTTPException(
                    400,
                    "Choose a currently detected drive with a stable serial number.",
                )
            occupied = db.scalar(
                select(BayAssignment)
                .join(Enclosure)
                .where(
                    Enclosure.server_id == enclosure.server_id,
                    BayAssignment.identity == identity,
                )
            )
            if occupied and (occupied.enclosure_id, occupied.slot) != (
                enclosure_id,
                slot,
            ):
                raise HTTPException(
                    409, "This drive already has a slot. Clear its old slot first."
                )
        row = db.get(BayAssignment, (enclosure_id, slot))
        if row:
            db.delete(row)
            db.flush()
        if identity:
            db.add(
                BayAssignment(enclosure_id=enclosure_id, slot=slot, identity=identity)
            )
        db.commit()
        return RedirectResponse("/drive-bays?server=" + str(enclosure.server_id), 303)

    @app.post("/enclosures/{enclosure_id}/delete", dependencies=[Depends(require_csrf)])
    def delete_enclosure(enclosure_id: int, db=Depends(session)):
        enclosure = db.get(Enclosure, enclosure_id)
        if not enclosure:
            raise HTTPException(404)
        sid = enclosure.server_id
        db.execute(
            delete(BayAssignment).where(BayAssignment.enclosure_id == enclosure_id)
        )
        db.delete(enclosure)
        db.commit()
        return RedirectResponse("/drive-bays?server=" + str(sid), 303)

    @app.post("/settings/history-policy", dependencies=[Depends(require_csrf)])
    def history_policy(
        request: Request,
        raw_days: int = Form(...),
        hourly_days: int = Form(...),
        mode: str = Form("preview"),
        db=Depends(session),
    ):
        if not 1 <= raw_days <= hourly_days <= 3650:
            raise HTTPException(400, "Use 1 ≤ raw days ≤ hourly days ≤ 3650.")
        if mode == "apply":
            set_setting(db, "raw_history_days", str(raw_days))
            set_setting(db, "hourly_history_days", str(hourly_days))
            db.commit()
            return RedirectResponse("/settings/database", 303)
        return templates.TemplateResponse(
            request=request,
            name="history_policy.html",
            context={
                "raw_days": raw_days,
                "hourly_days": hourly_days,
                "retention": get_int(db, "metric_retention_days", 90),
            },
        )
