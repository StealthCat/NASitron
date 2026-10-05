from __future__ import annotations

from .devices import is_zvol, device_label, drive_health

import math
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select, or_
from sqlalchemy.orm import Session, selectinload
from starlette.background import BackgroundTask

from .alerts import send_email
from .collector import CollectorError, SSHCollector
from .config import (
    ALLOW_INSECURE_HTTP,
    APP_NAME,
    APP_VERSION,
    SESSION_TTL_SECONDS,
    TIMEZONE,
    validate_runtime_config,
)
from .crypto import decrypt, encrypt
from .db import SessionLocal, init_db
from .instance_lock import InstanceLock
from .topology import pool_topology
from .parser import parse_status_sections
from .pool_capacity import capacity_rows, attach_capacity
from .maintenance import (
    ReplacementRequest,
    discover_replacement_options,
    invalidate_inventory_cache,
    replace_drive,
)
from .metrics import METRIC_NAMES
from .middleware import (
    RequestBodyLimitMiddleware,
    RequireHTTPSMiddleware,
    SecurityHeadersMiddleware,
)
from .models import Alert, CurrentState, MaintenanceAction, RemoteEnrollment, Server, WebUser, DriveLabel, Enclosure, BayAssignment
from .scheduler import start_scheduler, stop_scheduler, trigger_now
from .security import (
    SESSION_COOKIE_NAME,
    authenticate_web_credentials,
    clear_login_failures,
    create_session_token,
    csrf_token,
    ensure_bootstrap_admin,
    hash_password,
    login_is_rate_limited,
    record_login_failure,
    request_is_authenticated,
    request_user,
    require_csrf,
    require_secure_maintenance,
    safe_next_url,
)
from .experience import server_state, smart_state, pool_state, effective_role, operation_status, alert_target
from .insights import install as install_insights
from .observability import install as install_observability
from .personalization import install as install_personalization, user_timezone
from .zfs_actions import install as install_zfs_actions
from .storage_admin import install as install_storage_admin
from .service import latest_snapshot
from .settings_store import ensure_defaults, get_many, get_setting, set_setting, get_int
from .support import sanitize_diagnostics, support_bundle_lock
from .tls_manager import (
    ACME_CA_ROOT_PATH,
    MANUAL_CERT_PATH,
    MANUAL_KEY_PATH,
    TLSConfigurationError,
    build_caddyfile,
    manual_certificate_status,
    persist_and_apply_caddyfile,
    validate_acme_directory_url,
    validate_ca_root,
    validate_certificate_pair,
    write_secret_file,
)
from .validation import (
    bad_request,
    bounded_int,
    bounded_multiline,
    bounded_secret,
    bounded_text,
    validate_email,
    validate_host,
    validate_recipient_list,
    validate_threshold_pair,
)

from .display import temperature, temperature_value, power_on_duration, alert_temperature

BASE_DIR = Path(__file__).resolve().parent
INSTALLER_PATH = BASE_DIR.parent / "scripts" / "install-remote.sh"


def display_context(request):
    with SessionLocal() as db:
        unit = get_setting(db, "temperature_unit", "C")
    return {"temperature_unit": unit if unit in {"C", "F"} else "C"}


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"), context_processors=[display_context])
templates.env.filters.update(temperature=temperature, power_on_duration=power_on_duration,
                             alert_temperature=alert_temperature)
templates.env.globals["temperature_value"] = temperature_value
templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["display_timezone"] = lambda: user_timezone.get()
_instance_lock = InstanceLock()


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
            value = datetime.fromisoformat(value.removesuffix("Z"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo(user_timezone.get())).strftime("%Y-%m-%d %H:%M:%S %Z")


templates.env.filters["human_bytes"] = human_bytes
templates.env.filters["human_duration"] = human_duration
templates.env.filters["fmt_dt"] = fmt_dt
templates.env.globals.update(is_zvol=is_zvol, device_label=device_label, drive_health=drive_health, server_state=server_state, smart_state=smart_state,
                             pool_state=pool_state, effective_role=effective_role,
                             operation_status=operation_status, alert_target=alert_target)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_config()
    _instance_lock.acquire()
    try:
        init_db()
        with SessionLocal() as db:
            ensure_defaults(db)
            ensure_bootstrap_admin(db)
        start_scheduler()
        yield
    finally:
        stop_scheduler()
        _instance_lock.release()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(RequireHTTPSMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def require_web_session(request: Request, call_next):
    path = request.url.path
    public_path = (
        path in {"/healthz", "/login", "/install-remote.sh"}
        or path.startswith("/static/")
        or path.startswith("/api/enroll/")
    )

    with SessionLocal() as db:
        user = request_user(request, db)
        if user is not None:
            request.state.current_user = user

    user_timezone.set((user.timezone if user else "") or TIMEZONE)
    if public_path:
        return await call_next(request)
    if user is not None:
        role = effective_role(user)
        # Deny by default: operators have an explicit action allowlist.
        admin_page = (path.startswith(("/settings", "/users", "/api/enrollments")) or
                      path == "/servers/new" or path.endswith(("/edit", "/host-key")))
        operator_action = bool(re.fullmatch(
            r"/servers/\d+/(poll|replace-drive|support-bundle|drive-label|maintenance-window)|/alerts/\d+/(ack|snooze)|/enclosures(?:/\d+/(?:assign|delete))?", path))
        if admin_page and role != "admin":
            return PlainTextResponse("Administrator access required.", status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and path not in {"/logout","/preferences","/views"} and not re.fullmatch(r"/views/\d+/delete",path):
            if role != "admin" and not (role == "operator" and operator_action):
                return PlainTextResponse("This action requires an operator or administrator.", status_code=403)
        if "replace-drive" in path and role == "viewer":
            return PlainTextResponse("Operator access required.", status_code=403)
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse(
            {"detail": "Authentication required."},
            status_code=401,
        )

    query = request.url.query
    next_url = path + (f"?{query}" if query else "")
    return RedirectResponse(
        "/login?next=" + quote(safe_next_url(next_url), safe="/"),
        status_code=303,
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = Query("/"),
    db: Session = Depends(get_db),
):
    if request_is_authenticated(request, db):
        return RedirectResponse(safe_next_url(next), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "next_url": safe_next_url(next),
            "error": None,
        },
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    next_url = safe_next_url(next)
    if login_is_rate_limited(request):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            status_code=429,
            context={
                "request": request,
                "next_url": next_url,
                "error": "Too many failed sign-in attempts. Try again in a few minutes.",
            },
        )

    user = authenticate_web_credentials(db, username, password)
    if user is None:
        record_login_failure(request)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            status_code=401,
            context={
                "request": request,
                "next_url": next_url,
                "error": "Invalid username or password.",
            },
        )

    clear_login_failures(request)
    user.last_login_at = datetime.utcnow()
    db.commit()
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(user),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme.lower() == "https" or not ALLOW_INSECURE_HTTP,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/logout")
def logout(_: None = Depends(require_csrf)):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


def _current_user(request: Request) -> WebUser:
    user = getattr(request.state, "current_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _require_admin(request: Request) -> WebUser:
    user = _current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


def _clean_web_username(value: str) -> str:
    username = bounded_text(value, "Username", maximum=120)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,119}", username):
        bad_request(
            "Username must start with a letter or number and contain only letters, "
            "numbers, dot, underscore, @, or hyphen."
        )
    return username


def _validate_web_password(value: str, field: str = "Password") -> str:
    password = bounded_secret(value, field, maximum=4096)
    if len(password) < 12:
        bad_request(f"{field} must contain at least 12 characters.")
    return password


def _validate_mailjet_api_url(value: str) -> str:
    url = bounded_text(value, "Mailjet API URL", maximum=2048)
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        bad_request(
            "Mailjet API URL must be an HTTPS URL without credentials or a fragment."
        )
    return url


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/install-remote.sh", response_class=FileResponse)
def download_remote_installer():
    if not INSTALLER_PATH.is_file():
        raise HTTPException(status_code=404, detail="Remote installer is not packaged.")
    return FileResponse(
        INSTALLER_PATH,
        media_type="text/x-shellscript",
        filename="nasitron-install-remote.sh",
        headers={"X-NASitron-Version": APP_VERSION},
    )


def _build_enrollment_installer(
    enrollment: RemoteEnrollment,
    callback_url: str,
    secret: str,
    *,
    internal_tls: bool,
) -> str:
    preamble = (
        "#!/usr/bin/env bash\n"
        "# One-time NASitron enrollment bootstrap.\n"
        f"export NASITRON_SSH_PUBLIC_KEY={shlex.quote(enrollment.public_key)}\n"
        f"export NASITRON_ENROLL_URL={shlex.quote(callback_url)}\n"
        f"export NASITRON_ENROLL_SECRET={shlex.quote(secret)}\n"
    )
    if internal_tls:
        preamble += "set -- --enroll-insecure \"$@\"\n"
    return preamble + "\n" + INSTALLER_PATH.read_text(encoding="utf-8")


@app.get("/api/enroll/{enrollment_id}/install.sh")
def download_enrollment_installer(
    enrollment_id: str,
    request: Request,
    token: str = Query(..., min_length=64, max_length=64),
    db: Session = Depends(get_db),
):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", enrollment_id):
        raise HTTPException(status_code=404)
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise HTTPException(status_code=404)

    enrollment = db.get(RemoteEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404)
    now = datetime.utcnow()
    if enrollment.used_at is not None:
        raise HTTPException(status_code=409, detail="Enrollment token has already been used.")
    if enrollment.expires_at <= now:
        raise HTTPException(status_code=410, detail="Enrollment token has expired.")

    secret = decrypt(enrollment.secret_enc) or ""
    expected_token = hmac.new(
        secret.encode("utf-8"),
        b"installer-download",
        hashlib.sha256,
    ).hexdigest()
    if not secret or not hmac.compare_digest(token, expected_token):
        raise HTTPException(status_code=404)

    callback_url = str(
        request.url_for(
            "complete_remote_enrollment",
            enrollment_id=enrollment_id,
        )
    )
    body = _build_enrollment_installer(
        enrollment,
        callback_url,
        secret,
        internal_tls=(
            (get_setting(db, "tls_mode") or "internal").strip().lower()
            == "internal"
        ),
    )
    return Response(
        content=body,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": 'inline; filename="nasitron-enroll.sh"',
            "Cache-Control": "no-store",
            "X-NASitron-Version": APP_VERSION,
        },
    )


@app.post("/api/enroll/{enrollment_id}/complete")
async def complete_remote_enrollment(
    enrollment_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", enrollment_id):
        raise HTTPException(status_code=404)

    enrollment = db.get(RemoteEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404)
    now = datetime.utcnow()
    if enrollment.used_at is not None:
        raise HTTPException(status_code=409, detail="Enrollment token has already been used.")
    if enrollment.expires_at <= now:
        raise HTTPException(status_code=410, detail="Enrollment token has expired.")

    body = await request.body()
    supplied_signature = request.headers.get(
        "X-NASitron-Enrollment-Signature", ""
    ).strip().lower()
    secret = decrypt(enrollment.secret_enc) or ""
    if not secret or not re.fullmatch(r"[0-9a-f]{64}", supplied_signature):
        raise HTTPException(status_code=403, detail="Invalid enrollment proof.")
    expected_signature = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise HTTPException(status_code=403, detail="Invalid enrollment proof.")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid enrollment payload.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid enrollment payload.")

    hostname = bounded_text(
        str(payload.get("hostname") or ""),
        "Remote hostname",
        maximum=120,
    )
    host = validate_host(str(payload.get("host") or ""), "Remote host")
    try:
        raw_port = int(payload.get("port") or 22)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="SSH port must be an integer.")
    port = bounded_int(raw_port, "SSH port", 1, 65535)
    username = bounded_text(
        str(payload.get("username") or "nasitron"),
        "SSH username",
        maximum=120,
    )
    fingerprint = bounded_text(
        str(payload.get("host_key_fingerprint") or ""),
        "SSH host-key fingerprint",
        minimum=0,
        maximum=128,
    )

    private_key = decrypt(enrollment.private_key_enc)
    if not private_key:
        raise HTTPException(status_code=500, detail="Enrollment key is unavailable.")
    try:
        SSHCollector.parse_private_key(private_key, None)
    except CollectorError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    server = db.scalar(
        select(Server).where(
            Server.host == host,
            Server.port == port,
            Server.username == username,
        )
    )
    if server is None:
        name = hostname
        suffix = 2
        while db.scalar(select(Server.id).where(Server.name == name)) is not None:
            tail = f" ({suffix})"
            name = hostname[: max(1, 120 - len(tail))] + tail
            suffix += 1
        server = Server(
            name=name,
            host=host,
            port=port,
            username=username,
            auth_type="key",
            private_key_enc=encrypt(private_key),
            private_key_passphrase_enc=None,
            poll_interval_seconds=60,
            smart_interval_minutes=15,
            sudo_for_smart=True,
            strict_host_key=False,
            host_key_fingerprint=fingerprint or None,
            enabled=True,
        )
        db.add(server)
        db.flush()
    else:
        server.auth_type = "key"
        server.password_enc = None
        server.private_key_enc = encrypt(private_key)
        server.private_key_passphrase_enc = None
        server.sudo_for_smart = True
        server.enabled = True
        if fingerprint:
            server.host_key_fingerprint = fingerprint

    enrollment.used_at = now
    enrollment.server_id = server.id
    db.commit()
    trigger_now(server.id)
    return {
        "status": "registered",
        "server_id": server.id,
        "server_name": server.name,
        "host": server.host,
    }


@app.get("/api/enrollments/{enrollment_id}/status")
def remote_enrollment_status(
    enrollment_id: str,
    db: Session = Depends(get_db),
):
    enrollment = db.get(RemoteEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404)
    if enrollment.used_at is not None and enrollment.server_id is not None:
        enrolled_server = db.get(Server, enrollment.server_id)
        if enrolled_server is None:
            raise HTTPException(404, "Enrolled server was deleted")
        return {
            "status": "complete",
            "server_id": enrollment.server_id,
            "phase": ("reporting" if enrolled_server.last_ok_at else
                      "ssh_error" if enrolled_server.last_error else "verifying"),
        }
    if enrollment.expires_at <= datetime.utcnow():
        return {"status": "expired"}
    return {"status": "pending"}


def _decode_state(row: CurrentState | None) -> dict | None:
    if row is None:
        return None
    try:
        return json.loads(row.payload_json)
    except json.JSONDecodeError:
        return None


def _expected_pools(server: Server) -> set[str]:
    try:
        value = json.loads(server.expected_pools_json or "[]")
    except json.JSONDecodeError:
        value = []
    return {
        item
        for item in value
        if isinstance(item, str) and item
    } if isinstance(value, list) else set()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    server_filter = request.query_params.get("server", "")
    all_servers = db.scalars(select(Server).order_by(Server.name)).all()
    servers = [s for s in all_servers if not server_filter or str(s.id) == server_filter]
    server_ids = [server.id for server in servers]
    state_rows = (
        db.scalars(select(CurrentState).where(CurrentState.server_id.in_(server_ids))).all()
        if server_ids
        else []
    )
    states = {row.server_id: _decode_state(row) for row in state_rows}
    alert_rows = (
        db.scalars(
            select(Alert)
            .where(Alert.server_id.in_(server_ids), Alert.active.is_(True))
            .order_by(Alert.last_seen.desc())
        ).all()
        if server_ids
        else []
    )
    alerts_by_server: dict[int, list[Alert]] = {}
    for alert in alert_rows:
        alerts_by_server.setdefault(alert.server_id, []).append(alert)

    cards = []
    pool_rows = []
    drive_rows = []
    arc_rates = []
    online_servers = 0
    failed_drives = 0
    degraded_pools = 0
    maintenance_server = None

    for server in servers:
        snapshot = states.get(server.id)
        active_alerts = alerts_by_server.get(server.id, [])
        cards.append({"server": server, "snapshot": snapshot, "alerts": active_alerts})

        if server_state(server, snapshot)[0] == "good":
            online_servers += 1
            if "zfs.arc" not in set(
                snapshot.get("collection", {}).get("stale_subsystems", [])
            ):
                value = snapshot.get("arc", {}).get("hit_rate_pct")
                if value is not None:
                    arc_rates.append(float(value))

        if not snapshot:
            continue

        for pool in snapshot.get("pools", []):
            pool_rows.append({"server": server, "pool": pool, "snapshot": snapshot})
            if str(pool.get("health", "")).upper() != "ONLINE":
                degraded_pools += 1
                if maintenance_server is None:
                    maintenance_server = server

        for drive in snapshot.get("drives", []):
            drive_rows.append({"server": server, "drive": drive, "snapshot": snapshot})
            smart = drive.get("smart") or {}
            if not is_zvol(drive) and smart.get("smart_passed") is False:
                failed_drives += 1
                if maintenance_server is None:
                    maintenance_server = server

    recent_alerts = db.scalars(
        select(Alert)
        .options(selectinload(Alert.server))
        .where(Alert.active.is_(True), Alert.server_id.in_(server_ids))
        .order_by((Alert.severity == "critical").desc(), Alert.last_seen.desc())
        .limit(8)
    ).all()
    active_alerts = alert_rows
    critical_alerts = sum(1 for alert in active_alerts if alert.severity == "critical")
    collection_failures = sum(
        1 for server in servers if server.last_collection_state == "failed"
    )
    if critical_alerts or collection_failures or failed_drives:
        overall_status = "critical"
    elif active_alerts or degraded_pools:
        overall_status = "warning"
    elif not cards or any(server_state(c["server"], c["snapshot"])[0] != "good" for c in cards):
        overall_status = "unknown"
    else:
        overall_status = "good"

    summary = {
        "server_count": len(servers),
        "online_servers": online_servers,
        "pool_count": len(pool_rows),
        "healthy_pools": max(0, len(pool_rows) - degraded_pools),
        "drive_count": len(drive_rows),
        "failed_drives": failed_drives,
        "active_alerts": len(active_alerts),
        "critical_alerts": critical_alerts,
        "arc_reporting": len(arc_rates),
        "arc_hit_rate": (sum(arc_rates) / len(arc_rates)) if arc_rates else None,
        "overall_status": overall_status,
    }

    pool_rows.sort(key=lambda row: row["pool"].get("capacity_pct") or -1, reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "cards": cards,
            "summary": summary,
            "all_servers": all_servers,
            "server_filter": server_filter,
            "pool_rows": pool_rows,
            "drive_rows": drive_rows,
            "recent_alerts": recent_alerts,
            "maintenance_server": maintenance_server,
            "capacity_warning": get_int(db, "pool_capacity_warning", 80),
            "capacity_critical": get_int(db, "pool_capacity_critical", 90),
            "app_version": APP_VERSION,
        },
    )


def _inventory_rows(db: Session) -> dict:
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    server_ids = [server.id for server in servers]
    state_rows = (
        db.scalars(select(CurrentState).where(CurrentState.server_id.in_(server_ids))).all()
        if server_ids
        else []
    )
    states = {row.server_id: _decode_state(row) for row in state_rows}
    alert_rows = (
        db.scalars(
            select(Alert)
            .where(Alert.server_id.in_(server_ids), Alert.active.is_(True))
            .order_by(Alert.last_seen.desc())
        ).all()
        if server_ids
        else []
    )
    alerts_by_server: dict[int, list[Alert]] = {}
    for alert in alert_rows:
        alerts_by_server.setdefault(alert.server_id, []).append(alert)

    cards = []
    pool_rows = []
    tank_rows = []
    dataset_rows = []
    drive_rows = []
    inventory_warnings = []
    for server in servers:
        snapshot = states.get(server.id)
        cards.append(
            {
                "server": server,
                "snapshot": snapshot,
                "alerts": alerts_by_server.get(server.id, []),
            }
        )
        if not snapshot:
            continue

        collection = snapshot.get("collection", {})
        stale = set(collection.get("stale_subsystems", []))
        errors = collection.get("errors", [])
        if "drives.inventory" in stale or any(
            error.get("subsystem") == "drives.inventory"
            for error in errors
            if isinstance(error, dict)
        ):
            inventory_warnings.append(server)

        datasets_by_name = {
            str(dataset.get("name")): dataset
            for dataset in snapshot.get("datasets", [])
            if dataset.get("name")
        }
        pool_names = {
            str(pool.get("name"))
            for pool in snapshot.get("pools", [])
            if pool.get("name")
        }
        for dataset in snapshot.get("datasets", []):
            dataset_name = str(dataset.get("name") or "")
            tank_name = dataset_name.split("/", 1)[0] if dataset_name else ""
            dataset_rows.append(
                {
                    "server": server,
                    "snapshot": snapshot,
                    "dataset": dataset,
                    "tank_name": tank_name,
                    "is_root": dataset_name in pool_names,
                }
            )

        for pool in snapshot.get("pools", []):
            pool_rows.append({"server": server, "pool": pool, "snapshot": snapshot})
            pool_name = str(pool.get("name") or "")
            root_dataset = datasets_by_name.get(pool_name)
            tank_rows.append(
                {
                    "server": server,
                    "snapshot": snapshot,
                    "pool": pool,
                    "dataset": root_dataset,
                    "tank_name": pool_name,
                }
            )
        for drive in snapshot.get("drives", []):
            drive_rows.append({"server": server, "drive": drive, "snapshot": snapshot})

    labels = {(r.server_id, r.identity): r.label for r in db.scalars(select(DriveLabel))}
    # Resolve current layout assignments on every request; serials are scoped to
    # their server and no duplicate labels need to be persisted or synchronized.
    bay_locations = {
        (server_id, identity): f"{name} · Bay {slot}"
        for server_id, identity, name, slot in db.execute(
            select(Enclosure.server_id, BayAssignment.identity,
                   Enclosure.name, BayAssignment.slot)
            .join(BayAssignment, BayAssignment.enclosure_id == Enclosure.id)
        )
    }
    for row in drive_rows:
        disk = row["drive"]
        identity = disk.get("serial") or disk.get("path") or ""
        key = (row["server"].id, identity)
        disk["bay_label"] = bay_locations.get(key) or labels.get(key, "")
    pool_rows.sort(
        key=lambda row: (
            row["server"].name.lower(),
            str(row["pool"].get("name", "")).lower(),
        )
    )
    tank_rows.sort(
        key=lambda row: (
            row["server"].name.lower(),
            str(row["tank_name"]).lower(),
        )
    )
    dataset_rows.sort(
        key=lambda row: (
            row["server"].name.lower(),
            str(row["dataset"].get("name", "")).lower(),
        )
    )
    drive_rows.sort(
        key=lambda row: (
            row["server"].name.lower(),
            str(row["drive"].get("path", "")).lower(),
        )
    )
    return {
        "servers": servers,
        "cards": cards,
        "pool_rows": pool_rows,
        "tank_rows": tank_rows,
        "dataset_rows": dataset_rows,
        "drive_rows": drive_rows,
        "inventory_warnings": inventory_warnings,
    }


@app.get("/servers", response_class=HTMLResponse)
def servers_index(request: Request, db: Session = Depends(get_db)):
    data = _inventory_rows(db)
    data.update(capacity_warning=get_int(db,"pool_capacity_warning",80), capacity_critical=get_int(db,"pool_capacity_critical",90))
    return templates.TemplateResponse(
        request=request,
        name="servers.html",
        context={"request": request, **data},
    )


@app.get("/pools", response_class=HTMLResponse)
def pools_index(request: Request, db: Session = Depends(get_db)):
    data = _inventory_rows(db)
    for row in data["pool_rows"]:
        row["topology"] = pool_topology(row["pool"], row["snapshot"].get("drives", []))
        row["capacity_rows"] = capacity_rows(row["pool"], row["topology"])
        attach_capacity(row["topology"], row["capacity_rows"])
        pool_name = row["pool"]["name"]
        row["pool_datasets"] = [d for d in row["snapshot"].get("datasets", [])
                                if d.get("name") == pool_name or d.get("name", "").startswith(pool_name + "/")]
        status = row["pool"].get("status") or {}
        row["status_sections"] = status.get("sections") or parse_status_sections(status.get("raw", ""))
        if not row["status_sections"]:
            row["status_sections"] = [{"key": key, "text": status.get(key)}
                                      for key in ("scan", "errors") if status.get(key)]
        row["tab_key"] = f"pool-{row['server'].id}-" + str(row["pool"].get("name", "")).encode().hex()
    degraded = sum(
        1
        for row in data["pool_rows"]
        if str(row["pool"].get("health", "")).upper() != "ONLINE"
    )
    return templates.TemplateResponse(
        request=request,
        name="pools.html",
        context={
            "request": request,
            **data,
            "degraded_pools": degraded,
            "healthy_pools": max(0, len(data["pool_rows"]) - degraded),
        },
    )


@app.get("/tanks", response_class=HTMLResponse)
def tanks_index(request: Request, db: Session = Depends(get_db)):
    data = _inventory_rows(db)
    healthy_tanks = sum(
        1
        for row in data["tank_rows"]
        if str(row["pool"].get("health", "")).upper() == "ONLINE"
    )
    configured_properties = sum(
        len((row["dataset"] or {}).get("properties", []))
        + sum(
            1
            for prop in row["pool"].get("properties", [])
            if prop.get("is_set")
        )
        for row in data["tank_rows"]
    )
    compression_ratios = [
        float(row["pool"].get("compression_ratio"))
        for row in data["tank_rows"]
        if row["pool"].get("compression_ratio") is not None
    ]
    average_compression_ratio = (
        sum(compression_ratios) / len(compression_ratios)
        if compression_ratios
        else None
    )
    return templates.TemplateResponse(
        request=request,
        name="tanks.html",
        context={
            "request": request,
            **data,
            "healthy_tanks": healthy_tanks,
            "unhealthy_tanks": max(0, len(data["tank_rows"]) - healthy_tanks),
            "configured_tank_properties": configured_properties,
            "average_compression_ratio": average_compression_ratio,
        },
    )


@app.get("/datasets", response_class=HTMLResponse)
def datasets_index(request: Request, db: Session = Depends(get_db)):
    data = _inventory_rows(db)
    filesystem_count = 0
    volume_count = 0
    root_count = 0
    configured_properties = 0
    total_used = 0
    total_logical_used = 0
    total_snapshot_used = 0
    compression_ratios: list[float] = []

    for row in data["dataset_rows"]:
        dataset = row["dataset"]
        dataset_type = str(dataset.get("type") or "").lower()
        if dataset_type == "filesystem":
            filesystem_count += 1
        elif dataset_type == "volume":
            volume_count += 1
        if row["is_root"]:
            root_count += 1

        configured_properties += len(dataset.get("properties", []))
        total_used += int(dataset.get("used_bytes") or 0)
        total_logical_used += int(dataset.get("logical_used_bytes") or 0)
        total_snapshot_used += int(dataset.get("snapshot_used_bytes") or 0)
        if dataset.get("compression_ratio") is not None:
            compression_ratios.append(float(dataset["compression_ratio"]))

    return templates.TemplateResponse(
        request=request,
        name="datasets.html",
        context={
            "request": request,
            **data,
            "filesystem_count": filesystem_count,
            "volume_count": volume_count,
            "root_dataset_count": root_count,
            "configured_dataset_properties": configured_properties,
            "total_dataset_used": total_used,
            "total_logical_used": total_logical_used,
            "total_snapshot_used": total_snapshot_used,
            "average_dataset_compression": (
                sum(compression_ratios) / len(compression_ratios)
                if compression_ratios
                else None
            ),
        },
    )


@app.get("/drives", response_class=HTMLResponse)
def drives_index(request: Request, db: Session = Depends(get_db)):
    data = _inventory_rows(db)
    failed = 0
    unknown = 0
    assigned = 0
    for row in data["drive_rows"]:
        drive = row["drive"]
        if is_zvol(drive):
            continue
        smart = drive.get("smart") or {}
        if smart.get("smart_passed") is False:
            failed += 1
        elif smart.get("smart_passed") is not True:
            unknown += 1
        if drive.get("zfs_memberships"):
            assigned += 1

    return templates.TemplateResponse(
        request=request,
        name="drives.html",
        context={
            "request": request,
            **data,
            "physical_drive_count": sum(not is_zvol(r["drive"]) for r in data["drive_rows"]),
            "failed_drives": failed,
            "unknown_smart": unknown,
            "assigned_drives": assigned,
            "unassigned_drives": max(0, sum(not is_zvol(r["drive"]) for r in data["drive_rows"]) - assigned),
        },
    )


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance_index(request: Request, db: Session = Depends(get_db)):
    data = _inventory_rows(db)
    maintenance_rows = []
    for card in data["cards"]:
        snapshot = card["snapshot"]
        unhealthy_pools = []
        failed_drives = []
        if snapshot:
            unhealthy_pools = [
                pool
                for pool in snapshot.get("pools", [])
                if str(pool.get("health", "")).upper() != "ONLINE"
            ]
            failed_drives = [
                drive
                for drive in snapshot.get("drives", [])
                if (drive.get("smart") or {}).get("smart_passed") is False
            ]
        maintenance_rows.append(
            {
                **card,
                "unhealthy_pools": unhealthy_pools,
                "failed_drives": failed_drives,
            }
        )

    actions = db.scalars(
        select(MaintenanceAction)
        .options(selectinload(MaintenanceAction.server))
        .order_by(MaintenanceAction.created_at.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="maintenance.html",
        context={
            "request": request,
            **data,
            "maintenance_rows": maintenance_rows,
            "actions": actions,
        },
    )


@app.get("/servers/new", response_class=HTMLResponse)
def new_server(request: Request, db: Session = Depends(get_db)):
    tls_mode = (get_setting(db, "tls_mode") or "internal").strip().lower()
    tls_domain = (get_setting(db, "tls_domain") or "").strip()
    enrollment_id = secrets.token_urlsafe(18)
    enrollment_secret = secrets.token_urlsafe(32)
    private_key_obj = Ed25519PrivateKey.generate()
    private_key = private_key_obj.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_key = private_key_obj.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("utf-8")
    public_key += f" nasitron-enrollment-{enrollment_id}"

    enrollment = RemoteEnrollment(
        id=enrollment_id,
        secret_enc=encrypt(enrollment_secret) or "",
        public_key=public_key,
        private_key_enc=encrypt(private_key) or "",
        expires_at=datetime.utcnow() + timedelta(minutes=30),
    )
    db.add(enrollment)
    db.commit()

    download_token = hmac.new(
        enrollment_secret.encode("utf-8"),
        b"installer-download",
        hashlib.sha256,
    ).hexdigest()
    enrollment_installer_url = str(
        request.url_for(
            "download_enrollment_installer",
            enrollment_id=enrollment_id,
        )
    ) + "?token=" + download_token
    installer_url_shell = shlex.quote(enrollment_installer_url)

    callback_url = str(
        request.url_for(
            "complete_remote_enrollment",
            enrollment_id=enrollment_id,
        )
    )
    enrollment_script = _build_enrollment_installer(
        enrollment,
        callback_url,
        enrollment_secret,
        internal_tls=(tls_mode == "internal"),
    )
    enrollment_script_sha256 = hashlib.sha256(
        enrollment_script.encode("utf-8")
    ).hexdigest()

    if tls_mode == "internal":
        installer_command = (
            '(tmp="$(mktemp)" && '
            f'curl -kfsSL {installer_url_shell} -o "$tmp" && '
            f"printf '%s  %s\\n' {shlex.quote(enrollment_script_sha256)} \"$tmp\" "
            '| sha256sum -c - && sudo bash "$tmp"; '
            'rc=$?; rm -f "$tmp"; exit "$rc")'
        )
    else:
        installer_command = f"curl -fsSL {installer_url_shell} | sudo bash"
    return templates.TemplateResponse(
        request=request,
        name="server_form.html",
        context={
            "request": request,
            "server": None,
            "installer_url": enrollment_installer_url,
            "installer_url_shell": installer_url_shell,
            "installer_sha256": enrollment_script_sha256,
            "enrollment_script_sha256": enrollment_script_sha256,
            "installer_command": installer_command,
            "installer_tls_mode": tls_mode,
            "installer_tls_domain": tls_domain,
            "enrollment_id": enrollment_id,
            "enrollment_expires_minutes": 30,
        },
    )


def _validated_server_fields(
    *,
    name: str,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    password: str,
    private_key: str,
    private_key_passphrase: str,
    poll_interval_seconds: int,
    smart_interval_minutes: int,
) -> dict:
    clean_name = bounded_text(name, "Server name", maximum=120)
    clean_host = validate_host(host)
    clean_port = bounded_int(port, "SSH port", 1, 65535)
    clean_user = bounded_text(username, "SSH username", maximum=120)
    if auth_type not in {"key", "password"}:
        bad_request("Authentication type must be key or password.")
    clean_password = bounded_secret(password, "SSH password", maximum=4096)
    clean_key = bounded_multiline(private_key, "SSH private key", maximum=65536)
    clean_passphrase = bounded_secret(
        private_key_passphrase,
        "SSH private-key passphrase",
        maximum=4096,
    )
    poll = bounded_int(poll_interval_seconds, "Poll interval", 15, 86400)
    smart = bounded_int(smart_interval_minutes, "SMART interval", 1, 1440)
    if clean_key:
        try:
            SSHCollector.parse_private_key(clean_key, clean_passphrase or None)
        except CollectorError as exc:
            bad_request(str(exc))
    return {
        "name": clean_name,
        "host": clean_host,
        "port": clean_port,
        "username": clean_user,
        "auth_type": auth_type,
        "password": clean_password,
        "private_key": clean_key,
        "private_key_passphrase": clean_passphrase,
        "poll_interval_seconds": poll,
        "smart_interval_minutes": smart,
    }


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
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    fields = _validated_server_fields(
        name=name,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        password=password,
        private_key=private_key,
        private_key_passphrase=private_key_passphrase,
        poll_interval_seconds=poll_interval_seconds,
        smart_interval_minutes=smart_interval_minutes,
    )
    if db.scalar(select(Server.id).where(Server.name == fields["name"])) is not None:
        bad_request("A server with this name already exists.")
    if fields["auth_type"] == "key" and not fields["private_key"]:
        bad_request("A private key is required for key authentication.")
    if fields["auth_type"] == "password" and not fields["password"]:
        bad_request("A password is required for password authentication.")

    server = Server(
        name=fields["name"],
        host=fields["host"],
        port=fields["port"],
        username=fields["username"],
        auth_type=fields["auth_type"],
        password_enc=encrypt(fields["password"]) if fields["auth_type"] == "password" else None,
        private_key_enc=encrypt(fields["private_key"]) if fields["auth_type"] == "key" else None,
        private_key_passphrase_enc=(
            encrypt(fields["private_key_passphrase"])
            if fields["auth_type"] == "key"
            else None
        ),
        poll_interval_seconds=fields["poll_interval_seconds"],
        smart_interval_minutes=fields["smart_interval_minutes"],
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
    return templates.TemplateResponse(
        request=request,
        name="server_form.html",
        context={"request": request, "server": server},
    )


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
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)

    fields = _validated_server_fields(
        name=name,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        password=password,
        private_key=private_key,
        private_key_passphrase=private_key_passphrase,
        poll_interval_seconds=poll_interval_seconds,
        smart_interval_minutes=smart_interval_minutes,
    )
    duplicate = db.scalar(
        select(Server.id).where(
            Server.name == fields["name"],
            Server.id != server.id,
        )
    )
    if duplicate is not None:
        bad_request("A server with this name already exists.")

    host_changed = server.host != fields["host"] or server.port != fields["port"]

    if fields["auth_type"] == "key":
        if not fields["private_key"] and not server.private_key_enc:
            bad_request("A private key is required for key authentication.")
        server.password_enc = None
        if fields["private_key"]:
            server.private_key_enc = encrypt(fields["private_key"])
            server.private_key_passphrase_enc = encrypt(
                fields["private_key_passphrase"]
            )
        elif fields["private_key_passphrase"]:
            bad_request("A passphrase cannot be changed without supplying the private key.")
    else:
        if not fields["password"] and not server.password_enc:
            bad_request("A password is required for password authentication.")
        server.private_key_enc = None
        server.private_key_passphrase_enc = None
        if fields["password"]:
            server.password_enc = encrypt(fields["password"])

    server.name = fields["name"]
    server.host = fields["host"]
    server.port = fields["port"]
    server.username = fields["username"]
    server.auth_type = fields["auth_type"]
    server.poll_interval_seconds = fields["poll_interval_seconds"]
    server.smart_interval_minutes = fields["smart_interval_minutes"]
    server.sudo_for_smart = sudo_for_smart
    server.strict_host_key = strict_host_key
    server.enabled = enabled
    if host_changed:
        server.host_key_fingerprint = None
        server.zpool_status_json_supported = None
    db.commit()
    invalidate_inventory_cache(server.id)
    if enabled:
        trigger_now(server.id)
    return RedirectResponse(f"/servers/{server.id}", status_code=303)


@app.get("/servers/{server_id}/host-key", response_class=HTMLResponse)
def host_key_page(server_id: int, request: Request, db: Session = Depends(get_db)):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)
    candidate = None
    error = None
    try:
        candidate = SSHCollector.fetch_host_key(server)
    except Exception as exc:
        error = str(exc)
    return templates.TemplateResponse(
        request=request,
        name="host_key.html",
        context={
            "request": request,
            "server": server,
            "candidate": candidate,
            "error": error,
        },
    )


@app.post("/servers/{server_id}/host-key")
def enroll_host_key(
    server_id: int,
    fingerprint: str = Form(...),
    confirm_text: str = Form(...),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)
    clean_fp = bounded_text(fingerprint, "Fingerprint", maximum=128)
    clean_confirm = bounded_text(confirm_text, "Fingerprint confirmation", maximum=128)
    if clean_confirm != clean_fp:
        bad_request("Fingerprint confirmation does not match.")
    try:
        candidate = SSHCollector.enroll_host_key(server, clean_fp)
    except CollectorError as exc:
        bad_request(str(exc))
    server.host_key_fingerprint = candidate["fingerprint"]
    server.strict_host_key = True
    db.commit()
    trigger_now(server.id)
    return RedirectResponse(f"/servers/{server.id}", status_code=303)


@app.post("/servers/{server_id}/delete")
def delete_server(
    server_id: int,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    server = db.get(Server, server_id)
    if server:
        invalidate_inventory_cache(server.id)
        db.delete(server)
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/servers/{server_id}/poll")
def poll_server(
    server_id: int,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
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
        select(Alert)
        .where(Alert.server_id == server_id)
        .order_by(Alert.active.desc(), Alert.last_seen.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="server.html",
        context={
            "request": request,
            "server": server,
            "snapshot": snapshot,
            "alerts": alerts,
            "expected_pools": sorted(_expected_pools(server)),
        },
    )


@app.post("/servers/{server_id}/expected-pools/forget")
def forget_expected_pool(
    server_id: int,
    pool_name: str = Form(...),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)
    pool = bounded_text(pool_name, "Pool name", maximum=255)
    expected = _expected_pools(server)
    if pool not in expected:
        bad_request("That pool is not in the expected-pool inventory.")
    expected.remove(pool)
    server.expected_pools_json = json.dumps(sorted(expected))

    state = db.get(CurrentState, server.id)
    if state:
        current = _decode_state(state)
        if current:
            collection = current.setdefault("collection", {})
            collection["expected_pools"] = sorted(expected)
            collection["missing_pools"] = [
                item for item in collection.get("missing_pools", []) if item != pool
            ]
            state.payload_json = json.dumps(current, separators=(",", ":"))

    now = datetime.utcnow()
    for alert in db.scalars(
        select(Alert).where(Alert.server_id == server.id, Alert.active.is_(True))
    ).all():
        if (
            alert.key == f"pool.missing:{pool}"
            or alert.key == f"pool.health:{pool}"
            or alert.key == f"pool.capacity:{pool}"
            or alert.key == f"pool.scrub_age:{pool}"
            or alert.key.startswith(f"vdev.errors:{pool}:")
            or alert.key.startswith(f"vdev.state:{pool}:")
        ):
            alert.active = False
            alert.resolved_at = now
            alert.next_notification_at = None
    db.commit()
    return RedirectResponse(f"/servers/{server.id}", status_code=303)


@app.get("/servers/{server_id}/replace-drive", response_class=HTMLResponse)
def replace_drive_page(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_secure_maintenance(request)
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)

    inventory = {
        "failed": [],
        "candidates": [],
        "rejected_candidates": [],
        "statuses": {},
    }
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
        request=request,
        name="replace_drive.html",
        context={
            "request": request,
            "server": server,
            "inventory": inventory,
            "inventory_error": inventory_error,
            "actions": actions,
        },
    )


@app.post("/servers/{server_id}/replace-drive")
def perform_drive_replacement(
    server_id: int,
    request: Request,
    pool: str = Form(...),
    old_guid: str = Form(...),
    old_device: str = Form(...),
    new_device: str = Form(...),
    confirm_text: str = Form(...),
    allow_conflicting_operation: bool = Form(False),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    require_secure_maintenance(request)
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)

    clean_pool = bounded_text(pool, "Pool", maximum=255)
    clean_guid = bounded_text(old_guid, "Vdev GUID", maximum=32)
    if not clean_guid.isdigit():
        bad_request("Vdev GUID must be numeric.")
    clean_old = bounded_text(old_device, "Failed device", maximum=1024)
    clean_new = bounded_text(new_device, "Replacement device", maximum=1024)
    clean_confirm = bounded_text(confirm_text, "Confirmation text", maximum=300)

    expected = f"REPLACE {clean_pool}"
    if clean_confirm != expected:
        message = f"Confirmation text did not match. Type exactly: {expected}"
        return RedirectResponse(
            f"/servers/{server_id}/replace-drive?message=" + quote(message),
            status_code=303,
        )

    request_data = ReplacementRequest(
        pool=clean_pool,
        old_guid=clean_guid,
        old_device=clean_old,
        new_device=clean_new,
        allow_conflicting_operation=allow_conflicting_operation,
    )

    try:
        result = replace_drive(server, request_data)
        output = "\n".join(
            part
            for part in [
                result.get("stdout", ""),
                result.get("stderr", ""),
                result.get("pool_status", ""),
            ]
            if part
        )
        db.add(
            MaintenanceAction(
                server_id=server.id,
                action="zpool_replace",
                actor=_current_user(request).username,
                state="accepted" if result.get("ok") else "failed",
                pool=request_data.pool,
                old_device=f"{request_data.old_device} [guid={request_data.old_guid}]",
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
            detail = (
                result.get("stderr")
                or result.get("stdout")
                or "zpool replace failed"
            ).strip()
            message = f"Replacement failed: {detail[:1500]}"
    except Exception as exc:
        db.add(
            MaintenanceAction(
                server_id=server.id,
                action="zpool_replace",
                actor=_current_user(request).username,
                state="failed",
                pool=request_data.pool,
                old_device=f"{request_data.old_device} [guid={request_data.old_guid}]",
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
    scope: str = Query("", max_length=255),
    hours: float = Query(24, ge=0.25, le=24 * 365),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    db: Session = Depends(get_db),
):
    if name not in METRIC_NAMES:
        raise HTTPException(status_code=400, detail="Unknown metric name.")
    if db.get(Server, server_id) is None:
        raise HTTPException(404)

    if (start is None) != (end is None):
        raise HTTPException(400, "Provide both start and end.")
    until = datetime.utcnow()
    since = until - timedelta(hours=hours)
    if start is not None and end is not None:
        if start.tzinfo is None or end.tzinfo is None:
            raise HTTPException(400, "Start and end must include a timezone offset.")
        since = start.astimezone(timezone.utc).replace(tzinfo=None)
        until = end.astimezone(timezone.utc).replace(tzinfo=None)
        if not timedelta(0) < until - since <= timedelta(days=365):
            raise HTTPException(400, "Choose an end after start and a range of at most 365 days.")
    from .history import history_series
    return history_series(db, db.get(Server, server_id), [name], [scope], since, until)[0]


@app.get("/api/servers/{server_id}/metrics/batch")
def metric_batch(server_id: int, names: list[str] = Query([]), scopes: list[str] = Query([]), pairs: list[str] = Query([]),
                 hours: float = Query(24, ge=0.25, le=8760), start: datetime | None = Query(None),
                 end: datetime | None = Query(None), db: Session = Depends(get_db)):
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(404)
    selected_pairs=None
    if pairs:
        try:
            decoded = [json.loads(p) if len(p) <= 512 else None for p in pairs]
            if any(not isinstance(p, list) for p in decoded):
                raise ValueError()
            selected_pairs = [tuple(p) for p in decoded]
            if not 1<=len(selected_pairs)<=32 or any(len(p)!=2 or not all(isinstance(v,str) for v in p) or p[0] not in METRIC_NAMES or len(p[1])>255 for p in selected_pairs):
                raise ValueError()
            if len({p[1] for p in selected_pairs}) > 4:
                raise ValueError()
        except (ValueError,TypeError):
            raise HTTPException(400,"Select known metric/drive pairs for up to four drives.")
        names = list(dict.fromkeys(p[0] for p in selected_pairs))
        scopes = list(dict.fromkeys(p[1] for p in selected_pairs))
    elif not 1 <= len(names) <= 8 or not 1 <= len(scopes) <= 4 or any(n not in METRIC_NAMES for n in names) or any(len(s)>255 for s in scopes):
        raise HTTPException(400, "Select up to eight known metrics and four drives.")
    until = datetime.utcnow()
    since = until - timedelta(hours=hours)
    if (start is None) != (end is None):
        raise HTTPException(400, "Provide both start and end.")
    if start is not None:
        if start.tzinfo is None or end.tzinfo is None:
            raise HTTPException(400, "Start and end must include a timezone offset.")
        since = start.astimezone(timezone.utc).replace(tzinfo=None)
        until = end.astimezone(timezone.utc).replace(tzinfo=None)
        if not timedelta(0) < until-since <= timedelta(days=365):
            raise HTTPException(400, "Choose a range of at most 365 days.")
    from .history import history_series
    return {"series": history_series(db, server, list(dict.fromkeys(names)), list(dict.fromkeys(scopes)), since, until, pairs=selected_pairs)}


@app.post("/servers/{server_id}/support-bundle")
def support_bundle(
    server_id: int,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404)

    with support_bundle_lock(server_id):
        with SSHCollector(server) as collector:
            raw = collector.deep_collect()
        diagnostics = sanitize_diagnostics(raw)
        payload = {
            "format": "nasitron-support-bundle",
            "format_version": 3,
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
            "latest_snapshot": {k: v for k, v in (latest_snapshot(db, server_id) or {}).items() if not k.startswith("_")},
            "diagnostics": diagnostics,
            "privacy_note": (
                "SSH/SMTP credentials are excluded. Sensitive ZFS native values "
                "and all administrator-defined ZFS user-property values are redacted."
            ),
        }

        handle = tempfile.NamedTemporaryFile(
            prefix="nasitron-bundle-",
            suffix=".json.gz",
            delete=False,
        )
        temp_path = handle.name
        handle.close()
        try:
            with gzip.open(temp_path, "wt", encoding="utf-8", compresslevel=6) as gz:
                json.dump(payload, gz, indent=2, default=str)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", server.name).strip("-") or f"server-{server.id}"
    filename = f"nasitron-{safe}-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    return FileResponse(
        temp_path,
        media_type="application/gzip",
        filename=filename,
        background=BackgroundTask(os.unlink, temp_path),
    )


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: Session = Depends(get_db)):
    q = request.query_params.get("q", "")[:200]
    state = request.query_params.get("state", "active")
    severity = request.query_params.get("severity", "")
    server_id = request.query_params.get("server", "")
    try:
        page = max(1, min(100000, int(request.query_params.get("page", "1"))))
    except ValueError:
        page = 1
    query = select(Alert)
    if state == "active":
        query = query.where(Alert.active.is_(True))
    elif state == "resolved":
        query = query.where(Alert.active.is_(False))
    elif state == "acknowledged":
        query = query.where(Alert.acknowledged.is_(True))
    if severity in {"critical", "warning", "info"}:
        query = query.where(Alert.severity == severity)
    if server_id.isdigit():
        query = query.where(Alert.server_id == int(server_id))
    if q:
        query = query.where(or_(Alert.title.contains(q, autoescape=True), Alert.message.contains(q, autoescape=True)))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    alerts = db.scalars(query.options(selectinload(Alert.server)).order_by(
        Alert.active.desc(), Alert.last_seen.desc(), Alert.id.desc()).offset((page-1)*50).limit(50)).all()
    return templates.TemplateResponse(request=request, name="alerts.html", context={
        "alerts": alerts, "page": page, "total": total, "pages": max(1, (total+49)//50),
        "servers": db.scalars(select(Server).order_by(Server.name)).all(),
        "q": q, "state": state, "severity": severity, "server_filter": server_id,
    })


@app.post("/alerts/{alert_id}/ack")
def acknowledge_alert(
    alert_id: int,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    alert = db.get(Alert, alert_id)
    if alert:
        alert.acknowledged = True
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    users = db.scalars(select(WebUser).order_by(WebUser.username)).all()
    enabled_admins = db.scalar(
        select(func.count(WebUser.id)).where(
            WebUser.is_admin.is_(True),
            WebUser.enabled.is_(True),
        )
    ) or 0
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "request": request,
            "users": users,
            "enabled_admins": enabled_admins,
        },
    )


@app.get("/users/new", response_class=HTMLResponse)
def new_user_page(request: Request):
    _require_admin(request)
    return templates.TemplateResponse(
        request=request,
        name="user_form.html",
        context={
            "request": request,
            "user": None,
            "error": None,
        },
    )


@app.post("/users/new")
def create_web_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    is_admin: bool = Form(False),
    role: str = Form("viewer"),
    enabled: bool = Form(False),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    _require_admin(request)
    if role not in {"viewer", "operator", "admin"}:
        bad_request("Invalid role")
    is_admin = is_admin or role == "admin"
    clean_username = _clean_web_username(username)
    clean_password = _validate_web_password(password)
    if clean_password != password_confirm:
        bad_request("Password confirmation does not match.")
    if db.scalar(select(WebUser.id).where(WebUser.username == clean_username)) is not None:
        bad_request("A user with that username already exists.")

    user = WebUser(
        username=clean_username,
        password_hash=hash_password(clean_password),
        is_admin=is_admin,
        role=role,
        enabled=enabled,
        session_version=1,
    )
    db.add(user)
    db.commit()
    return RedirectResponse(
        "/users?message=" + quote(f"User {clean_username} created."),
        status_code=303,
    )


@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_page(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    _require_admin(request)
    user = db.get(WebUser, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="user_form.html",
        context={
            "request": request,
            "user": user,
            "error": None,
        },
    )


@app.post("/users/{user_id}/edit")
def update_web_user(
    user_id: int,
    request: Request,
    username: str = Form(...),
    password: str = Form(""),
    password_confirm: str = Form(""),
    is_admin: bool = Form(False),
    role: str = Form("viewer"),
    enabled: bool = Form(False),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    current = _require_admin(request)
    user = db.get(WebUser, user_id)
    if user is None:
        raise HTTPException(status_code=404)

    if role not in {"viewer", "operator", "admin"}:
        bad_request("Invalid role")
    is_admin = is_admin or role == "admin"
    clean_username = _clean_web_username(username)
    duplicate = db.scalar(
        select(WebUser.id).where(
            WebUser.username == clean_username,
            WebUser.id != user.id,
        )
    )
    if duplicate is not None:
        bad_request("A user with that username already exists.")

    if user.id == current.id and not enabled:
        bad_request("You cannot disable your own account.")
    if user.id == current.id and not is_admin:
        bad_request("You cannot remove your own administrator role.")

    if user.is_admin and user.enabled and (not is_admin or not enabled):
        enabled_admins = db.scalar(
            select(func.count(WebUser.id)).where(
                WebUser.is_admin.is_(True),
                WebUser.enabled.is_(True),
            )
        ) or 0
        if enabled_admins <= 1:
            bad_request("NASitron must retain at least one enabled administrator.")

    security_changed = (
        user.username != clean_username
        or user.role != role
        or user.is_admin != is_admin
        or user.enabled != enabled
    )

    user.username = clean_username
    user.role = role
    user.is_admin = is_admin
    user.enabled = enabled

    if password or password_confirm:
        if password != password_confirm:
            bad_request("Password confirmation does not match.")
        clean_password = _validate_web_password(password, "New password")
        user.password_hash = hash_password(clean_password)
        security_changed = True

    if security_changed:
        user.session_version = int(user.session_version or 0) + 1

    db.commit()

    if user.id == current.id and security_changed:
        response = RedirectResponse(
            "/login?message=" + quote("Account updated. Sign in again."),
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    return RedirectResponse(
        "/users?message=" + quote(f"User {clean_username} updated."),
        status_code=303,
    )


@app.post("/users/{user_id}/delete")
def delete_web_user(
    user_id: int,
    request: Request,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    current = _require_admin(request)
    user = db.get(WebUser, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    if user.id == current.id:
        bad_request("You cannot delete your own account.")

    if user.is_admin and user.enabled:
        enabled_admins = db.scalar(
            select(func.count(WebUser.id)).where(
                WebUser.is_admin.is_(True),
                WebUser.enabled.is_(True),
            )
        ) or 0
        if enabled_admins <= 1:
            bad_request("NASitron must retain at least one enabled administrator.")

    username = user.username
    db.delete(user)
    db.commit()
    return RedirectResponse(
        "/users?message=" + quote(f"User {username} deleted."),
        status_code=303,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    keys = [
        "smtp_enabled",
        "temperature_unit",
        "email_transport",
        "mailjet_api_url",
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_from",
        "smtp_to",
        "smtp_starttls",
        "smtp_ssl",
        "pool_capacity_warning",
        "pool_capacity_critical",
        "drive_temp_warning_c",
        "drive_temp_critical_c",
        "nvme_percentage_used_warning",
        "nvme_percentage_used_critical",
        "scrub_age_warning_days",
        "collection_failure_threshold",
        "metric_retention_days",
        "snapshot_retention_days",
        "full_snapshot_interval_minutes",
        "tls_mode",
        "tls_domain",
        "tls_acme_email",
        "tls_acme_ca",
    ]
    values = get_many(db, keys)
    values["smtp_password_configured"] = bool(get_setting(db, "smtp_password"))
    values["mailjet_api_key_configured"] = bool(get_setting(db, "mailjet_api_key"))
    values["mailjet_secret_key_configured"] = bool(get_setting(db, "mailjet_secret_key"))
    values["tls_status"] = manual_certificate_status()
    active_tab = request.query_params.get("tab", "smtp").strip().lower()
    if active_tab not in {"smtp", "health", "history", "enrollment", "tls", "display"}:
        active_tab = "smtp"
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "values": values,
            "active_tab": active_tab,
        },
    )


@app.post("/settings")
def save_settings(
    section: str = Form("all"),
    temperature_unit: str | None = Form(None),
    threshold_temperature_unit: str = Form("C"),
    drive_temp_warning: float | None = Form(None),
    drive_temp_critical: float | None = Form(None),
    smtp_enabled: bool = Form(False),
    email_transport: str = Form("smtp"),
    mailjet_api_url: str = Form("https://api.mailjet.com/v3.1/send"),
    mailjet_api_key: str = Form(""),
    mailjet_secret_key: str = Form(""),
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
    nvme_percentage_used_warning: int = Form(80),
    nvme_percentage_used_critical: int = Form(95),
    scrub_age_warning_days: int = Form(35),
    collection_failure_threshold: int = Form(2),
    metric_retention_days: int = Form(90),
    snapshot_retention_days: int = Form(30),
    full_snapshot_interval_minutes: int = Form(15),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    section = section.strip().lower()
    if section not in {"all", "smtp", "health", "history", "display"}:
        bad_request("Unknown settings section.")

    saved_sections: list[str] = []
    if section in {"all", "display"} and (temperature_unit is not None or section == "display"):
        if temperature_unit not in {"C", "F"}:
            bad_request("Temperature unit must be C or F.")
        set_setting(db, "temperature_unit", temperature_unit)
        saved_sections.append("Display")

    if section in {"all", "smtp"}:
        transport = email_transport.strip().lower()
        if transport not in {"smtp", "mailjet"}:
            bad_request("Email transport must be SMTP or Mailjet.")

        if smtp_ssl and smtp_starttls:
            bad_request("SMTP implicit TLS and STARTTLS cannot both be enabled.")

        clean_smtp_host = smtp_host.strip()
        if clean_smtp_host:
            clean_smtp_host = validate_host(clean_smtp_host, "SMTP host")
        clean_smtp_port = bounded_int(smtp_port, "SMTP port", 1, 65535)
        clean_smtp_user = bounded_text(
            smtp_username,
            "SMTP username",
            minimum=0,
            maximum=320,
        )
        clean_smtp_password = bounded_secret(
            smtp_password,
            "SMTP password",
            maximum=4096,
        )

        clean_from = smtp_from.strip()
        if clean_from:
            clean_from = validate_email(clean_from, "From address")
        clean_to = validate_recipient_list(smtp_to) if smtp_to.strip() else ""

        clean_mailjet_url = _validate_mailjet_api_url(mailjet_api_url)
        clean_mailjet_api_key = bounded_secret(
            mailjet_api_key,
            "Mailjet API key",
            maximum=1024,
        )
        clean_mailjet_secret_key = bounded_secret(
            mailjet_secret_key,
            "Mailjet secret key",
            maximum=4096,
        )
        stored_mailjet_api_key = get_setting(db, "mailjet_api_key")
        stored_mailjet_secret_key = get_setting(db, "mailjet_secret_key")

        if smtp_enabled and (not clean_from or not clean_to):
            bad_request(
                "From address and at least one recipient are required when email alerts are enabled."
            )
        if smtp_enabled and transport == "smtp" and not clean_smtp_host:
            bad_request("SMTP host is required when SMTP is selected.")
        if smtp_enabled and transport == "mailjet":
            if not (clean_mailjet_api_key or stored_mailjet_api_key):
                bad_request("Mailjet API key is required when Mailjet is selected.")
            if not (clean_mailjet_secret_key or stored_mailjet_secret_key):
                bad_request("Mailjet secret key is required when Mailjet is selected.")

        email_values = {
            "smtp_enabled": str(smtp_enabled).lower(),
            "email_transport": transport,
            "mailjet_api_url": clean_mailjet_url,
            "smtp_host": clean_smtp_host,
            "smtp_port": str(clean_smtp_port),
            "smtp_username": clean_smtp_user,
            "smtp_from": clean_from,
            "smtp_to": clean_to,
            "smtp_starttls": str(smtp_starttls).lower(),
            "smtp_ssl": str(smtp_ssl).lower(),
        }
        for key, value in email_values.items():
            set_setting(db, key, value)

        if clean_smtp_password:
            set_setting(db, "smtp_password", clean_smtp_password, secret=True)
        if clean_mailjet_api_key:
            set_setting(db, "mailjet_api_key", clean_mailjet_api_key, secret=True)
        if clean_mailjet_secret_key:
            set_setting(db, "mailjet_secret_key", clean_mailjet_secret_key, secret=True)
        saved_sections.append("Email")

    if section in {"all", "health"}:
        warn_cap, crit_cap = validate_threshold_pair(
            pool_capacity_warning, pool_capacity_critical, "Pool capacity", 1, 100
        )
        if threshold_temperature_unit not in {"C", "F"}:
            bad_request("Temperature unit must be C or F.")
        if (drive_temp_warning is None) != (drive_temp_critical is None):
            bad_request("Both temperature thresholds are required.")
        if drive_temp_warning is not None:
            warn_temp, crit_temp = drive_temp_warning, drive_temp_critical
            if threshold_temperature_unit == "F":
                warn_temp, crit_temp = (round((v - 32) * 5 / 9, 10) for v in (warn_temp, crit_temp))
        else:
            warn_temp, crit_temp = drive_temp_warning_c, drive_temp_critical_c
        if not all(math.isfinite(v) and 1 <= v <= 150 for v in (warn_temp, crit_temp)) or warn_temp >= crit_temp:
            bad_request("Temperature thresholds must be between 1°C and 150°C (33.8°F–302°F), with warning below critical.")
        warn_nvme, crit_nvme = validate_threshold_pair(
            nvme_percentage_used_warning,
            nvme_percentage_used_critical,
            "NVMe percentage used",
            1,
            100,
        )
        scrub_days = bounded_int(scrub_age_warning_days, "Scrub age", 0, 3650)
        fail_threshold = bounded_int(
            collection_failure_threshold, "Collection failure threshold", 1, 100
        )

        health_values = {
            "pool_capacity_warning": str(warn_cap),
            "pool_capacity_critical": str(crit_cap),
            "drive_temp_warning_c": str(warn_temp),
            "drive_temp_critical_c": str(crit_temp),
            "nvme_percentage_used_warning": str(warn_nvme),
            "nvme_percentage_used_critical": str(crit_nvme),
            "scrub_age_warning_days": str(scrub_days),
            "collection_failure_threshold": str(fail_threshold),
        }
        for key, value in health_values.items():
            set_setting(db, key, value)
        saved_sections.append("Health")

    if section in {"all", "history"}:
        metric_days = bounded_int(metric_retention_days, "Metric retention", 1, 3650)
        snapshot_days = bounded_int(snapshot_retention_days, "Snapshot retention", 1, 3650)
        snapshot_interval = bounded_int(
            full_snapshot_interval_minutes, "Full snapshot interval", 1, 1440
        )

        history_values = {
            "metric_retention_days": str(metric_days),
            "snapshot_retention_days": str(snapshot_days),
            "full_snapshot_interval_minutes": str(snapshot_interval),
        }
        for key, value in history_values.items():
            set_setting(db, key, value)
        saved_sections.append("History")

    db.commit()

    if section == "all":
        redirect_tab = "smtp"
        message = "Settings saved."
    else:
        redirect_tab = section
        message = f"{saved_sections[0]} settings saved."

    return RedirectResponse(
        "/settings?tab=" + quote(redirect_tab) + "&message=" + quote(message),
        status_code=303,
    )

@app.post("/settings/tls")
async def save_tls_settings(
    tls_mode: str = Form("internal"),
    tls_domain: str = Form(""),
    tls_acme_email: str = Form(""),
    tls_acme_ca: str = Form(""),
    clear_acme_ca_root: bool = Form(False),
    certificate: UploadFile | None = File(None),
    private_key: UploadFile | None = File(None),
    acme_ca_root: UploadFile | None = File(None),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    mode = tls_mode.strip().lower()
    if mode not in {"internal", "manual", "acme"}:
        bad_request("TLS mode must be internal, manual, or acme.")

    domain = tls_domain.strip()
    if mode == "acme":
        domain = validate_host(domain, "TLS hostname")
    elif domain:
        domain = validate_host(domain, "TLS hostname")

    email = tls_acme_email.strip()
    if email:
        email = validate_email(email, "ACME account email")

    ca_url = tls_acme_ca.strip()
    if mode == "acme":
        try:
            ca_url = validate_acme_directory_url(ca_url)
        except TLSConfigurationError as exc:
            bad_request(str(exc))

    old_cert = MANUAL_CERT_PATH.read_bytes() if MANUAL_CERT_PATH.exists() else None
    old_key = MANUAL_KEY_PATH.read_bytes() if MANUAL_KEY_PATH.exists() else None
    old_ca_root = ACME_CA_ROOT_PATH.read_bytes() if ACME_CA_ROOT_PATH.exists() else None

    try:
        if mode == "manual":
            cert_bytes = await certificate.read() if certificate and certificate.filename else b""
            key_bytes = await private_key.read() if private_key and private_key.filename else b""
            if bool(cert_bytes) != bool(key_bytes):
                raise TLSConfigurationError(
                    "Upload both the certificate chain and private key together."
                )
            if cert_bytes:
                if len(cert_bytes) > 192 * 1024 or len(key_bytes) > 192 * 1024:
                    raise TLSConfigurationError("Certificate or key upload is too large.")
                validate_certificate_pair(cert_bytes, key_bytes)
                write_secret_file(MANUAL_CERT_PATH, cert_bytes)
                write_secret_file(MANUAL_KEY_PATH, key_bytes)
            elif not (MANUAL_CERT_PATH.exists() and MANUAL_KEY_PATH.exists()):
                raise TLSConfigurationError(
                    "Upload a certificate chain and matching private key for manual TLS."
                )

        ca_root_bytes = (
            await acme_ca_root.read()
            if acme_ca_root and acme_ca_root.filename
            else b""
        )
        if len(ca_root_bytes) > 192 * 1024:
            raise TLSConfigurationError("ACME CA root upload is too large.")
        if ca_root_bytes:
            validate_ca_root(ca_root_bytes)
            write_secret_file(ACME_CA_ROOT_PATH, ca_root_bytes)
        elif clear_acme_ca_root:
            ACME_CA_ROOT_PATH.unlink(missing_ok=True)

        caddyfile = build_caddyfile(
            mode,
            domain=domain,
            email=email,
            acme_ca=ca_url,
            use_acme_ca_root=ACME_CA_ROOT_PATH.exists(),
        )
        persist_and_apply_caddyfile(caddyfile)
    except Exception as exc:
        if old_cert is None:
            MANUAL_CERT_PATH.unlink(missing_ok=True)
        else:
            write_secret_file(MANUAL_CERT_PATH, old_cert)
        if old_key is None:
            MANUAL_KEY_PATH.unlink(missing_ok=True)
        else:
            write_secret_file(MANUAL_KEY_PATH, old_key)
        if old_ca_root is None:
            ACME_CA_ROOT_PATH.unlink(missing_ok=True)
        else:
            write_secret_file(ACME_CA_ROOT_PATH, old_ca_root)
        if isinstance(exc, TLSConfigurationError):
            bad_request(str(exc))
        raise

    set_setting(db, "tls_mode", mode)
    set_setting(db, "tls_domain", domain)
    set_setting(db, "tls_acme_email", email)
    set_setting(db, "tls_acme_ca", ca_url)
    db.commit()
    return RedirectResponse(
        "/settings?tab=tls&message=" + quote("TLS configuration applied successfully."),
        status_code=303,
    )


@app.post("/settings/test-email")
def test_email(
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    try:
        transport = get_setting(db, "email_transport", "smtp").strip().lower()
        send_email(
            db,
            "[NASitron] Email delivery test",
            (
                f"NASitron {APP_VERSION} successfully sent this test through "
                f"the configured {transport.upper()} transport."
            ),
            force=True,
        )
        result = f"{transport.upper()} test message sent."
    except Exception as exc:
        result = f"Email test failed: {exc}"
    return RedirectResponse("/settings?tab=smtp&message=" + quote(result), status_code=303)

# Register focused read-only insight pages and monitoring controls.
install_insights(app, templates)
install_observability(app, templates)
install_personalization(app, templates)
install_zfs_actions(app, templates)

install_storage_admin(app, templates)
