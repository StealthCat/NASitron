import json
import re
from datetime import datetime, timedelta
from html import unescape
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.main import app
from app.db import SessionLocal
from app.models import (
    Server,
    CurrentState,
    Enclosure,
    SavedView,
    WebUser,
    MonitorEvent,
    MaintenanceAction,
    SnapshotInventory,
)
from app.security import csrf_token, hash_password
from app.settings_store import get_int
from app.collector import SSHCollector, CollectorError


def login(client, name="ci-admin", password="ci-password-strong"):
    assert (
        client.post(
            "/login",
            data={
                "username": name,
                "password": password,
                "csrf_token": csrf_token(),
                "next": "/",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )


def post(client, route, **data):
    return client.post(
        route, data={**data, "csrf_token": csrf_token()}, follow_redirects=False
    )


def test_personal_views_enclosures_policy_and_input_bounds():
    with TestClient(app) as client:
        login(client)
        with SessionLocal() as db:
            s = Server(
                name="refinement-test", host="localhost", username="test", enabled=False
            )
            db.add(s)
            db.flush()
            sid = s.id
            db.add(
                CurrentState(
                    server_id=sid,
                    payload_json=json.dumps(
                        {"drives": [{"serial": "stable", "path": "/dev/sda",
                            "size_bytes": 1024**4, "model": "Bay test HDD",
                            "smart": {"temperature_c": 35, "power_on_hours": 8760,
                                      "sampled_at": "2026-10-02T12:00:00Z"}}]}
                    ),
                )
            )
            user = WebUser(
                username="refinement-viewer",
                password_hash=hash_password("viewer-password"),
                role="viewer",
                is_admin=False,
                enabled=True,
            )
            db.add(user)
            db.commit()
            uid = user.id
        try:
            for bad in ["²", "９", "9" * 100, "-1", "0", "1.0"]:
                for route in ["timeline", "drive-bays"]:
                    assert (
                        client.get("/" + route, params={"server": bad}).status_code
                        == 400
                    )
            assert (
                post(
                    client, "/enclosures", server_id=sid, name="Rack", rows=1, columns=3
                ).status_code
                == 303
            )
            with SessionLocal() as db:
                eid = db.scalar(select(Enclosure.id).where(Enclosure.server_id == sid))
            assert (
                post(
                    client, f"/enclosures/{eid}/assign", slot=1, identity="stable"
                ).status_code
                == 303
            )
            assert (
                post(
                    client, f"/enclosures/{eid}/assign", slot=2, identity="stable"
                ).status_code
                == 409
            )
            assert (
                post(
                    client, f"/enclosures/{eid}/assign", slot=4, identity="stable"
                ).status_code
                == 400
            )
            # Assignments in any enclosure remove a drive from every available list.
            assert post(client, "/enclosures", server_id=sid, name="Second rack", rows=1, columns=2).status_code == 303
            page = client.get(f"/drive-bays?server={sid}").text
            assert '<option value="stable">' not in page
            assert "Rack · Bay 1" in page
            assert "Unlabeled" not in page
            bay, detected = page.split("<h3>Detected drives</h3>")
            for detail in ("Bay test HDD", "1.0 TiB", "35°C", "1 year", "2026-10-02"):
                assert detail in bay and detail in detected
            assert post(client, f"/enclosures/{eid}/assign", slot=1, identity="").status_code == 303
            page = client.get(f"/drive-bays?server={sid}").text
            assert page.count('<option value="stable">') == 2
            assert "Unlabeled" in page
            assert post(client, f"/enclosures/{eid}/assign", slot=1, identity="stable").status_code == 303
            with SessionLocal() as db:
                db.get(CurrentState, sid).payload_json = '{"drives":[]}'
                db.commit()
            page = client.get(f"/drive-bays?server={sid}").text
            assert "Missing drive" in page and "Empty slot" in page
            with SessionLocal() as db:
                before = get_int(db, "raw_history_days", 7)
            assert (
                post(
                    client, "/settings/history-policy", raw_days=2, hourly_days=5
                ).status_code
                == 200
            )
            with SessionLocal() as db:
                assert get_int(db, "raw_history_days", 7) == before
            assert (
                post(
                    client,
                    "/settings/history-policy",
                    raw_days=2,
                    hourly_days=5,
                    mode="apply",
                ).status_code
                == 303
            )
            with SessionLocal() as db:
                assert get_int(db, "raw_history_days", 7) == 2
            assert (
                post(
                    client,
                    "/settings/history-policy",
                    raw_days=7,
                    hourly_days=30,
                    mode="apply",
                ).status_code
                == 303
            )
            assert (
                post(
                    client, "/views", name="Admin view", path="/disk-io?server_id=1"
                ).status_code
                == 303
            )
            with SessionLocal() as db:
                vid = db.scalar(
                    select(SavedView.id).where(SavedView.name == "Admin view")
                )
            login(client, "refinement-viewer", "viewer-password")
            assert post(client, f"/views/{vid}/delete").status_code == 404
            assert (
                post(
                    client,
                    "/views",
                    name="Own view",
                    path="/drives?table_drives_q=test",
                ).status_code
                == 303
            )
            assert (
                post(
                    client, "/views", name="External", path="//example.com/"
                ).status_code
                == 400
            )
            assert (
                post(
                    client, "/views", name="External", path="javascript:alert(1)"
                ).status_code
                == 400
            )
            assert (
                post(client, "/preferences", timezone="America/New_York").status_code
                == 303
            )
            assert 'data-timezone="America/New_York"' in client.get("/preferences").text
            assert (
                post(client, "/preferences", timezone="invalid/timezone").status_code
                == 400
            )
            assert (
                post(
                    client,
                    "/enclosures",
                    server_id=sid,
                    name="Forbidden",
                    rows=1,
                    columns=1,
                ).status_code
                == 403
            )
            assert (
                post(
                    client,
                    "/settings/history-policy",
                    raw_days=1,
                    hourly_days=1,
                    mode="apply",
                ).status_code
                == 403
            )
        finally:
            with SessionLocal() as db:
                db.delete(db.get(Server, sid))
                db.delete(db.get(WebUser, uid))
                db.delete(db.get(SavedView, vid))
                db.commit()


def test_timeline_cursor_and_explicit_metric_pairs():
    with TestClient(app) as client:
        login(client)
        with SessionLocal() as db:
            s = Server(
                name="cursor-test", host="localhost", username="test", enabled=False
            )
            db.add(s)
            db.flush()
            sid = s.id
            at = datetime.utcnow() - timedelta(minutes=1)
            for i in range(65):
                db.add(
                    MonitorEvent(
                        server_id=sid,
                        kind="replacement",
                        captured_at=at,
                        message=f"event-{i:03}",
                    )
                )
            db.add(
                MaintenanceAction(
                    server_id=sid,
                    action="replace",
                    success=False,
                    state="failed",
                    pool="failed-pool",
                    created_at=at,
                )
            )
            db.commit()
        try:
            page = client.get(f"/timeline?server={sid}").text
            next_url = unescape(re.search(r'href="([^"]+)">Older', page)[1])
            second = client.get(next_url).text
            found = re.findall(r"event-\d{3}", page + second)
            assert len(found) == 65 and len(set(found)) == 65
            assert "failed-pool: command failed" in page + second
            assert client.get("/timeline?before=bad").status_code == 400
            url = f"/api/servers/{sid}/metrics/batch"
            pairs = [
                ("pairs", json.dumps(["drive.io.read_bps", "a"])),
                ("pairs", json.dumps(["drive.io.write_bps", "b"])),
            ]
            response = client.get(url, params=pairs)
            assert response.status_code == 200 and len(response.json()["series"]) == 2
            assert (
                client.get(
                    url, params={"pairs": '{"drive.io.read_bps":"a"}'}
                ).status_code
                == 400
            )
        finally:
            with SessionLocal() as db:
                db.delete(db.get(Server, sid))
                db.commit()


def test_inventory_preserves_last_good_rows_on_failure():
    from app.inventory_store import sync_inventory

    with TestClient(app):
        with SessionLocal() as db:
            s = Server(
                name="normalized-test", host="localhost", username="test", enabled=False
            )
            db.add(s)
            db.flush()
            sync_inventory(
                db,
                s.id,
                {
                    "fresh": True,
                    "captured_at": "2026-09-01",
                    "rows": [{"name": "tank@one", "used": 99}],
                },
            )
            db.commit()
            sync_inventory(
                db, s.id, {"fresh": False, "error": "unavailable", "rows": []}
            )
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(SnapshotInventory)
                    .where(SnapshotInventory.server_id == s.id)
                )
                == 1
            )
            db.delete(s)
            db.commit()


def test_collector_global_deadline_stops_before_remote_command():
    collector = SSHCollector(Mock())
    collector.client = Mock()
    collector.collection_deadline = 0
    with pytest.raises(CollectorError, match="deadline"):
        collector.run("true")
    collector.client.exec_command.assert_not_called()


def test_worker_reports_failures_and_running_state(monkeypatch):
    from app import scheduler

    observed = []

    def fail(db, sid):
        observed.append(scheduler.collector_states()[sid]["state"])
        raise RuntimeError("test")

    monkeypatch.setattr(scheduler, "collect_server", fail)
    scheduler._collect_worker(987654)
    assert observed == ["running"]
    assert scheduler.collector_states()[987654]["state"] == "failed"
    with scheduler._lock:
        scheduler._states.pop(987654)


def test_slow_collection_cache_keeps_fast_counters_live(monkeypatch):
    collector = SSHCollector(Mock(zpool_status_json_supported=None))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return {"stdout": "", "stderr": "", "exit": 0}

    monkeypatch.setattr(collector, "run", run)
    first = collector.collect()
    collector.previous = {"_detail_cache": first["detail_cache"]}
    commands.clear()
    second = collector.collect()
    assert second["details_cached"]
    assert "uname -r" not in commands
    assert "cat /proc/loadavg" in commands
    collector.previous["_detail_cache"]["captured_at"] = 0
    commands.clear()
    collector.collect()
    assert "uname -r" in commands


def test_housekeeping_failure_is_recorded(monkeypatch):
    from app import scheduler
    from app.settings_store import get_setting

    def fail(db):
        raise RuntimeError("synthetic housekeeping failure")

    monkeypatch.setattr(scheduler, "prune_history", fail)
    scheduler._housekeeping()
    with SessionLocal() as db:
        assert get_setting(db, "history_housekeeping_state") == "failed"
        assert "synthetic" in get_setting(db, "history_housekeeping_error")
        assert float(get_setting(db, "history_housekeeping_seconds")) >= 0
