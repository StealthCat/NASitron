from datetime import datetime, timedelta
from threading import RLock

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Server, Metric, MetricRollup
from app.history import compact_history, history_series, delete_chunks


@pytest.fixture
def history_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        server = Server(
            name="history", host="localhost", username="test", enabled=False
        )
        db.add(server)
        db.commit()
        yield db, server
    engine.dispose()


def test_rollups_preserve_extremes_counts_and_worst_health(history_db):
    db, server = history_db
    now = datetime(2026, 9, 30, 12)
    at = now - timedelta(days=40)
    for name, values in [
        ("drive.io.read_bps", [10, 1000, 30]),
        ("drive.smart_passed", [1, 0, 1]),
    ]:
        for i, value in enumerate(values):
            db.add(
                Metric(
                    server_id=server.id,
                    name=name,
                    scope="disk",
                    captured_at=at + timedelta(minutes=i),
                    value=value,
                )
            )
    db.commit()
    compact_history(db, RLock(), now)
    assert db.scalar(select(func.count()).select_from(Metric)) == 0
    rows = db.scalars(select(MetricRollup)).all()
    assert len(rows) == 2 and all(
        r.resolution == 86400 and r.sample_count == 3 for r in rows
    )
    data = history_series(
        db,
        server,
        ["drive.io.read_bps", "drive.smart_passed"],
        ["disk"],
        at - timedelta(days=1),
        now,
    )
    io, health = data
    assert io["sample_count"] == 3
    assert io["points"][0]["min"] == 10 and io["points"][0]["max"] == 1000
    assert io["points"][0]["v"] == 30
    assert health["points"][0]["v"] == 0
    compact_history(db, RLock(), now)
    assert db.scalar(select(func.sum(MetricRollup.sample_count))) == 6
    # Late backfill merges with the existing daily summary rather than replacing it.
    db.add(
        Metric(
            server_id=server.id,
            name="drive.io.read_bps",
            scope="disk",
            captured_at=at + timedelta(minutes=3),
            value=2000,
        )
    )
    db.commit()
    compact_history(db, RLock(), now)
    io = history_series(
        db, server, ["drive.io.read_bps"], ["disk"], at - timedelta(days=1), now
    )[0]
    assert io["sample_count"] == 4 and io["points"][0]["max"] == 2000
    assert io["points"][0]["v"] == 2000


def test_mixed_raw_and_hourly_history_and_scopes(history_db):
    db, server = history_db
    now = datetime(2026, 9, 30, 12)
    for days, value in [(10, 20), (10, 40), (0, 90)]:
        db.add(
            Metric(
                server_id=server.id,
                name="system.load1",
                scope="",
                captured_at=now - timedelta(days=days),
                value=value,
            )
        )
    db.commit()
    compact_history(db, RLock(), now)
    data = history_series(
        db,
        server,
        ["system.load1"],
        ["", "missing"],
        now - timedelta(days=11),
        now,
        latest_exact=False,
    )
    assert data[0]["sample_count"] == 3
    assert [p["v"] for p in data[0]["points"]] == [30, 90]
    assert data[0]["retained_resolution_seconds"] == 3600
    assert data[1]["points"] == []
    assert (
        delete_chunks(
            db, RLock(), MetricRollup, MetricRollup.captured_at < now, batch=1
        )
        == 1
    )


def test_rollup_failure_keeps_source_samples(history_db, monkeypatch):
    db, server = history_db
    now = datetime(2026, 9, 30, 12)
    db.add(
        Metric(
            server_id=server.id,
            name="system.load1",
            scope="",
            captured_at=now - timedelta(days=10),
            value=5,
        )
    )
    db.commit()
    original = db.commit

    def fail():
        raise RuntimeError("interrupted")

    monkeypatch.setattr(db, "commit", fail)
    with pytest.raises(RuntimeError):
        compact_history(db, RLock(), now)
    db.rollback()
    monkeypatch.setattr(db, "commit", original)
    assert db.scalar(select(func.count()).select_from(Metric)) == 1
    assert db.scalar(select(func.count()).select_from(MetricRollup)) == 0


def test_overlapping_summary_bounds_are_explicit_and_raw_keeps_own_resolution(
    history_db,
):
    db, server = history_db
    at = datetime(2026, 8, 1)
    db.add(
        MetricRollup(
            server_id=server.id,
            name="system.load1",
            scope="",
            captured_at=at,
            resolution=86400,
            sample_count=2,
            total=30,
            minimum=10,
            maximum=20,
            last_at=at + timedelta(hours=20),
            last_value=20,
        )
    )
    db.commit()
    start, end = at + timedelta(hours=10), at + timedelta(hours=12)
    result = history_series(db, server, ["system.load1"], [""], start, end)[0]
    assert result["sample_count"] == 2
    point = result["points"][0]
    assert point["partial"] and point["v"] == 15
    assert point["t"] == end.isoformat() + "Z"
    assert result["partial_bucket_count"] == 1
    recent = at + timedelta(days=40)
    db.add(
        Metric(
            server_id=server.id,
            name="system.load1",
            scope="",
            captured_at=recent,
            value=7,
        )
    )
    db.commit()
    result = history_series(
        db, server, ["system.load1"], [""], at, recent, bucket_seconds=60
    )[0]
    assert [p["resolution_seconds"] for p in result["points"]] == [86400, 60]


def test_configurable_compaction_windows(history_db):
    db, server = history_db
    now = datetime(2026, 9, 30)
    db.add(
        Metric(
            server_id=server.id,
            name="system.load1",
            scope="",
            captured_at=now - timedelta(days=3),
            value=4,
        )
    )
    db.commit()
    compact_history(db, RLock(), now, raw_days=4, hourly_days=5)
    assert db.scalar(select(func.count()).select_from(Metric)) == 1
    compact_history(db, RLock(), now, raw_days=1, hourly_days=2)
    assert db.scalar(select(MetricRollup.resolution)) == 86400
