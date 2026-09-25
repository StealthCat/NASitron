from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from .models import Alert, Server
from .settings_store import get_bool, get_int, get_setting


def _utcnow() -> datetime:
    return datetime.utcnow()


def send_email(db: Session, subject: str, body: str, force: bool = False) -> bool:
    if not force and not get_bool(db, "smtp_enabled", False):
        return False

    host = get_setting(db, "smtp_host")
    port = get_int(db, "smtp_port", 587)
    sender = get_setting(db, "smtp_from")
    recipients = [
        x.strip()
        for x in get_setting(db, "smtp_to").replace(";", ",").split(",")
        if x.strip()
    ]
    if not host or not sender or not recipients:
        raise RuntimeError(
            "SMTP is enabled but host, From, or recipients are not configured"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    username = get_setting(db, "smtp_username")
    password = get_setting(db, "smtp_password")
    use_ssl = get_bool(db, "smtp_ssl", False)
    starttls = get_bool(db, "smtp_starttls", True)
    if use_ssl and starttls:
        raise RuntimeError("SMTP implicit TLS and STARTTLS cannot both be enabled")

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(host, port, timeout=15) as smtp:
        if not use_ssl and starttls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)
    return True


def _queue_notification(alert: Alert, now: datetime) -> None:
    alert.last_notified_at = None
    alert.notification_attempts = 0
    alert.notification_error = None
    alert.next_notification_at = now


def _upsert_alert(
    db: Session,
    server: Server,
    key: str,
    severity: str,
    title: str,
    message: str,
) -> Alert:
    now = _utcnow()
    alert = db.scalar(
        select(Alert).where(Alert.server_id == server.id, Alert.key == key)
    )
    changed = False

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
        changed = True
    elif not alert.active:
        alert.active = True
        alert.acknowledged = False
        alert.resolved_at = None
        alert.first_seen = now
        alert.last_seen = now
        alert.severity = severity
        alert.title = title
        alert.message = message
        changed = True
    else:
        if alert.severity != severity or alert.title != title:
            changed = True
        alert.last_seen = now
        alert.severity = severity
        alert.title = title
        alert.message = message

    if changed:
        _queue_notification(alert, now)
    return alert


def _resolve_keys_not_seen(
    db: Session,
    server: Server,
    active_keys: set[str],
    resolvable_prefixes: set[str],
    resolvable_exact: set[str],
) -> None:
    if not resolvable_prefixes:
        return
    now = _utcnow()
    existing = db.scalars(
        select(Alert).where(
            Alert.server_id == server.id,
            Alert.active.is_(True),
        )
    ).all()
    for alert in existing:
        if alert.key in active_keys:
            continue
        if alert.key in resolvable_exact or any(
            alert.key.startswith(prefix) for prefix in resolvable_prefixes
        ):
            alert.active = False
            alert.resolved_at = now
            alert.notification_error = None
            alert.next_notification_at = None


def evaluate_snapshot(db: Session, server: Server, snapshot: dict[str, Any]) -> None:
    active_keys: set[str] = set()
    resolvable_prefixes: set[str] = {"pool.health:", "pool.capacity:"}
    resolvable_exact: set[str] = {"collector.partial"}
    warn_cap = get_int(db, "pool_capacity_warning", 80)
    crit_cap = get_int(db, "pool_capacity_critical", 90)
    warn_temp = get_int(db, "drive_temp_warning_c", 45)
    crit_temp = get_int(db, "drive_temp_critical_c", 55)
    scrub_days = get_int(db, "scrub_age_warning_days", 35)

    collection = snapshot.get("collection", {})
    errors = collection.get("errors", [])
    if errors:
        key = "collector.partial"
        active_keys.add(key)
        detail = "; ".join(
            f"{item.get('subsystem', 'unknown')}: {item.get('message', 'failed')}"
            for item in errors[:8]
        )
        if len(errors) > 8:
            detail += f"; plus {len(errors) - 8} additional collection errors"
        _upsert_alert(
            db,
            server,
            key,
            "warning",
            "Monitoring collection is partial",
            detail,
        )

    pool_status_ok = set(collection.get("pool_status_ok", []))
    for pool in snapshot.get("pools", []):
        name = pool.get("name", "unknown")
        health = str(pool.get("health", "")).upper()
        if health != "ONLINE":
            key = f"pool.health:{name}"
            active_keys.add(key)
            _upsert_alert(
                db,
                server,
                key,
                "critical",
                f"Pool {name} is {health}",
                f"ZFS reports pool {name} health as {health}.",
            )

        cap = float(pool.get("capacity_pct") or 0)
        if cap >= warn_cap:
            severity = "critical" if cap >= crit_cap else "warning"
            key = f"pool.capacity:{name}"
            active_keys.add(key)
            _upsert_alert(
                db,
                server,
                key,
                severity,
                f"Pool {name} capacity threshold exceeded",
                (
                    f"Pool capacity is {cap:.1f}%. Warning threshold is "
                    f"{warn_cap}% and critical threshold is {crit_cap}%."
                ),
            )

        if name not in pool_status_ok:
            continue

        resolvable_prefixes.add(f"vdev.errors:{name}:")
        resolvable_exact.add(f"pool.scrub_age:{name}")
        status = pool.get("status") or {}
        for vdev in status.get("vdevs", []):
            errs = (
                int(vdev.get("read_errors", 0))
                + int(vdev.get("write_errors", 0))
                + int(vdev.get("checksum_errors", 0))
            )
            if errs > 0:
                device = vdev.get("name", "unknown")
                key = f"vdev.errors:{name}:{device}"
                active_keys.add(key)
                _upsert_alert(
                    db,
                    server,
                    key,
                    "warning",
                    f"ZFS errors on {device}",
                    (
                        f"{device} in pool {name} has read={vdev.get('read_errors', 0)}, "
                        f"write={vdev.get('write_errors', 0)}, "
                        f"checksum={vdev.get('checksum_errors', 0)} errors."
                    ),
                )

        scrub_finished = status.get("scrub_finished_at")
        if scrub_finished and scrub_days > 0:
            try:
                age_days = (_utcnow() - datetime.fromisoformat(scrub_finished)).days
                if age_days > scrub_days:
                    key = f"pool.scrub_age:{name}"
                    active_keys.add(key)
                    _upsert_alert(
                        db,
                        server,
                        key,
                        "warning",
                        f"Pool {name} scrub is overdue",
                        (
                            f"Last parsed scrub completion is {age_days} days old; "
                            f"threshold is {scrub_days} days."
                        ),
                    )
            except ValueError:
                pass

    for disk in snapshot.get("drives", []):
        smart = disk.get("smart") or {}
        if smart.get("stale"):
            continue

        ident = disk.get("serial") or disk.get("path") or "unknown"
        display = disk.get("model") or disk.get("path") or ident
        if not smart.get("data_available"):
            key = f"drive.smart_unavailable:{ident}"
            active_keys.add(key)
            findings = ", ".join(smart.get("exit_findings") or []) or "unknown error"
            _upsert_alert(
                db,
                server,
                key,
                "warning",
                f"SMART data unavailable for {display}",
                (
                    f"smartctl did not return usable JSON for {display} ({ident}); "
                    f"exit={smart.get('command_exit')}, findings={findings}."
                ),
            )
            continue

        for prefix in (
            "drive.smart:",
            "drive.smart_unavailable:",
            "drive.temp:",
            "drive.pending_sectors:",
            "drive.offline_uncorrectable:",
        ):
            resolvable_exact.add(f"{prefix}{ident}")

        if smart.get("smart_passed") is False:
            key = f"drive.smart:{ident}"
            active_keys.add(key)
            _upsert_alert(
                db,
                server,
                key,
                "critical",
                f"SMART failure on {display}",
                f"SMART overall health reports failure for {display} ({ident}).",
            )

        temp = smart.get("temperature_c")
        if temp is not None and float(temp) >= warn_temp:
            severity = "critical" if float(temp) >= crit_temp else "warning"
            key = f"drive.temp:{ident}"
            active_keys.add(key)
            _upsert_alert(
                db,
                server,
                key,
                severity,
                f"High drive temperature on {display}",
                (
                    f"Drive temperature is {temp} C. Warning threshold is "
                    f"{warn_temp} C; critical threshold is {crit_temp} C."
                ),
            )

        for attr, label in (
            ("pending_sectors", "pending sectors"),
            ("offline_uncorrectable", "offline uncorrectable sectors"),
        ):
            value = smart.get(attr)
            if value not in (None, 0, "0"):
                key = f"drive.{attr}:{ident}"
                active_keys.add(key)
                _upsert_alert(
                    db,
                    server,
                    key,
                    "warning",
                    f"{display} has {label}",
                    f"SMART reports {value} {label} on {display} ({ident}).",
                )

    _resolve_keys_not_seen(
        db,
        server,
        active_keys,
        resolvable_prefixes,
        resolvable_exact,
    )


def collection_failed(db: Session, server: Server, message: str) -> None:
    threshold = max(1, get_int(db, "collection_failure_threshold", 2))
    if server.consecutive_failures >= threshold:
        _upsert_alert(
            db,
            server,
            "collector.offline",
            "critical",
            "SSH collection is failing",
            (
                f"Collection has failed {server.consecutive_failures} consecutive times. "
                f"Last error: {message}"
            ),
        )


def collection_recovered(db: Session, server: Server) -> None:
    alert = db.scalar(
        select(Alert).where(
            Alert.server_id == server.id,
            Alert.key == "collector.offline",
        )
    )
    if alert and alert.active:
        alert.active = False
        alert.resolved_at = _utcnow()
        alert.next_notification_at = None
        alert.notification_error = None


def deliver_pending_notifications(db: Session) -> int:
    if not get_bool(db, "smtp_enabled", False):
        return 0

    now = _utcnow()
    pending = db.scalars(
        select(Alert)
        .options(selectinload(Alert.server))
        .where(
            Alert.active.is_(True),
            Alert.last_notified_at.is_(None),
            or_(
                Alert.next_notification_at.is_(None),
                Alert.next_notification_at <= now,
            ),
        )
        .order_by(Alert.server_id, Alert.severity.desc(), Alert.first_seen)
    ).all()
    if not pending:
        return 0

    grouped: dict[int, list[Alert]] = {}
    for alert in pending:
        grouped.setdefault(alert.server_id, []).append(alert)

    sent_groups = 0
    for alerts in grouped.values():
        server = alerts[0].server
        lines = [
            f"NASitron active alerts for {server.name} ({server.host})",
            "",
        ]
        for alert in alerts:
            lines.extend(
                [
                    f"[{alert.severity.upper()}] {alert.title}",
                    alert.message,
                    "",
                ]
            )
        try:
            sent = send_email(
                db,
                f"[NASitron] {server.name}: {len(alerts)} active alert(s)",
                "\n".join(lines),
            )
            if sent:
                for alert in alerts:
                    alert.last_notified_at = now
                    alert.notification_attempts = 0
                    alert.next_notification_at = None
                    alert.notification_error = None
                sent_groups += 1
        except Exception as exc:
            for alert in alerts:
                attempts = (alert.notification_attempts or 0) + 1
                delay_minutes = min(360, 2 ** min(attempts, 8))
                alert.notification_attempts = attempts
                alert.next_notification_at = now + timedelta(minutes=delay_minutes)
                alert.notification_error = str(exc)[:2000]

    db.commit()
    return sent_groups
