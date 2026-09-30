"""Bounded history queries with explicit coverage and restart-safe rollups."""

from datetime import datetime, timedelta, timezone
from collections import defaultdict
from sqlalchemy import (
    select,
    func,
    cast,
    Integer,
    literal,
    union_all,
    delete,
    and_,
    or_,
)
from .models import Metric, MetricRollup
from .config import MAX_METRIC_POINTS

RAW_DAYS = 7
HOURLY_DAYS = 30


def epoch(value):
    return value.replace(tzinfo=timezone.utc).timestamp()


def source_query(server_id, names, scopes, since, until, pairs=None):
    pairs = pairs or [(n, s) for n in names for s in scopes]
    queries = []
    for model in (Metric, MetricRollup):
        raw = model is Metric
        stamp = cast(func.strftime("%s", model.captured_at), Integer)
        resolution = literal(1) if raw else model.resolution
        end = stamp if raw else stamp + resolution
        at = model.captured_at if raw else model.last_at
        value = model.value if raw else model.total
        filters = [
            model.server_id == server_id,
            or_(*(and_(model.name == n, model.scope == s) for n, s in pairs)),
            model.captured_at <= until,
        ]
        if raw:
            filters.append(model.captured_at >= since)
        else:
            filters.extend(
                [model.captured_at >= since - timedelta(days=1), end > epoch(since)]
            )
        queries.append(
            select(
                model.name.label("name"),
                model.scope.label("scope"),
                stamp.label("start"),
                end.label("end"),
                at.label("last_at"),
                resolution.label("resolution"),
                (literal(1) if raw else model.sample_count).label("n"),
                value.label("total"),
                (model.value if raw else model.minimum).label("minimum"),
                (model.value if raw else model.maximum).label("maximum"),
            ).where(*filters)
        )
    return union_all(*queries).subquery()


def latest_reading(db, server_id, name, scope, since, until):
    candidates = []
    for model in (Metric, MetricRollup):
        raw = model is Metric
        at = model.captured_at if raw else model.last_at
        value = model.value if raw else model.last_value
        row = db.execute(
            select(at, value)
            .where(
                model.server_id == server_id,
                model.name == name,
                model.scope == scope,
                model.captured_at >= since - timedelta(days=1)
                if not raw
                else model.captured_at >= since,
                model.captured_at <= until,
                at >= since,
                at <= until,
            )
            .order_by(model.captured_at.desc(), model.id.desc())
            .limit(1)
        ).first()
        if row:
            candidates.append(row)
    return max(candidates, key=lambda r: r[0]) if candidates else None


def history_series(
    db,
    server,
    names,
    scopes,
    since,
    until,
    bucket_seconds=None,
    latest_exact=True,
    pairs=None,
):
    pairs = list(dict.fromkeys(pairs or [(n, s) for n in names for s in scopes]))
    if not pairs:
        return []
    bucket_seconds = bucket_seconds or max(
        1,
        int(
            ((until - since).total_seconds() + MAX_METRIC_POINTS - 1)
            // MAX_METRIC_POINTS
        ),
    )
    c = source_query(server.id, names, scopes, since, until, pairs).c
    bucket = cast(c.start / bucket_seconds, Integer)
    rows = db.execute(
        select(
            c.name,
            c.scope,
            bucket.label("bucket"),
            func.max(c.last_at).label("at"),
            func.min(c.start).label("start"),
            func.max(c.end).label("end"),
            func.sum(c.n).label("n"),
            func.sum(c.total).label("total"),
            func.min(c.minimum).label("low"),
            func.max(c.maximum).label("high"),
            c.resolution.label("resolution"),
        )
        .group_by(c.name, c.scope, bucket, c.resolution)
        .order_by(c.name, c.scope, bucket, c.resolution)
    ).all()
    grouped = defaultdict(list)
    for r in rows:
        grouped[r.name, r.scope].append(r)
    result = []
    for name, scope in pairs:
        values = grouped[name, scope]
        points = []
        for r in values:
            at = min(until, max(since, r.at))
            partial = r.resolution > 1 and (
                r.start < epoch(since) or r.end > epoch(until)
            )
            points.append(
                {
                    "t": at.isoformat() + "Z",
                    "v": r.low if name == "drive.smart_passed" else r.total / r.n,
                    "min": r.low,
                    "max": r.high,
                    "n": r.n,
                    "resolution_seconds": max(r.resolution, bucket_seconds),
                    "coverage_start": datetime.fromtimestamp(
                        r.start, timezone.utc
                    ).isoformat(),
                    "coverage_end": datetime.fromtimestamp(
                        r.end, timezone.utc
                    ).isoformat(),
                    "partial": partial,
                }
            )
        points.sort(key=lambda p: p["t"])
        latest = (
            latest_reading(db, server.id, name, scope, since, until)
            if latest_exact and points and name != "drive.smart_passed"
            else None
        )
        if (
            latest
            and not points[-1]["partial"]
            and points[-1]["t"] == latest[0].isoformat() + "Z"
        ):
            points[-1]["v"] = latest[1]
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
                "partial_bucket_count": sum(p["partial"] for p in points),
                "start": since.isoformat() + "Z",
                "end": until.isoformat() + "Z",
                "expected_interval_seconds": server.smart_interval_minutes * 60
                if name.startswith("drive.") and not name.startswith("drive.io.")
                else server.poll_interval_seconds,
                "points": points,
            }
        )
    return result


def compact_history(
    db, lock, now=None, max_windows=48, raw_days=RAW_DAYS, hourly_days=HOURLY_DAYS
):
    """Compact one complete hour/day per transaction, with bounded work per run.

    Insertion and source deletion commit together; interrupted work is rolled
    back and can safely retry without double counting.
    """
    now = now or datetime.utcnow()
    count = 0
    for model, resolution, cutoff, source_resolution in [
        (Metric, 3600, now - timedelta(days=raw_days), None),
        (MetricRollup, 86400, now - timedelta(days=hourly_days), 3600),
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
