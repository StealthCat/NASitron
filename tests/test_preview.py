from datetime import datetime, timedelta
import gzip
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.db import SessionLocal, init_db
from app.main import app
from app.models import (
    Server,
    WebUser,
    Alert,
    MaintenanceWindow,
    MaintenanceAction,
    CurrentState,
)
from app.security import csrf_token, hash_password
from app.experience import (
    server_state,
    smart_state,
    forecast,
    operation_status,
    update_operations,
)
from app.parser import parse_pool_status, parse_snapshot_inventory
from app.insights import bundle_values, read_bundle
from app.settings_store import set_setting


def test_health_does_not_confuse_unknown_stale_and_disabled_with_healthy():
    server = SimpleNamespace(
        enabled=True,
        last_collection_state="ok",
        last_ok_at=None,
        poll_interval_seconds=60,
    )
    assert smart_state({})[0] == "unknown"
    assert smart_state({"smart_passed": True, "stale": True})[0] == "warning"
    assert smart_state({"smart_passed": True, "pending_sectors": 3})[0] == "warning"
    assert server_state(server, {})[0] == "unknown"
    snapshot = {
        "collection": {
            "captured_at": (datetime.utcnow() - timedelta(hours=1)).isoformat()
        }
    }
    assert server_state(server, snapshot)[0] == "warning"
    server.enabled = False
    assert server_state(server, snapshot)[1] == "Monitoring disabled"


def test_scan_parser_keeps_progress_and_completion_is_not_command_success():
    parsed = parse_pool_status("""  pool: tank
 state: ONLINE
  scan: resilver in progress since Sun Sep 27 12:00:00 2026
        10G scanned at 500M/s, 5G issued at 200M/s
        5G resilvered, 50.00% done, 00:02:00 to go
config:
        NAME STATE READ WRITE CKSUM
        tank ONLINE 0 0 0
""")
    status = operation_status({"status": parsed})
    assert status["active"] and status["percent"] == 50 and status["eta"] == "00:02:00"
    init_db()
    with SessionLocal() as db:
        server = Server(
            name="preview-operation",
            host="localhost",
            username="nasitron",
            enabled=False,
        )
        db.add(server)
        db.flush()
        action = MaintenanceAction(
            server_id=server.id,
            action="zpool_replace",
            pool="tank",
            success=True,
            state="accepted",
        )
        db.add(action)
        db.flush()
        snapshot = {
            "pools": [
                {
                    "name": "tank",
                    "health": "ONLINE",
                    "status": {
                        "scan": "resilvered 10G in 1s with 0 errors on Sun Sep 20 12:00:00 2026",
                        "vdevs": [{"state": "ONLINE"}],
                    },
                }
            ]
        }
        update_operations(db, server, snapshot)
        assert action.completed_at is None
        snapshot["pools"][0]["status"] = parsed
        update_operations(db, server, snapshot)
        assert action.state == "resilvering"
        snapshot["pools"][0]["status"] = {
            "scan": "resilvered 10G in 1s with 0 errors",
            "vdevs": [{"state": "ONLINE"}],
        }
        update_operations(db, server, snapshot)
        assert action.state == "complete" and action.completed_at
        db.rollback()


def test_forecast_requires_history_and_reports_uncertainty():
    now = datetime.utcnow()
    assert forecast([(now, 20)])["days"] is None
    points = [(now - timedelta(days=10 - i), 20 + i) for i in range(10)]
    result = forecast(points, 80)
    assert result["days"] == 51 and result["fit"] == 1 and result["range"] == [51, 51]
    assert forecast([(t, 90) for t, _ in points], 80)["days"] == 0
    assert forecast([(t, 20) for t, _ in points])["days"] is None


def test_snapshot_inventory_rejects_incomplete_output():
    assert not parse_snapshot_inventory({"exit": 0, "stdout": "broken"}, "now")["fresh"]
    assert not parse_snapshot_inventory({"exit": 0, "stdout_truncated": True}, "now")[
        "fresh"
    ]
    inventory = parse_snapshot_inventory(
        {"exit": 0, "stdout": "tank/data@daily\t1720000000\t42\t100\n"}, "now"
    )
    assert inventory["rows"][0]["used"] == 42 and inventory["fresh"]


def login(client, username="ci-admin", password="ci-password-strong"):
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_token(),
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_roles_guard_backend_and_new_pages_render():
    with TestClient(app) as client:
        login(client)
        for path in [
            "/operations",
            "/snapshots",
            "/forecasts",
            "/settings/tools",
            "/alerts?state=all&page=2&q=abc",
        ]:
            assert client.get(path).status_code == 200
        with SessionLocal() as db:
            user = WebUser(
                username="preview-viewer",
                password_hash=hash_password("preview-password"),
                role="viewer",
                enabled=True,
                is_admin=False,
            )
            db.add(user)
            db.commit()
            uid = user.id
        try:
            login(client, "preview-viewer", "preview-password")
            for path in [
                "/settings",
                "/users",
                "/servers/new",
                "/servers/1/replace-drive",
                "/api/enrollments/anything/status",
            ]:
                assert client.get(path).status_code == 403
            for path in [
                "/servers/1/poll",
                "/servers/1/replace-drive",
                "/servers/1/delete",
                "/settings",
                "/alerts/1/ack",
            ]:
                assert (
                    client.post(path, data={"csrf_token": csrf_token()}).status_code
                    == 403
                )
            assert client.get("/operations").status_code == 200
            with SessionLocal() as db:
                db.get(WebUser, uid).role = "operator"
                db.commit()
            assert client.get("/settings").status_code == 403
            assert (
                client.post(
                    "/servers/999999/maintenance-window",
                    data={"minutes": 60, "csrf_token": csrf_token()},
                ).status_code
                == 404
            )
            assert (
                client.post(
                    "/servers/999999/delete", data={"csrf_token": csrf_token()}
                ).status_code
                == 403
            )
        finally:
            with SessionLocal() as db:
                db.delete(db.get(WebUser, uid))
                db.commit()


def test_notification_windows_and_snoozes_preserve_pending_alerts(monkeypatch):
    import app.alerts as alerts

    init_db()
    now = datetime.utcnow()
    with SessionLocal() as db:
        set_setting(db, "smtp_enabled", "true")
        db.commit()
        server = Server(
            name="preview-muted", host="localhost", username="nasitron", enabled=False
        )
        db.add(server)
        db.flush()
        alert = Alert(
            server_id=server.id,
            key="preview",
            title="Test",
            message="Test",
            active=True,
        )
        db.add(alert)
        window = MaintenanceWindow(
            server_id=server.id,
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=1),
            actor="test",
            reason="test",
        )
        db.add(window)
        db.flush()
        sent = []
        monkeypatch.setattr(
            alerts, "send_email", lambda *a, **kw: sent.append(kw) or True
        )
        assert alerts.deliver_pending_notifications(db) == 0
        assert alert.last_notified_at is None
        window.ends_at = now - timedelta(seconds=1)
        alert.snoozed_until = now + timedelta(minutes=1)
        db.flush()
        assert alerts.deliver_pending_notifications(db) == 0
        alert.snoozed_until = None
        db.flush()
        assert alerts.deliver_pending_notifications(db) >= 1 and sent
        db.rollback()
        set_setting(db, "smtp_enabled", "false")
        db.commit()


def test_tuning_comparison_redacts_sensitive_properties():
    data = {
        "format": "nasitron-support-bundle",
        "diagnostics": {
            "zfs_get_all": {
                "stdout": "tank\tkeylocation\tfile:///secret\tlocal\ntank\tcompression\tzstd\tlocal",
                "stderr": "",
                "exit": 0,
            }
        },
    }
    values = bundle_values(data)
    assert "file:///secret" not in str(values)
    assert values["zfs_get_all/tank/compression"] == "zstd · local"


def test_compressed_bundle_expansion_limit():
    upload = UploadFile(
        filename="bomb.gz", file=io.BytesIO(gzip.compress(b" " * (33 * 1024 * 1024)))
    )
    with pytest.raises(ValueError):
        __import__("asyncio").run(read_bundle(upload))


def test_backup_round_trip_and_non_overwrite(tmp_path, monkeypatch):
    from app import backup, config, instance_lock

    data = tmp_path / "data"
    data.mkdir()
    database = data / "nasitron.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE proof(value TEXT)")
        db.execute("INSERT INTO proof VALUES ('retained')")
    (data / "known_hosts").write_text("test-key")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(instance_lock, "DATA_DIR", data)
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///" + str(database))
    monkeypatch.setattr(config, "SECRET_KEY", "preview-secret")
    monkeypatch.setattr(config, "KNOWN_HOSTS_PATH", data / "known_hosts")
    archive = tmp_path / "backup.nsb"
    destination = tmp_path / "restore"
    backup.create(archive, "strong-preview-password")
    assert b"preview-secret" not in archive.read_bytes()
    with pytest.raises(ValueError):
        backup.restore(archive, destination, "wrong-password")
    backup.restore(archive, destination, "strong-preview-password")
    assert (destination / "recovery-secret-key").read_text() == "preview-secret"
    with sqlite3.connect(destination / "nasitron.db") as db:
        assert db.execute("SELECT value FROM proof").fetchone()[0] == "retained"
    with pytest.raises(ValueError):
        backup.restore(archive, destination, "strong-preview-password")


def test_drive_detail_and_label_roundtrip():
    with TestClient(app) as client:
        login(client)
        with SessionLocal() as db:
            server = Server(
                name="preview-drive",
                host="localhost",
                username="nasitron",
                enabled=False,
            )
            db.add(server)
            db.flush()
            sid = server.id
            db.add(
                CurrentState(
                    server_id=sid,
                    payload_json=json.dumps(
                        {
                            "drives": [
                                {
                                    "path": "/dev/sda",
                                    "serial": "SER-1",
                                    "size_bytes": 100,
                                    "model": "Drive",
                                    "smart": {},
                                    "zfs_memberships": [],
                                }
                            ]
                        }
                    ),
                )
            )
            db.commit()
        try:
            response = client.get(f"/servers/{sid}/drive?identity=SER-1")
            assert response.status_code == 200 and "Unknown" in response.text
            response = client.post(
                f"/servers/{sid}/drive-label",
                data={
                    "csrf_token": csrf_token(),
                    "identity": "SER-1",
                    "label": "Bay 25",
                },
            )
            assert response.status_code == 200 and "Bay 25" in response.text
        finally:
            with SessionLocal() as db:
                db.delete(db.get(Server, sid))
                db.commit()


def test_existing_database_gets_additive_role_and_operation_columns(
    tmp_path, monkeypatch
):
    from sqlalchemy import create_engine, inspect, text
    import app.db as database

    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE web_users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, is_admin BOOLEAN, enabled BOOLEAN, session_version INTEGER, last_login_at DATETIME, created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO web_users (id, username, is_admin) VALUES (1, 'legacy-admin', 1)"
            )
        )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT is_admin, role FROM web_users WHERE id=1")
        ).one()
        assert row == (1, "viewer")  # is_admin still controls effective admin role.
    assert "completed_at" in {
        c["name"] for c in inspect(engine).get_columns("maintenance_actions")
    }
    engine.dispose()
