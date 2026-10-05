"""Storage workspace, persistent policies and read-only planning."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, delete

from .insights import session
from .models import (
    Server,
    StoragePolicy,
    StorageRun,
    StorageHostCache,
    StorageSample,
    MonitorEvent,
    MaintenanceAction,
)
from .service import latest_snapshot
from .storage_jobs import remote_inventory, next_due, enqueue
from .storage_insights import (
    diagnose,
    expansion_plan,
    dataset_forecasts,
    vdev_diagnosis,
)
from .zfs_actions import admin, helper_json
from .security import require_csrf
from .db import SessionLocal

_refresh_pool = None
_refresh_guard = threading.Lock()
_refreshing = set()


def validate_policy(kind, config, cron, zone):
    ZoneInfo(zone)
    due = next_due(cron, zone)
    if kind not in {"snapshot", "replication", "scrub", "smart"}:
        raise ValueError("Unsupported job type")
    if (
        not 1 <= int(config["keep"]) <= 3650
        or not 1 <= int(config["max_age_hours"]) <= 8760
    ):
        raise ValueError("Retention must be 1–3650; overdue threshold 1–8760 hours")
    if kind in {"snapshot", "replication"}:
        if not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*",
            config["dataset"],
        ):
            raise ValueError(
                "Use an exact dataset name; wildcards and recursive policies are not supported"
            )
    if kind == "replication":
        if not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)+",
            config["destination"],
        ):
            raise ValueError("Destination must be a dedicated child dataset")
        if not 0 <= int(config["bandwidth_mib"]) <= 10000:
            raise ValueError("Bandwidth must be 0–10000 MiB/s; zero means unlimited")
    if kind == "smart" and (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,254}", config["disk"])
        or config["test"] not in {"short", "long"}
    ):
        raise ValueError("Select a by-id basename and short or long test")
    if kind == "scrub" and not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_.:-]*", config["pool"]
    ):
        raise ValueError("Invalid pool name")
    return due


def refresh_host(server_id):
    try:
        with SessionLocal() as db:
            server = db.get(Server, server_id)
            if not server:
                return
            cache = db.get(StorageHostCache, server_id)
            if cache is None:
                cache = StorageHostCache(server_id=server_id)
                db.add(cache)
            cache.attempted_at = datetime.utcnow()
            db.commit()
            try:
                data = remote_inventory(server)
                # Include live topology/by-id choices for planning and host capabilities.
                data["topology"] = helper_json(server, "inventory")
                now = datetime.utcnow()
                latest_sample = db.scalar(
                    select(StorageSample.captured_at)
                    .where(StorageSample.server_id == server_id)
                    .order_by(StorageSample.captured_at.desc())
                    .limit(1)
                )
                if latest_sample is None or now - latest_sample >= timedelta(hours=1):
                    for dataset in data["datasets"]:
                        if (
                            not str(dataset.get("used", "")).isdigit()
                            or not str(dataset.get("available", "")).isdigit()
                        ):
                            continue
                        db.add(
                            StorageSample(
                                server_id=server_id,
                                name=dataset["name"],
                                used=int(dataset["used"]),
                                available=int(dataset["available"]),
                            )
                        )
                recent = set(
                    db.scalars(
                        select(MonitorEvent.message)
                        .where(
                            MonitorEvent.server_id == server_id,
                            MonitorEvent.kind == "zfs",
                        )
                        .order_by(MonitorEvent.captured_at.desc())
                        .limit(1000)
                    ).all()
                )
                for block in re.split(
                    r"(?m)(?=^[A-Z][a-z]{2} +\d)", data.get("events", "")
                ):
                    if (
                        not block.strip()
                        or "ereport." not in block
                        and "sysevent." not in block
                    ):
                        continue
                    digest = hashlib.sha256(block.encode()).hexdigest()[:20]
                    message = "[" + digest + "] " + block.strip()[:1900]
                    if message not in recent:
                        db.add(
                            MonitorEvent(
                                server_id=server_id,
                                kind="zfs",
                                severity="warning" if "ereport." in block else "info",
                                message=message,
                            )
                        )
                        recent.add(message)
                cache.payload_json, cache.captured_at, cache.error = (
                    json.dumps(data),
                    now,
                    "",
                )
                db.execute(
                    delete(StorageSample).where(
                        StorageSample.captured_at < now - timedelta(days=90)
                    )
                )
            except Exception as exc:
                cache.error = str(exc)[:4000]
            db.commit()
    finally:
        with _refresh_guard:
            _refreshing.discard(server_id)


def request_refresh(server_id):
    global _refresh_pool
    with _refresh_guard:
        if server_id in _refreshing:
            return
        if _refresh_pool is None:
            _refresh_pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="nasitron-storage-inventory"
            )
        _refreshing.add(server_id)
        _refresh_pool.submit(refresh_host, server_id)


def refresh_due():
    with SessionLocal() as db:
        for server in db.scalars(select(Server).where(Server.enabled.is_(True))).all():
            cache = db.get(StorageHostCache, server.id)
            if (
                cache is None
                or cache.attempted_at is None
                or datetime.utcnow() - cache.attempted_at > timedelta(hours=1)
            ):
                request_refresh(server.id)


def shutdown():
    global _refresh_pool
    if _refresh_pool:
        _refresh_pool.shutdown(wait=True, cancel_futures=True)
        _refresh_pool = None
    with _refresh_guard:
        _refreshing.clear()


def install(app, templates):
    @app.get("/servers/{server_id}/storage")
    def workspace(
        request: Request,
        server_id: int,
        tab: str = "overview",
        pool: str = "",
        size_tb: float = 12,
        q: str = "",
        kind: str = "",
        page: int = 1,
        edit: int = 0,
        db=Depends(session),
    ):
        admin(request)
        server = db.get(Server, server_id)
        if not server:
            raise HTTPException(404)
        if not 0 < size_tb <= 1000:
            raise HTTPException(400, "Proposed disk size must be 0–1000 TB")
        cache = db.get(StorageHostCache, server_id)
        data = json.loads(cache.payload_json) if cache else {}
        for snapshot_row in data.get("snapshots", []):
            try:
                snapshot_row["created_at"] = datetime.utcfromtimestamp(
                    int(snapshot_row["creation"])
                )
            except (ValueError, OverflowError, OSError):
                snapshot_row["created_at"] = None
        snapshot = latest_snapshot(db, server_id) or {}
        diagnostics = diagnose(db, server_id, snapshot, datetime.utcnow())
        pools = data.get("topology", {}).get("pools", [])
        selected = next(
            (p for p in pools if p["name"] == pool), pools[0] if pools else None
        )
        plan = (
            expansion_plan(
                selected["members"], snapshot.get("drives", []), int(size_tb * 10**12)
            )
            if selected
            else []
        )
        policies = db.scalars(
            select(StoragePolicy)
            .where(StoragePolicy.server_id == server_id)
            .order_by(StoragePolicy.id)
        ).all()
        runs = db.execute(
            select(StorageRun, StoragePolicy.name)
            .join(StoragePolicy)
            .where(StoragePolicy.server_id == server_id)
            .order_by(StorageRun.id.desc())
            .limit(100)
        ).all()
        events = select(MonitorEvent).where(MonitorEvent.server_id == server_id)
        if q:
            events = events.where(
                MonitorEvent.message.contains(q[:200], autoescape=True)
            )
        if kind:
            events = events.where(MonitorEvent.kind == kind)
        events = db.scalars(
            events.order_by(MonitorEvent.captured_at.desc())
            .offset((max(1, min(page, 10000)) - 1) * 50)
            .limit(51)
        ).all()
        audits = db.scalars(
            select(MaintenanceAction)
            .where(MaintenanceAction.server_id == server_id)
            .order_by(MaintenanceAction.created_at.desc())
            .limit(50)
        ).all()
        editing = next((p for p in policies if p.id == edit), None)
        return templates.TemplateResponse(
            request=request,
            name="storage.html",
            context=dict(
                server=server,
                data=data,
                cache=cache,
                editing=editing,
                edit_config=json.loads(editing.config_json) if editing else {},
                snapshot=snapshot,
                vdevs=vdev_diagnosis(data.get("topology", {}), diagnostics),
                active_tab=tab,
                diagnostics=diagnostics,
                pools=pools,
                selected=selected,
                plan=plan,
                size_tb=size_tb,
                policies=policies,
                runs=runs,
                forecasts=dataset_forecasts(db, server_id, datetime.utcnow()),
                events=events[:50],
                more=len(events) > 50,
                page=max(1, page),
                q=q,
                kind=kind,
                audits=audits,
                servers=db.scalars(select(Server).order_by(Server.name)).all(),
                zones=sorted(available_timezones()),
                message=request.query_params.get("message", ""),
            ),
        )

    @app.post(
        "/servers/{server_id}/storage/refresh", dependencies=[Depends(require_csrf)]
    )
    def refresh(request: Request, server_id: int, db=Depends(session)):
        admin(request)
        if not db.get(Server, server_id):
            raise HTTPException(404)
        request_refresh(server_id)
        return RedirectResponse(
            f"/servers/{server_id}/storage?message=Inventory+refresh+queued.+Reload+in+a+moment.",
            303,
        )

    @app.post(
        "/servers/{server_id}/storage/policies", dependencies=[Depends(require_csrf)]
    )
    def create_policy(
        request: Request,
        server_id: int,
        name: str = Form(...),
        kind: str = Form(...),
        cron: str = Form(...),
        timezone: str = Form("UTC"),
        dataset: str = Form(""),
        pool: str = Form(""),
        disk: str = Form(""),
        test: str = Form("short"),
        keep: int = Form(24),
        max_age_hours: int = Form(48),
        destination_server: int = Form(0),
        destination: str = Form(""),
        bandwidth_mib: int = Form(0),
        confirm_text: str = Form(...),
        policy_id: int = Form(0),
        db=Depends(session),
    ):
        admin(request)
        server = db.get(Server, server_id)
        if not server:
            raise HTTPException(404)
        if confirm_text != "ENABLE " + name or not name.strip() or len(name) > 120:
            raise HTTPException(400, "Type ENABLE followed by the exact policy name")
        existing = db.get(StoragePolicy, policy_id) if policy_id else None
        if policy_id and (existing is None or existing.server_id != server_id):
            raise HTTPException(404)
        previous = json.loads(existing.config_json) if existing else {}
        config = dict(
            owner=previous.get("owner") or uuid.uuid4().hex,
            dataset=dataset,
            pool=pool,
            disk=disk,
            test=test,
            keep=keep,
            max_age_hours=max_age_hours,
            destination_server=destination_server,
            destination=destination,
            bandwidth_mib=bandwidth_mib,
        )
        try:
            due = validate_policy(kind, config, cron, timezone)
            if existing:
                if existing.kind != kind or any(
                    previous.get(k) != config.get(k)
                    for k in [
                        "dataset",
                        "destination_server",
                        "destination",
                        "disk",
                        "pool",
                        "test",
                    ]
                ):
                    raise ValueError(
                        "Changing job targets requires a new policy; schedule, retention and bandwidth can be edited"
                    )
                if db.scalar(
                    select(StorageRun.id).where(
                        StorageRun.policy_id == existing.id,
                        StorageRun.state.in_(["queued", "running", "cancel_requested"]),
                    )
                ):
                    raise ValueError(
                        "Wait for the active run before editing this policy"
                    )
            if kind == "replication":
                target = db.get(Server, destination_server)
                if (
                    not target
                    or target.id == server_id
                    or target.host == server.host
                    and target.port == server.port
                ):
                    raise ValueError("Choose a different NAS host")
                if not server.host_key_fingerprint or not target.host_key_fingerprint:
                    raise ValueError(
                        "Verify and pin both SSH host keys before enabling replication"
                    )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        if existing:
            (
                existing.name,
                existing.config_json,
                existing.cron,
                existing.timezone,
                existing.next_run,
            ) = name.strip(), json.dumps(config), cron, timezone, due
            existing.enabled = True
        else:
            row = StoragePolicy(
                server_id=server_id,
                name=name.strip(),
                kind=kind,
                config_json=json.dumps(config),
                cron=cron,
                timezone=timezone,
                next_run=due,
            )
            db.add(row)
        db.add(
            MonitorEvent(
                server_id=server_id,
                kind="administration",
                message=f"{request.state.current_user.username} enabled {kind} policy {name}; schedule {cron} ({timezone})",
            )
        )
        db.commit()
        return RedirectResponse(f"/servers/{server_id}/storage?tab=jobs", 303)

    @app.post(
        "/servers/{server_id}/storage/policies/{policy_id}/{operation}",
        dependencies=[Depends(require_csrf)],
    )
    def policy_action(
        request: Request,
        server_id: int,
        policy_id: int,
        operation: str,
        db=Depends(session),
    ):
        admin(request)
        policy = db.get(StoragePolicy, policy_id)
        if not policy or policy.server_id != server_id:
            raise HTTPException(404)
        if operation == "toggle":
            policy.enabled = not policy.enabled
            policy.next_run = next_due(policy.cron, policy.timezone)
            db.commit()
        elif operation == "run":
            try:
                enqueue(policy.id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        elif operation == "cancel":
            for run in db.scalars(
                select(StorageRun).where(
                    StorageRun.policy_id == policy.id,
                    StorageRun.state.in_(["running", "queued"]),
                )
            ).all():
                run.state = "cancel_requested"
            db.commit()
        else:
            raise HTTPException(404)
        db.add(
            MonitorEvent(
                server_id=server_id,
                kind="administration",
                message=f"{request.state.current_user.username}: {operation} policy {policy.name}",
            )
        )
        db.commit()
        return RedirectResponse(f"/servers/{server_id}/storage?tab=jobs", 303)
