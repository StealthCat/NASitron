"""Shared health semantics, operation progress and explainable capacity forecasts."""

from datetime import datetime, timedelta, timezone
import math
import re
from statistics import mean


def effective_role(user):
    return "admin" if user.is_admin else (user.role or "viewer")


def parse_time(value):
    try:
        dt = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, AttributeError, TypeError):
        return None


def server_state(server, snapshot=None):
    if not server.enabled:
        return "offline", "Monitoring disabled"
    if server.last_collection_state == "failed":
        return "critical", "Unreachable / collection failed"
    captured = (
        parse_time((snapshot or {}).get("collection", {}).get("captured_at"))
        or server.last_ok_at
    )
    if not snapshot or not captured:
        return "unknown", "Waiting for first collection"
    if datetime.utcnow() - captured > timedelta(
        seconds=max(180, (server.poll_interval_seconds or 60) * 3)
    ):
        return "warning", "Stale telemetry"
    collection = snapshot.get("collection", {})
    if collection.get("missing_pools"):
        return "critical", "Missing expected pool"
    if (
        collection.get("partial")
        or collection.get("stale_subsystems")
        or server.last_collection_state == "partial"
    ):
        return "warning", "Partial collection"
    return "good", "Reporting"


def smart_state(smart):
    smart = smart or {}
    if smart.get("smart_passed") is False:
        return "critical", "Failed"
    if any(
        smart.get(k, 0)
        for k in (
            "pending_sectors",
            "offline_uncorrectable",
            "media_errors",
            "reallocated_sectors",
        )
    ):
        return "warning", "SMART attention"
    if smart.get("smart_passed") is True:
        return "good", "Passed"
    return "unknown", "Unknown"


def pool_state(pool, server=None, snapshot=None):
    health = pool.get("health")
    if health and health != "ONLINE":
        return "critical", health
    if server is not None:
        state = server_state(server, snapshot)
        if state[0] != "good":
            return state
    if health == "ONLINE":
        return "good", "ONLINE"
    return "unknown", "Unknown"


def operation_status(pool):
    status = pool.get("status") or {}
    scan = status.get("scan") or "No scan reported"
    progress = status.get("scan_detail") or scan
    active = bool(re.search(r"in progress|resilvering|scanning|paused", scan, re.I))
    pct = re.search(r"(\d+(?:\.\d+)?)%\s+done", progress)
    eta = re.search(r"([\w:]+)\s+(?:to go|remaining)", progress)
    rate = re.findall(r"\bat\s+([^,\n]+?/[sS])", progress)
    return {
        "active": active,
        "kind": "Resilver" if "resilver" in scan.lower() else "Scrub",
        "scan": progress,
        "percent": min(100, float(pct[1])) if pct else None,
        "eta": eta[1] if eta else None,
        "rate": rate[-1] if rate else None,
        "last_scrub": status.get("scrub_finished_at"),
    }


def update_operations(db, server, snapshot):
    from sqlalchemy import select
    from .models import MaintenanceAction

    pools = {p["name"]: p for p in snapshot.get("pools", [])}
    stale = set(snapshot.get("collection", {}).get("stale_subsystems", []))
    for action in db.scalars(
        select(MaintenanceAction).where(
            MaintenanceAction.server_id == server.id,
            MaintenanceAction.success.is_(True),
            MaintenanceAction.completed_at.is_(None),
        )
    ):
        pool = pools.get(action.pool)
        if not pool or f"pool.status:{action.pool}" in stale:
            continue
        scan = operation_status(pool)
        if scan["active"] and scan["kind"] == "Resilver":
            action.state = "resilvering"
        elif re.search(r"resilvered .* with 0 errors", scan["scan"], re.I):
            # A completion must be observed after this command, not an old scan.
            match = re.search(r" on (.+)$", scan["scan"].splitlines()[0])
            try:
                finished = (
                    datetime.strptime(match[1].strip(), "%a %b %d %H:%M:%S %Y")
                    if match
                    else None
                )
            except ValueError:
                finished = None
            if action.state in {"resilvering", "verifying"} or (
                finished and finished >= action.created_at
            ):
                action.state = "verifying"
                vdevs = (pool.get("status") or {}).get("vdevs", [])
                if (
                    pool.get("health") == "ONLINE"
                    and vdevs
                    and all(
                        v.get("state") in {"ONLINE", "AVAIL"}
                        and not any(
                            v.get(k)
                            for k in ("read_errors", "write_errors", "checksum_errors")
                        )
                        for v in vdevs
                    )
                ):
                    action.state = "complete"
                    action.completed_at = datetime.utcnow()


def forecast(points, threshold=80):
    """Fit daily means; expose fit quality and a residual-based time range."""
    daily = {}
    for when, value in points:
        if value is not None and math.isfinite(float(value)):
            daily.setdefault(when.date(), []).append(float(value))
    if len(daily) < 7:
        return {"status": "Need at least 7 days of history", "days": None}
    days = sorted(daily)
    xs = [(d - days[0]).days for d in days]
    ys = [mean(daily[d]) for d in days]
    mx, my = mean(xs), mean(ys)
    variance = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / variance
    residual = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys))
    total = sum((y - my) ** 2 for y in ys)
    fit = max(0, 1 - residual / total) if total else 0
    if ys[-1] >= threshold:
        return {
            "status": "Threshold already reached",
            "days": 0,
            "fit": fit,
            "slope": slope,
        }
    if slope <= 0.001:
        return {
            "status": "Stable or shrinking; no crossing projected",
            "days": None,
            "fit": fit,
            "slope": slope,
        }
    predicted = max(0, (threshold - ys[-1]) / slope)
    error = 2 * math.sqrt(residual / max(1, len(ys) - 2)) / slope
    return {
        "status": "Trend estimate, not a guarantee",
        "days": round(predicted, 1),
        "range": [round(max(0, predicted - error), 1), round(predicted + error, 1)],
        "fit": round(fit, 3),
        "slope": round(slope, 4),
        "samples": len(days),
    }


def alert_target(alert):
    from urllib.parse import quote

    if alert.key.startswith("drive.") and ":" in alert.key:
        return f"/servers/{alert.server_id}/drive?identity=" + quote(
            alert.key.split(":", 1)[1], safe=""
        )
    if alert.key.startswith(("pool.", "vdev.")):
        return f"/servers/{alert.server_id}#pools"
    return f"/servers/{alert.server_id}#events"
