"""Bounded history queries and transactional, restart-safe retention rollups."""

from datetime import datetime, timedelta
from collections import defaultdict

from sqlalchemy import select, func, cast, Integer, literal, union_all, delete
from .models import Metric, MetricRollup
from .config import MAX_METRIC_POINTS

RAW_DAYS = 7
HOURLY_DAYS = 30


def source_query(server_id, names, scopes, since, until):
    raw = select(
        Metric.name.label("name"),
        Metric.scope.label("scope"),
        Metric.captured_at.label("captured_at"),
        literal(1).label("n"),
        Metric.value.label("total"),
        Metric.value.label("minimum"),
        Metric.value.label("maximum"),
        Metric.captured_at.label("last_at"),
        Metric.value.label("last_value"),
        literal(1).label("resolution"),
    ).where(
        Metric.server_id == server_id,
        Metric.name.in_(names),
        Metric.scope.in_(scopes),
        Metric.captured_at >= since,
        Metric.captured_at <= until,
    )
    # Rollups are selected by bucket start. Boundary buckets are approximate;
    # the response explicitly identifies the retained resolution.
    rolled = select(
        MetricRollup.name,
        MetricRollup.scope,
        MetricRollup.captured_at,
        MetricRollup.sample_count,
        MetricRollup.total,
        MetricRollup.minimum,
        MetricRollup.maximum,
        MetricRollup.last_at,
        MetricRollup.last_value,
        MetricRollup.resolution,
    ).where(
        MetricRollup.server_id == server_id,
        MetricRollup.name.in_(names),
        MetricRollup.scope.in_(scopes),
        MetricRollup.captured_at >= since,
        MetricRollup.captured_at <= until,
    )
    return union_all(raw, rolled).subquery()


def history_series(
    db, server, names, scopes, since, until, bucket_seconds=None, latest_exact=True
):
    duration = (until - since).total_seconds()
    bucket_seconds = bucket_seconds or max(
        1, int((duration + MAX_METRIC_POINTS - 1) // MAX_METRIC_POINTS)
    )
    source = source_query(server.id, names, scopes, since, until)
    c = source.c
    bucket = cast(
        cast(func.strftime("%s", c.captured_at), Integer) / bucket_seconds, Integer
    )
    rows = db.execute(
        select(
            c.name,
            c.scope,
            bucket.label("bucket"),
            func.max(c.last_at).label("at"),
            func.sum(c.n).label("n"),
            func.sum(c.total).label("total"),
            func.min(c.minimum).label("low"),
            func.max(c.maximum).label("high"),
            func.max(c.resolution).label("resolution"),
        )
        .group_by(c.name, c.scope, bucket)
        .order_by(c.name, c.scope, bucket)
    ).all()
    ranked = select(
        c.name,
        c.scope,
        c.last_at,
        c.last_value,
        func.row_number()
        .over(partition_by=(c.name, c.scope), order_by=c.last_at.desc())
        .label("rank"),
    ).subquery()
    latest = {
        (r.name, r.scope): r
        for r in (
            db.execute(select(ranked).where(ranked.c.rank == 1)) if latest_exact else []
        )
    }
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.name, row.scope].append(row)
    result = []
    for name in names:
        for scope in scopes:
            values = grouped[name, scope]
            points = [
                {
                    "t": r.at.isoformat() + "Z",
                    "v": r.low if name == "drive.smart_passed" else r.total / r.n,
                    "min": r.low,
                    "max": r.high,
                    "n": r.n,
                }
                for r in values
            ]
            last = latest.get((name, scope))
            # Keep the latest exact reading distinct from bucket averages.
            if last and latest_exact and name != "drive.smart_passed":
                points[-1]["v"] = last.last_value
            result.append(
                {
                    "name": name,
                    "scope": scope,
                    "sample_count": sum(r.n for r in values),
                    "returned_points": len(points),
                    "bucket_seconds": bucket_seconds,
                    "retained_resolution_seconds": max(
                        (r.resolution for r in values), default=1
                    ),
                    "start": since.isoformat() + "Z",
                    "end": until.isoformat() + "Z",
                    "expected_interval_seconds": server.smart_interval_minutes * 60
                    if name.startswith("drive.") and not name.startswith("drive.io.")
                    else server.poll_interval_seconds,
                    "points": points,
                }
            )
    return result


def compact_history(db, lock, now=None, max_windows=48):
    """Compact one complete hour/day per transaction, with bounded work per run.

    Insertion and source deletion commit together; interrupted work is rolled
    back and can safely retry without double counting.
    """
    now = now or datetime.utcnow()
    count = 0
    for model, resolution, cutoff, source_resolution in [
        (Metric, 3600, now - timedelta(days=RAW_DAYS), None),
        (MetricRollup, 86400, now - timedelta(days=HOURLY_DAYS), 3600),
    ]:
        for _ in range(max_windows):
            conditions = [model.captured_at < cutoff]
            if source_resolution:
                conditions.append(model.resolution == source_resolution)
            oldest = db.scalar(select(func.min(model.captured_at)).where(*conditions))
            if oldest is None:
                break
            start = oldest.replace(minute=0, second=0, microsecond=0)
            if resolution == 86400:
                start = start.replace(hour=0)
            end = start + timedelta(seconds=resolution)
            if end > cutoff:
                break
            conditions = [model.captured_at >= start, model.captured_at < end]
            if source_resolution:
                conditions.append(model.resolution == source_resolution)
            with lock:
                n = literal(1) if model is Metric else model.sample_count
                total = model.value if model is Metric else model.total
                low = model.value if model is Metric else model.minimum
                high = model.value if model is Metric else model.maximum
                at = model.captured_at if model is Metric else model.last_at
                value = model.value if model is Metric else model.last_value
                aggregates = db.execute(
                    select(
                        model.server_id,
                        model.name,
                        model.scope,
                        func.sum(n).label("n"),
                        func.sum(total).label("total"),
                        func.min(low).label("low"),
                        func.max(high).label("high"),
                    )
                    .where(*conditions)
                    .group_by(model.server_id, model.name, model.scope)
                ).all()
                ranked = (
                    select(
                        model.server_id,
                        model.name,
                        model.scope,
                        at.label("at"),
                        value.label("value"),
                        func.row_number()
                        .over(
                            partition_by=(model.server_id, model.name, model.scope),
                            order_by=(at.desc(), model.id.desc()),
                        )
                        .label("rank"),
                    )
                    .where(*conditions)
                    .subquery()
                )
                lasts = {
                    (r.server_id, r.name, r.scope): r
                    for r in db.execute(select(ranked).where(ranked.c.rank == 1))
                }
                existing_buckets = {
                    (r.server_id, r.name, r.scope): r
                    for r in db.scalars(
                        select(MetricRollup).where(
                            MetricRollup.captured_at == start,
                            MetricRollup.resolution == resolution,
                        )
                    )
                }
                for row in aggregates:
                    last = lasts[row.server_id, row.name, row.scope]
                    # A late source sample can arrive in an already compacted bucket.
                    existing = existing_buckets.get(
                        (row.server_id, row.name, row.scope)
                    )
                    if existing:
                        existing.sample_count += row.n
                        existing.total += row.total
                        existing.minimum = min(existing.minimum, row.low)
                        existing.maximum = max(existing.maximum, row.high)
                        if last.at >= existing.last_at:
                            existing.last_at, existing.last_value = last.at, last.value
                    else:
                        db.add(
                            MetricRollup(
                                server_id=row.server_id,
                                name=row.name,
                                scope=row.scope,
                                captured_at=start,
                                resolution=resolution,
                                sample_count=row.n,
                                total=row.total,
                                minimum=row.low,
                                maximum=row.high,
                                last_at=last.at,
                                last_value=last.value,
                            )
                        )
                db.execute(delete(model).where(*conditions))
                db.commit()
                count += len(aggregates)
    return count


def delete_chunks(db, lock, model, condition, batch=5000):
    total = 0
    while True:
        with lock:
            ids = select(model.id).where(condition).limit(batch)
            removed = db.execute(delete(model).where(model.id.in_(ids))).rowcount
            db.commit()
        total += removed
        if removed < batch:
            return total
