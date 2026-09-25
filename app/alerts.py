from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from .models import Alert, Server
from .settings_store import get_bool, get_int, get_many, get_setting


def _utcnow() -> datetime:
    return datetime.utcnow()


def send_email(db: Session, subject: str, body: str, force: bool = False) -> bool:
    if not force and not get_bool(db, "smtp_enabled", False):
        return False

    settings = get_many(
        db,
        [
            "smtp_host",
            "smtp_port",
            "smtp_from",
            "smtp_to",
            "smtp_username",
            "smtp_password",
            "smtp_ssl",
            "smtp_starttls",
        ],
    )
    host = settings["smtp_host"]
    try:
        port = int(settings["smtp_port"])
    except ValueError:
        port = 587
    sender = settings["smtp_from"]
    recipients = [
        x.strip()
        for x in settings["smtp_to"].replace(";", ",").split(",")
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

    use_ssl = settings["smtp_ssl"].strip().lower() in {"1", "true", "yes", "on"}
    starttls = settings["smtp_starttls"].strip().lower() in {"1", "true", "yes", "on"}
    if use_ssl and starttls:
        raise RuntimeError("SMTP implicit TLS and STARTTLS cannot both be enabled")

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(host, port, timeout=15) as smtp:
        if not use_ssl and starttls:
            smtp.starttls()
        if settings["smtp_username"]:
            smtp.login(settings["smtp_username"], settings["smtp_password"])
        smtp.send_message(msg)
    return True


def _queue_notification(alert: Alert, now: datetime) -> None:
    alert.last_notified_at = None
    alert.notification_attempts = 0
    alert.notification_error = None
    alert.next_notification_at = now


def _upsert_alert(
    db: Session,
    existing: dict[str, Alert],
    server: Server,
    key: str,
    severity: str,
    title: str,
    message: str,
) -> Alert:
    now = _utcnow()
    alert = existing.get(key)
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
        existing[key] = alert
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
    existing: dict[str, Alert],
    active_keys: set[str],
    resolvable_prefixes: set[str],
    resolvable_exact: set[str],
) -> None:
    if not resolvable_prefixes and not resolvable_exact:
        return
    now = _utcnow()
    for alert in existing.values():
        if not alert.active or alert.key in active_keys:
            continue
        if alert.key in resolvable_exact or any(
            alert.key.startswith(prefix) for prefix in resolvable_prefixes
        ):
            alert.active = False
            alert.resolved_at = now
            alert.notification_error = None
            alert.next_notification_at = None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_snapshot(db: Session, server: Server, snapshot: dict[str, Any]) -> None:
    existing = {
        alert.key: alert
        for alert in db.scalars(
            select(Alert).where(Alert.server_id == server.id)
        ).all()
    }
    settings = get_many(
        db,
        [
            "pool_capacity_warning",
            "pool_capacity_critical",
            "drive_temp_warning_c",
            "drive_temp_critical_c",
            "nvme_percentage_used_warning",
            "nvme_percentage_used_critical",
            "scrub_age_warning_days",
        ],
    )
    warn_cap = int(settings["pool_capacity_warning"] or 80)
    crit_cap = int(settings["pool_capacity_critical"] or 90)
    warn_temp = int(settings["drive_temp_warning_c"] or 45)
    crit_temp = int(settings["drive_temp_critical_c"] or 55)
    warn_nvme = int(settings["nvme_percentage_used_warning"] or 80)
    crit_nvme = int(settings["nvme_percentage_used_critical"] or 95)
    scrub_days = int(settings["scrub_age_warning_days"] or 35)

    active_keys: set[str] = set()
    resolvable_prefixes: set[str] = set()
    resolvable_exact: set[str] = {"collector.partial"}
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
            existing,
            server,
            key,
            "warning",
            "Monitoring collection is partial",
            detail,
        )

    missing_pools = set(collection.get("missing_pools", []))
    for name in missing_pools:
        key = f"pool.missing:{name}"
        active_keys.add(key)
        _upsert_alert(
            db,
            existing,
            server,
            key,
            "critical",
            f"Expected pool {name} is missing",
            (
                f"Pool {name} was previously discovered on this server but is no longer "
                "present in the current imported-pool inventory."
            ),
        )

    pool_status_ok = set(collection.get("pool_status_ok", []))
    for pool in snapshot.get("pools", []):
        name = str(pool.get("name") or "unknown")
        resolvable_exact.update(
            {
                f"pool.health:{name}",
                f"pool.capacity:{name}",
                f"pool.missing:{name}",
            }
        )

        health = str(pool.get("health", "")).upper()
        if health != "ONLINE":
            key = f"pool.health:{name}"
            active_keys.add(key)
            _upsert_alert(
                db,
                existing,
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
                existing,
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

        resolvable_prefixes.update(
            {
                f"vdev.errors:{name}:",
                f"vdev.state:{name}:",
            }
        )
        resolvable_exact.add(f"pool.scrub_age:{name}")
        status = pool.get("status") or {}
        for vdev in status.get("vdevs", []):
            ident = str(vdev.get("guid") or vdev.get("name") or "unknown")
            device = str(vdev.get("name") or ident)
            errs = (
                int(vdev.get("read_errors", 0))
                + int(vdev.get("write_errors", 0))
                + int(vdev.get("checksum_errors", 0))
            )
            if errs > 0:
                key = f"vdev.errors:{name}:{ident}"
                active_keys.add(key)
                _upsert_alert(
                    db,
                    existing,
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

            state = str(vdev.get("state") or "").upper()
            if vdev.get("leaf") and state not in {"ONLINE", "HEALTHY", "AVAIL"}:
                role = str(vdev.get("role") or "data")
                severity = "warning" if role in {"cache", "spare"} else "critical"
                key = f"vdev.state:{name}:{ident}"
                active_keys.add(key)
                _upsert_alert(
                    db,
                    existing,
                    server,
                    key,
                    severity,
                    f"Vdev {device} is {state}",
                    f"{device} in pool {name} ({role}) reports state {state}.",
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
                        existing,
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
        ident = str(disk.get("serial") or disk.get("path") or "unknown")
        display = str(disk.get("model") or disk.get("path") or ident)
        alert_keys = {
            "overall": f"drive.smart:{ident}",
            "unavailable": f"drive.smart_unavailable:{ident}",
            "findings": f"drive.smart_findings:{ident}",
            "temp": f"drive.temp:{ident}",
            "reallocated": f"drive.reallocated_sectors:{ident}",
            "pending": f"drive.pending_sectors:{ident}",
            "uncorrectable": f"drive.offline_uncorrectable:{ident}",
            "media": f"drive.media_errors:{ident}",
            "endurance": f"drive.nvme_endurance:{ident}",
        }

        if smart.get("stale") and smart.get("data_available", True):
            continue

        if not smart.get("data_available"):
            key = alert_keys["unavailable"]
            active_keys.add(key)
            findings = ", ".join(smart.get("exit_findings") or []) or "unknown error"
            _upsert_alert(
                db,
                existing,
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

        resolvable_exact.update(alert_keys.values())

        if smart.get("smart_passed") is False:
            key = alert_keys["overall"]
            active_keys.add(key)
            _upsert_alert(
                db,
                existing,
                server,
                key,
                "critical",
                f"SMART failure on {display}",
                f"SMART overall health reports failure for {display} ({ident}).",
            )

        important_findings = [
            value
            for value in (smart.get("exit_findings") or [])
            if value
            in {
                "prefail_attribute",
                "past_threshold_attribute",
                "error_log_records",
                "self_test_errors",
            }
        ]
        if important_findings:
            key = alert_keys["findings"]
            active_keys.add(key)
            severity = (
                "critical"
                if {"prefail_attribute", "past_threshold_attribute"} & set(important_findings)
                else "warning"
            )
            _upsert_alert(
                db,
                existing,
                server,
                key,
                severity,
                f"SMART health findings on {display}",
                "smartctl reports: " + ", ".join(important_findings),
            )

        temp = _number(smart.get("temperature_c"))
        if temp is not None and temp >= warn_temp:
            severity = "critical" if temp >= crit_temp else "warning"
            key = alert_keys["temp"]
            active_keys.add(key)
            _upsert_alert(
                db,
                existing,
                server,
                key,
                severity,
                f"High drive temperature on {display}",
                (
                    f"Drive temperature is {temp:g} C. Warning threshold is "
                    f"{warn_temp} C; critical threshold is {crit_temp} C."
                ),
            )

        for attr, key_name, label, severity in (
            ("reallocated_sectors", "reallocated", "reallocated sectors", "warning"),
            ("pending_sectors", "pending", "pending sectors", "warning"),
            ("offline_uncorrectable", "uncorrectable", "offline uncorrectable sectors", "critical"),
            ("media_errors", "media", "NVMe media errors", "critical"),
        ):
            value = _number(smart.get(attr))
            if value is not None and value > 0:
                key = alert_keys[key_name]
                active_keys.add(key)
                _upsert_alert(
                    db,
                    existing,
                    server,
                    key,
                    severity,
                    f"{display} has {label}",
                    f"SMART reports {value:g} {label} on {display} ({ident}).",
                )

        used = _number(smart.get("percentage_used"))
        if used is not None and used >= warn_nvme:
            severity = "critical" if used >= crit_nvme else "warning"
            key = alert_keys["endurance"]
            active_keys.add(key)
            _upsert_alert(
                db,
                existing,
                server,
                key,
                severity,
                f"NVMe endurance threshold on {display}",
                (
                    f"NVMe percentage used is {used:g}%. Warning threshold is "
                    f"{warn_nvme}% and critical threshold is {crit_nvme}%."
                ),
            )

    _resolve_keys_not_seen(
        existing,
        active_keys,
        resolvable_prefixes,
        resolvable_exact,
    )


def collection_failed(db: Session, server: Server, message: str) -> None:
    threshold = max(1, get_int(db, "collection_failure_threshold", 2))
    if server.consecutive_failures < threshold:
        return
    existing = {
        alert.key: alert
        for alert in db.scalars(
            select(Alert).where(
                Alert.server_id == server.id,
                Alert.key == "collector.offline",
            )
        ).all()
    }
    _upsert_alert(
        db,
        existing,
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
        lines = [f"NASitron active alerts for {server.name} ({server.host})", ""]
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
