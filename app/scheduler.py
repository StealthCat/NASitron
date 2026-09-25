from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from .db import SessionLocal
from .models import Server
from .service import collect_server

_scheduler = BackgroundScheduler(timezone="UTC")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nasitron-poll")
_lock = threading.Lock()
_inflight: set[int] = set()


def _collect_worker(server_id: int) -> None:
    try:
        with SessionLocal() as db:
            try:
                collect_server(db, server_id)
            except Exception:
                # The collection service persists the error/alert state.
                pass
    finally:
        with _lock:
            _inflight.discard(server_id)


def trigger_now(server_id: int) -> bool:
    with _lock:
        if server_id in _inflight:
            return False
        _inflight.add(server_id)
    _pool.submit(_collect_worker, server_id)
    return True


def _schedule_due() -> None:
    now = datetime.utcnow()
    with SessionLocal() as db:
        servers = db.scalars(select(Server).where(Server.enabled.is_(True))).all()
        for server in servers:
            interval = max(15, server.poll_interval_seconds or 60)
            due = server.last_poll_at is None or now - server.last_poll_at >= timedelta(seconds=interval)
            if due:
                trigger_now(server.id)


def start_scheduler() -> None:
    if _scheduler.running:
        return
    _scheduler.add_job(
        _schedule_due,
        "interval",
        seconds=10,
        id="poll-due-servers",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    _schedule_due()


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
    _pool.shutdown(wait=False, cancel_futures=True)
