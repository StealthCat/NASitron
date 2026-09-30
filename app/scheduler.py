from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from .alerts import deliver_pending_notifications
from .config import COLLECTOR_WORKERS
from .db import SessionLocal
from .models import Server
from .service import collect_server, prune_history

_scheduler = BackgroundScheduler(timezone="UTC")
_pool: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_inflight: set[int] = set()
_states: dict[int, dict] = {}


def collector_states():
    with _lock:
        return {key: dict(value) for key, value in _states.items()}


def _collect_worker(server_id: int) -> None:
    with _lock:
        _states[server_id] = {"state": "running", "at": datetime.utcnow()}
    result = "completed"
    try:
        with SessionLocal() as db:
            try:
                collect_server(db, server_id)
            except Exception:
                result = "failed"
    finally:
        with _lock:
            _inflight.discard(server_id)
            _states[server_id] = {"state": result, "at": datetime.utcnow()}


def trigger_now(server_id: int) -> bool:
    global _pool
    with _lock:
        if server_id in _inflight:
            return False
        if _pool is None:
            return False
        _inflight.add(server_id)
        _states[server_id] = {"state": "queued", "at": datetime.utcnow()}
        pool = _pool
    try:
        pool.submit(_collect_worker, server_id)
    except RuntimeError:
        with _lock:
            _inflight.discard(server_id)
            _states.pop(server_id, None)
        return False
    return True


def _schedule_due() -> None:
    now = datetime.utcnow()
    with SessionLocal() as db:
        servers = db.scalars(select(Server).where(Server.enabled.is_(True))).all()
        for server in servers:
            interval = max(15, min(86400, server.poll_interval_seconds or 60))
            due = server.last_poll_at is None or now - server.last_poll_at >= timedelta(
                seconds=interval
            )
            if due:
                trigger_now(server.id)


def _deliver_notifications() -> None:
    with SessionLocal() as db:
        try:
            deliver_pending_notifications(db)
        except Exception:
            db.rollback()


def _housekeeping() -> None:
    from .settings_store import set_setting
    import time

    started = time.monotonic()
    with SessionLocal() as db:
        try:
            set_setting(db, "history_housekeeping_state", "running")
            db.commit()
            prune_history(db)
            set_setting(db, "history_housekeeping_state", "completed")
            set_setting(db, "history_housekeeping_error", "")
        except Exception as exc:
            db.rollback()
            set_setting(db, "history_housekeeping_state", "failed")
            set_setting(db, "history_housekeeping_error", str(exc)[:2000])
        finally:
            set_setting(
                db,
                "history_housekeeping_seconds",
                str(round(time.monotonic() - started, 2)),
            )
            set_setting(
                db, "history_housekeeping_attempt", datetime.utcnow().isoformat()
            )
            db.commit()


def start_scheduler() -> None:
    global _pool
    if _scheduler.running:
        return
    _pool = ThreadPoolExecutor(
        max_workers=COLLECTOR_WORKERS,
        thread_name_prefix="nasitron-poll",
    )
    _scheduler.add_job(
        _schedule_due,
        "interval",
        seconds=10,
        id="poll-due-servers",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _deliver_notifications,
        "interval",
        minutes=1,
        id="deliver-alert-notifications",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _housekeeping,
        "interval",
        hours=1,
        id="database-housekeeping",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    _schedule_due()


def stop_scheduler() -> None:
    global _pool
    # Lifespan releases the data-directory instance lock immediately after
    # this returns, so all scheduled work and collector threads must be fully
    # stopped before returning. Otherwise an old worker can overlap a new
    # NASitron process during restart and write the same SQLite database.
    if _scheduler.running:
        _scheduler.shutdown(wait=True)
    if _pool is not None:
        _pool.shutdown(wait=True, cancel_futures=True)
        _pool = None
    with _lock:
        _inflight.clear()
        _states.clear()
