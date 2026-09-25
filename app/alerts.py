from __future__ import annotations

import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Alert, Server
from .settings_store import get_bool, get_int, get_setting


def _utcnow() -> datetime:
    return datetime.utcnow()


def send_email(db: Session, subject: str, body: str) -> None:
    if not get_bool(db, "smtp_enabled", False):
        return
    host = get_setting(db, "smtp_host")
    port = get_int(db, "smtp_port", 587)
    sender = get_setting(db, "smtp_from")
    recipients = [
        x.strip() for x in get_setting(db, "smtp_to").replace(";", ",").split(",") if x.strip()
    ]
    if not host or not sender or not recipients:
        raise RuntimeError("SMTP is enabled but host, From, or recipients are not configured")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    username = get_setting(db, "smtp_username")
    password = get_setting(db, "smtp_password")
    use_ssl = get_bool(db, "smtp_ssl", False)
    starttls = get_bool(db, "smtp_starttls", True)

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(host, port, timeout=15) as smtp:
        if not use_ssl and starttls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)


def _upsert_alert(
    db: Session,
    server: Server,
    key: str,
    severity: str,
    title: str,
    message: str,
) -> Alert:
    now = _utcnow()
    alert = db.scalar(select(Alert).where(Alert.server_id == server.id, Alert.key == key))
    notify = False
    if alert is None:
        alert = Alert(
            server_id=server.id,
            key=key,
            severity=severity,
            title=title,
            message=message,
            active=True,
            first_seen=now,
            last_seen=now,
        )
        db.add(alert)
        notify = True
    elif not alert.active:
        alert.active = True
        alert.acknowledged = False
        alert.resolved_at = None
        alert.first_seen = now
        alert.last_seen = now
        alert.severity = severity
        alert.title = title
        alert.message = message
        notify = True
    else:
        if alert.severity != severity or alert.message != message:
            notify = True
        alert.last_seen = now
        alert.severity = severity
        alert.title = title
        alert.message = message

    if notify:
        try:
            send_email(
                db,
                f"[NASitron][{severity.upper()}] {server.name}: {title}",
                f"Server: {server.name} ({server.host})\nSeverity: {severity}\n\n{message}\n",
            )
            alert.last_notified_at = now
        except Exception as exc:
            # Alert persistence must not fail because the relay is unavailable.
            alert.message = f"{message}\n\nSMTP notification error: {exc}"
    return alert


def _resolve_missing(db: Session, server: Server, active_keys: set[str]) -> None:
    now = _utcnow()
    existing = db.scalars(select(Alert).where(Alert.server_id == server.id, Alert.active.is_(True))).all()
    for alert in existing:
        if alert.key not in active_keys and alert.key != "collector.offline":
            alert.active = False
            alert.resolved_at = now


def evaluate_snapshot(db: Session, server: Server, snapshot: dict[str, Any]) -> None:
    active_keys: set[str] = set()
    warn_cap = get_int(db, "pool_capacity_warning", 80)
    crit_cap = get_int(db, "pool_capacity_critical", 90)
    warn_temp = get_int(db, "drive_temp_warning_c", 45)
    crit_temp = get_int(db, "drive_temp_critical_c", 55)
    scrub_days = get_int(db, "scrub_age_warning_days", 35)

    for pool in snapshot.get("pools", []):
        name = pool.get("name", "unknown")
        health = str(pool.get("health", "")).upper()
        if health != "ONLINE":
            key = f"pool.health:{name}"
            active_keys.add(key)
            _upsert_alert(db, server, key, "critical", f"Pool {name} is {health}", f"ZFS reports pool {name} health as {health}.")

        cap = float(pool.get("capacity_pct") or 0)
        if cap >= warn_cap:
            severity = "critical" if cap >= crit_cap else "warning"
            key = f"pool.capacity:{name}"
            active_keys.add(key)
            _upsert_alert(db, server, key, severity, f"Pool {name} is {cap:.0f}% full", f"Pool capacity is {cap:.1f}%. Warning threshold is {warn_cap}% and critical threshold is {crit_cap}%.")

        status = pool.get("status") or {}
        for vdev in status.get("vdevs", []):
            errs = int(vdev.get("read_errors", 0)) + int(vdev.get("write_errors", 0)) + int(vdev.get("checksum_errors", 0))
            if errs > 0:
                device = vdev.get("name", "unknown")
                key = f"vdev.errors:{name}:{device}"
                active_keys.add(key)
                _upsert_alert(db, server, key, "warning", f"ZFS errors on {device}", f"{device} in pool {name} has read={vdev.get('read_errors', 0)}, write={vdev.get('write_errors', 0)}, checksum={vdev.get('checksum_errors', 0)} errors.")

        scrub_finished = status.get("scrub_finished_at")
        if scrub_finished and scrub_days > 0:
            try:
                age_days = (_utcnow() - datetime.fromisoformat(scrub_finished)).days
                if age_days > scrub_days:
                    key = f"pool.scrub_age:{name}"
                    active_keys.add(key)
                    _upsert_alert(db, server, key, "warning", f"Pool {name} scrub is overdue", f"Last parsed scrub completion is {age_days} days old; threshold is {scrub_days} days.")
            except ValueError:
                pass

    for disk in snapshot.get("drives", []):
        smart = disk.get("smart") or {}
        ident = disk.get("serial") or disk.get("path") or "unknown"
        display = disk.get("model") or disk.get("path") or ident
        if smart.get("smart_passed") is False:
            key = f"drive.smart:{ident}"
            active_keys.add(key)
            _upsert_alert(db, server, key, "critical", f"SMART failure on {display}", f"SMART overall health reports failure for {display} ({ident}).")
        temp = smart.get("temperature_c")
        if temp is not None and float(temp) >= warn_temp:
            severity = "critical" if float(temp) >= crit_temp else "warning"
            key = f"drive.temp:{ident}"
            active_keys.add(key)
            _upsert_alert(db, server, key, severity, f"High drive temperature on {display}", f"Drive temperature is {temp} C. Warning threshold is {warn_temp} C; critical threshold is {crit_temp} C.")

        for attr, label in [
            ("pending_sectors", "pending sectors"),
            ("offline_uncorrectable", "offline uncorrectable sectors"),
        ]:
            value = smart.get(attr)
            if value not in (None, 0, "0"):
                key = f"drive.{attr}:{ident}"
                active_keys.add(key)
                _upsert_alert(db, server, key, "warning", f"{display} has {label}", f"SMART reports {value} {label} on {display} ({ident}).")

    _resolve_missing(db, server, active_keys)


def collection_failed(db: Session, server: Server, message: str) -> None:
    threshold = max(1, get_int(db, "collection_failure_threshold", 2))
    if server.consecutive_failures >= threshold:
        _upsert_alert(
            db,
            server,
            "collector.offline",
            "critical",
            "SSH collection is failing",
            f"Collection has failed {server.consecutive_failures} consecutive times. Last error: {message}",
        )


def collection_recovered(db: Session, server: Server) -> None:
    alert = db.scalar(select(Alert).where(Alert.server_id == server.id, Alert.key == "collector.offline"))
    if alert and alert.active:
        alert.active = False
        alert.resolved_at = _utcnow()
