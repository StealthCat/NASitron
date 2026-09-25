from __future__ import annotations

import base64
import gzip
import hmac
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .alerts import send_email
from .collector import SSHCollector
from .config import APP_NAME, APP_VERSION, SECRET_KEY, TIMEZONE, WEB_PASSWORD, WEB_USERNAME
from .crypto import encrypt
from .db import SessionLocal, init_db
from .maintenance import ReplacementRequest, discover_replacement_options, replace_drive
from .models import Alert, MaintenanceAction, Metric, Server, Snapshot
from .scheduler import start_scheduler, stop_scheduler, trigger_now
from .service import latest_snapshot
from .settings_store import ensure_defaults, get_bool, get_int, get_setting, set_setting

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def human_bytes(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    idx = 0
    while abs(number) >= 1024 and idx < len(units) - 1:
        number /= 1024.0
        idx += 1
    return f"{number:.1f} {units[idx]}" if idx else f"{number:.0f} {units[idx]}"


def human_duration(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def fmt_dt(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    try:
        zone = ZoneInfo(TIMEZONE)
    except Exception:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")


templates.env.filters["human_bytes"] = human_bytes
templates.env.filters["human_duration"] = human_duration
templates.env.filters["fmt_dt"] = fmt_dt


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        ensure_defaults(db)
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    if request.url.path == "/healthz" or not WEB_USERNAME:
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    valid = False
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            valid = hmac.compare_digest(username, WEB_USERNAME) and hmac.compare_digest(password, WEB_PASSWORD)
        except Exception:
            valid = False
    if not valid:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="NASitron"'})
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    cards = []
    for server in servers:
        snapshot = latest_snapshot(db, server.id)
        active_alerts = db.scalars(
            select(Alert).where(Alert.server_id == server.id, Alert.active.is_(True))
        ).all()
        cards.append({"server": server, "snapshot": snapshot, "alerts": active_alerts})
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "cards": cards, "app_version": APP_VERSION},
    )


@app.get("/servers/new", response_class=HTMLResponse)
def new_server(request: Request):
    return templates.TemplateResponse("server_form.html", {"request": request, "server": None})


@app.post("/servers/new")
def create_server(
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    username: str = Form(...),
    auth_type: str = Form("key"),
    password: str = Form(""),
    private_key: str = Form(""),
    private_key_passphrase: str = Form(""),
    poll_interval_seconds: int = Form(60),
    smart_interval_minutes: int = Form(15),
    sudo_for_smart: bool = Form(False),
    strict_host_key: bool = Form(False),
    enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    server = Server(
        name=name.strip(),
        host=host.strip(),
        port=max(1, min(65535, port)),
        username=username.strip(),
        auth_type=auth_type if auth_type in {"key", "password"} else "key",
        password_enc=encrypt(password),
        private_key_enc=encrypt(private_key),
        private_key_passphrase_enc=encrypt(private_key_passphrase),
        poll_interval_seconds=max(15, poll_interval_seconds),
        smart_interval_minutes=max(1, smart_interval_minutes),
        sudo_for_smart=sudo_for_smart,
        strict_host_key=strict_host_key,
        enabled=enabled,
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    if server.enabled:
        trigger_now(server.id)
    return RedirectResponse(f"/servers/{server.id}", status_code=303)


@app.get("/servers/{server_id}/edit", response_class=HTMLResponse)
def edit_server(server_id: int, request: Request, db: Session = Depends(get_db)):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)
    return templates.TemplateResponse("server_form.html", {"request": request, "server": server})


@app.post("/servers/{server_id}/edit")
def update_server(
    server_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    username: str = Form(...),
    auth_type: str = Form("key"),
    password: str = Form(""),
    private_key: str = Form(""),
    private_key_passphrase: str = Form(""),
    poll_interval_seconds: int = Form(60),
    smart_interval_minutes: int = Form(15),
    sudo_for_smart: bool = Form(False),
    strict_host_key: bool = Form(False),
    enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)
    server.name = name.strip()
    server.host = host.strip()
    server.port = max(1, min(65535, port))
    server.username = username.strip()
    server.auth_type = auth_type if auth_type in {"key", "password"} else "key"
    server.poll_interval_seconds = max(15, poll_interval_seconds)
    server.smart_interval_minutes = max(1, smart_interval_minutes)
    server.sudo_for_smart = sudo_for_smart
    server.strict_host_key = strict_host_key
    server.enabled = enabled
    if password:
        server.password_enc = encrypt(password)
    if private_key:
        server.private_key_enc = encrypt(private_key)
    if private_key_passphrase:
        server.private_key_passphrase_enc = encrypt(private_key_passphrase)
    db.commit()
    if enabled:
        trigger_now(server.id)
    return RedirectResponse(f"/servers/{server.id}", status_code=303)


@app.post("/servers/{server_id}/delete")
def delete_server(server_id: int, db: Session = Depends(get_db)):
    server = db.get(Server, server_id)
    if server:
        db.delete(server)
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/servers/{server_id}/poll")
def poll_server(server_id: int, db: Session = Depends(get_db)):
    if not db.get(Server, server_id):
        raise HTTPException(404)
    trigger_now(server_id)
    return RedirectResponse(f"/servers/{server_id}", status_code=303)


@app.get("/servers/{server_id}", response_class=HTMLResponse)
def server_detail(server_id: int, request: Request, db: Session = Depends(get_db)):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)
    snapshot = latest_snapshot(db, server_id)
    alerts = db.scalars(
        select(Alert).where(Alert.server_id == server_id).order_by(Alert.active.desc(), Alert.last_seen.desc()).limit(50)
    ).all()
    return templates.TemplateResponse(
        "server.html",
        {"request": request, "server": server, "snapshot": snapshot, "alerts": alerts},
    )



def _maintenance_token(server_id: int, offset_hours: int = 0) -> str:
    if not SECRET_KEY:
        raise HTTPException(
            503,
            detail="NASITRON_SECRET_KEY must be configured before remote maintenance is enabled.",
        )
    stamp = (datetime.utcnow() + timedelta(hours=offset_hours)).strftime("%Y%m%d%H")
    payload = f"replace-drive:{server_id}:{stamp}".encode("utf-8")
    return hmac.new(SECRET_KEY.encode("utf-8"), payload, "sha256").hexdigest()


def _valid_maintenance_token(server_id: int, token: str) -> bool:
    if not token:
        return False
    return any(
        hmac.compare_digest(token, _maintenance_token(server_id, offset))
        for offset in (0, -1)
    )


@app.get("/servers/{server_id}/replace-drive", response_class=HTMLResponse)
def replace_drive_page(server_id: int, request: Request, db: Session = Depends(get_db)):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)

    inventory = {"failed": [], "candidates": [], "statuses": {}}
    inventory_error = None
    try:
        inventory = discover_replacement_options(server)
    except Exception as exc:
        inventory_error = str(exc)

    actions = db.scalars(
        select(MaintenanceAction)
        .where(MaintenanceAction.server_id == server_id)
        .order_by(MaintenanceAction.created_at.desc())
        .limit(30)
    ).all()
    return templates.TemplateResponse(
        "replace_drive.html",
        {
            "request": request,
            "server": server,
            "inventory": inventory,
            "inventory_error": inventory_error,
            "actions": actions,
            "csrf_token": _maintenance_token(server_id),
        },
    )


@app.post("/servers/{server_id}/replace-drive")
def perform_drive_replacement(
    server_id: int,
    pool: str = Form(...),
    old_device: str = Form(...),
    new_device: str = Form(...),
    confirm_text: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)
    if not _valid_maintenance_token(server_id, csrf_token):
        raise HTTPException(403, detail="Maintenance confirmation token expired or invalid.")

    expected = f"REPLACE {pool}"
    if confirm_text.strip() != expected:
        message = f"Confirmation text did not match. Type exactly: {expected}"
        return RedirectResponse(
            f"/servers/{server_id}/replace-drive?message=" + quote(message),
            status_code=303,
        )

    request_data = ReplacementRequest(
        pool=pool.strip(),
        old_device=old_device.strip(),
        new_device=new_device.strip(),
    )

    try:
        result = replace_drive(server, request_data)
        output = "\n".join(
            part for part in [result.get("stdout", ""), result.get("stderr", ""), result.get("pool_status", "")]
            if part
        )
        db.add(
            MaintenanceAction(
                server_id=server.id,
                action="zpool_replace",
                pool=request_data.pool,
                old_device=request_data.old_device,
                new_device=request_data.new_device,
                command=result.get("command", ""),
                success=bool(result.get("ok")),
                exit_code=result.get("exit"),
                output=output[-20000:],
            )
        )
        db.commit()

        if result.get("ok"):
            trigger_now(server.id)
            message = (
                f"Replacement command accepted for {request_data.old_device} -> "
                f"{request_data.new_device}. ZFS should now resilver; monitor pool status."
            )
        else:
            detail = (result.get("stderr") or result.get("stdout") or "zpool replace failed").strip()
            message = f"Replacement failed: {detail[:1500]}"
    except Exception as exc:
        db.add(
            MaintenanceAction(
                server_id=server.id,
                action="zpool_replace",
                pool=request_data.pool,
                old_device=request_data.old_device,
                new_device=request_data.new_device,
                command="",
                success=False,
                exit_code=None,
                output=str(exc)[:20000],
            )
        )
        db.commit()
        message = f"Replacement was not started: {exc}"

    return RedirectResponse(
        f"/servers/{server_id}/replace-drive?message=" + quote(message),
        status_code=303,
    )


@app.get("/api/servers/{server_id}/metrics")
def metric_history(
    server_id: int,
    name: str = Query(..., min_length=1, max_length=100),
    scope: str = Query(""),
    hours: int = Query(24, ge=1, le=24 * 365),
    db: Session = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = db.scalars(
        select(Metric)
        .where(
            Metric.server_id == server_id,
            Metric.name == name,
            Metric.scope == scope,
            Metric.captured_at >= since,
        )
        .order_by(Metric.captured_at)
    ).all()
    return {
        "name": name,
        "scope": scope,
        "points": [{"t": row.captured_at.isoformat() + "Z", "v": row.value} for row in rows],
    }


@app.get("/servers/{server_id}/support-bundle")
def support_bundle(server_id: int, db: Session = Depends(get_db)):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)
    with SSHCollector(server) as collector:
        raw = collector.deep_collect()
    payload = {
        "format": "nasitron-support-bundle",
        "format_version": 1,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "nasitron_version": APP_VERSION,
        "server": {
            "name": server.name,
            "host": server.host,
            "port": server.port,
            "username": server.username,
            "poll_interval_seconds": server.poll_interval_seconds,
            "smart_interval_minutes": server.smart_interval_minutes,
            "sudo_for_smart": server.sudo_for_smart,
        },
        "latest_snapshot": latest_snapshot(db, server_id),
        "diagnostics": raw,
        "privacy_note": "SSH private keys/passwords and SMTP credentials are never included in this bundle.",
    }
    compressed = gzip.compress(json.dumps(payload, indent=2, default=str).encode("utf-8"), compresslevel=6)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", server.name).strip("-") or f"server-{server.id}"
    filename = f"nasitron-{safe}-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    return Response(
        compressed,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: Session = Depends(get_db)):
    alerts = db.scalars(select(Alert).order_by(Alert.active.desc(), Alert.last_seen.desc()).limit(500)).all()
    return templates.TemplateResponse("alerts.html", {"request": request, "alerts": alerts})


@app.post("/alerts/{alert_id}/ack")
def acknowledge_alert(alert_id: int, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    if alert:
        alert.acknowledged = True
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    values = {}
    keys = [
        "smtp_enabled", "smtp_host", "smtp_port", "smtp_username", "smtp_from", "smtp_to",
        "smtp_starttls", "smtp_ssl", "pool_capacity_warning", "pool_capacity_critical",
        "drive_temp_warning_c", "drive_temp_critical_c", "scrub_age_warning_days",
        "collection_failure_threshold", "metric_retention_days", "snapshot_retention_days",
    ]
    for key in keys:
        values[key] = get_setting(db, key)
    values["smtp_password_configured"] = bool(get_setting(db, "smtp_password"))
    return templates.TemplateResponse("settings.html", {"request": request, "values": values})


@app.post("/settings")
def save_settings(
    smtp_enabled: bool = Form(False),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_to: str = Form(""),
    smtp_starttls: bool = Form(False),
    smtp_ssl: bool = Form(False),
    pool_capacity_warning: int = Form(80),
    pool_capacity_critical: int = Form(90),
    drive_temp_warning_c: int = Form(45),
    drive_temp_critical_c: int = Form(55),
    scrub_age_warning_days: int = Form(35),
    collection_failure_threshold: int = Form(2),
    metric_retention_days: int = Form(90),
    snapshot_retention_days: int = Form(7),
    db: Session = Depends(get_db),
):
    values = {
        "smtp_enabled": str(smtp_enabled).lower(),
        "smtp_host": smtp_host.strip(),
        "smtp_port": str(max(1, min(65535, smtp_port))),
        "smtp_username": smtp_username.strip(),
        "smtp_from": smtp_from.strip(),
        "smtp_to": smtp_to.strip(),
        "smtp_starttls": str(smtp_starttls).lower(),
        "smtp_ssl": str(smtp_ssl).lower(),
        "pool_capacity_warning": str(max(1, min(99, pool_capacity_warning))),
        "pool_capacity_critical": str(max(1, min(100, pool_capacity_critical))),
        "drive_temp_warning_c": str(max(1, drive_temp_warning_c)),
        "drive_temp_critical_c": str(max(1, drive_temp_critical_c)),
        "scrub_age_warning_days": str(max(0, scrub_age_warning_days)),
        "collection_failure_threshold": str(max(1, collection_failure_threshold)),
        "metric_retention_days": str(max(1, metric_retention_days)),
        "snapshot_retention_days": str(max(1, snapshot_retention_days)),
    }
    for key, value in values.items():
        set_setting(db, key, value)
    if smtp_password:
        set_setting(db, "smtp_password", smtp_password, secret=True)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/test-email")
def test_email(db: Session = Depends(get_db)):
    try:
        send_email(
            db,
            "[NASitron] SMTP test",
            f"NASitron {APP_VERSION} successfully connected to the configured SMTP relay.",
            force=True,
        )
        result = "SMTP test message sent."
    except Exception as exc:
        result = f"SMTP test failed: {exc}"
    return RedirectResponse("/settings?message=" + quote(result), status_code=303)
