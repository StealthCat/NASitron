from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re

from fastapi.testclient import TestClient
from app.crypto import decrypt
from app.db import SessionLocal
from app.main import app
from app.models import CurrentState, Metric, RemoteEnrollment, Server, WebUser
from app.security import csrf_token
from app.settings_store import get_setting, set_setting


def _login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={
            "username": "ci-admin",
            "password": "ci-password-strong",
            "csrf_token": csrf_token(),
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "nasitron_session" in response.cookies


def test_healthz_is_public_and_dashboard_uses_form_login():
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        installer = client.get("/install-remote.sh")
        assert installer.status_code == 200
        assert installer.headers["content-type"].startswith("text/x-shellscript")
        assert "attachment;" in installer.headers["content-disposition"]
        assert installer.text.startswith("#!/usr/bin/env bash")
        assert "__NASITRON_ROOT_HELPER__" in installer.text
        assert "Generating a dedicated Ed25519 SSH keypair for NASitron" in installer.text
        assert "NASITRON GENERATED PRIVATE KEY" in installer.text
        assert installer.text.index("Registering this NAS with NASitron") < installer.text.index("Validating NASitron account permissions")
        assert "--retry 4" in installer.text

        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

        login_page = client.get("/login")
        assert login_page.status_code == 200
        assert 'name="username"' in login_page.text
        assert "WWW-Authenticate" not in login_page.headers

        _login(client)
        assert client.get("/").status_code == 200


def test_bad_login_is_rejected_without_basic_auth_challenge():
    with TestClient(app) as client:
        response = client.post(
            "/login",
            data={
                "username": "ci-admin",
                "password": "wrong-password",
                "csrf_token": csrf_token(),
                "next": "/",
            },
        )
        assert response.status_code == 401
        assert "Invalid username or password" in response.text
        assert "WWW-Authenticate" not in response.headers


def test_logout_clears_session():
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/logout",
            data={"csrf_token": csrf_token()},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert client.get("/", follow_redirects=False).status_code == 303


def test_mutating_route_rejects_bad_csrf():
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/settings",
            data={"csrf_token": "not-valid"},
        )
        assert response.status_code == 403


def test_metric_history_is_bounded_and_keeps_latest_sample():
    with SessionLocal() as db:
        server = Server(
            name="metric-downsample-test",
            host="127.0.0.1",
            username="nasitron",
            auth_type="password",
            enabled=False,
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        start = datetime.utcnow() - timedelta(hours=1)
        db.add_all(
            [
                Metric(
                    server_id=server.id,
                    captured_at=start + timedelta(seconds=i),
                    name="system.load1",
                    scope="",
                    value=float(i),
                )
                for i in range(2500)
            ]
        )
        db.commit()
        server_id = server.id

    with TestClient(app) as client:
        _login(client)
        response = client.get(
            f"/api/servers/{server_id}/metrics?name=system.load1&hours=24",
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["sample_count"] == 2500
        assert payload["returned_points"] <= 1201
        assert payload["points"][-1]["v"] == 2499.0

    with SessionLocal() as db:
        server = db.get(Server, server_id)
        db.delete(server)
        db.commit()



def test_sidebar_pages_are_real_routes_and_drives_page_shows_all_25():
    drives = [
        {
            "name": f"sd{chr(ord('a') + index)}",
            "kname": f"sd{chr(ord('a') + index)}",
            "path": f"/dev/sd{chr(ord('a') + index)}",
            "size_bytes": 1_000_000_000_000,
            "rotational": True,
            "transport": "sata",
            "model": "Test Disk",
            "serial": f"SERIAL-{index:02d}",
            "smart": {"smart_passed": True, "temperature_c": 30 + index % 5},
            "zfs_memberships": [],
        }
        for index in range(25)
    ]
    payload = {
        "system": {
            "hostname": "big-nas",
            "memory": {"used_pct": 10},
            "zfs_version": "zfs-2.2",
        },
        "arc": {"hit_rate_pct": 99.0},
        "pools": [],
        "datasets": [],
        "drives": drives,
        "collection": {"stale_subsystems": [], "errors": []},
    }

    with SessionLocal() as db:
        server = Server(
            name="twenty-five-drive-test",
            host="127.0.0.2",
            username="nasitron",
            auth_type="password",
            enabled=False,
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        db.add(
            CurrentState(
                server_id=server.id,
                captured_at=datetime.utcnow(),
                payload_json=__import__("json").dumps(payload),
            )
        )
        db.commit()
        server_id = server.id

    try:
        with TestClient(app) as client:
            _login(client)
            for path in (
                "/servers",
                "/pools",
                "/tanks",
                "/drives",
                "/alerts",
                "/maintenance",
                "/settings",
            ):
                assert client.get(path).status_code == 200

            response = client.get("/drives")
            assert response.status_code == 200
            assert "25 shown" in response.text
            assert "/dev/sda" in response.text
            assert "/dev/sdy" in response.text
            assert "SERIAL-24" in response.text
    finally:
        with SessionLocal() as db:
            server = db.get(Server, server_id)
            if server:
                db.delete(server)
                db.commit()



def test_add_server_page_shows_environment_specific_curl_instructions():
    with TestClient(app) as client:
        _login(client)

        with SessionLocal() as db:
            set_setting(db, "tls_mode", "internal")
            set_setting(db, "tls_domain", "nasitron.internal")
            db.commit()

        response = client.get("/servers/new")
        assert response.status_code == 200
        assert "Prepare the NAS with one command" in response.text
        assert "<details" in response.text
        assert "/api/enroll/" in response.text
        assert "/install.sh?token=" in response.text
        assert "curl -kfsSL" in response.text
        assert "sha256sum -c -" in response.text
        assert "sudo bash" in response.text
        assert "--public-key" not in response.text
        assert "--enroll-url" not in response.text
        assert "--enroll-secret" not in response.text
        assert "registers the server automatically" in response.text
        assert "Installer SHA-256" in response.text
        assert "/api/enroll/" in response.text
        assert "/install.sh?token=" in response.text

        with SessionLocal() as db:
            set_setting(db, "tls_mode", "acme")
            set_setting(db, "tls_domain", "nas.example.com")
            db.commit()

        response = client.get("/servers/new")
        assert response.status_code == 200
        assert "curl -fsSL" in response.text
        assert "| sudo bash" in response.text
        assert "curl -kfsSL" not in response.text
        assert "--public-key" not in response.text
        assert "--enroll-url" not in response.text
        assert "--enroll-secret" not in response.text
        match = re.search(
            r'(http://testserver/api/enroll/[A-Za-z0-9_-]+/install\.sh\?token=[0-9a-f]{64})',
            response.text,
        )
        assert match is not None
        bootstrap = client.get(match.group(1))
        assert bootstrap.status_code == 200
        assert "NASITRON_SSH_PUBLIC_KEY=" in bootstrap.text
        assert "NASITRON_ENROLL_URL=" in bootstrap.text
        assert "NASITRON_ENROLL_SECRET=" in bootstrap.text
        assert "nasitron-enrollment-" in bootstrap.text

        with SessionLocal() as db:
            set_setting(db, "tls_mode", "internal")
            set_setting(db, "tls_domain", "localhost")
            db.commit()


def test_remote_enrollment_callback_creates_server_and_is_one_time():
    created_server_id = None
    enrollment_id = None
    try:
        with TestClient(app) as client:
            _login(client)
            with SessionLocal() as db:
                set_setting(db, "tls_mode", "acme")
                set_setting(db, "tls_domain", "nas.example.com")
                db.commit()

            page = client.get("/servers/new")
            assert page.status_code == 200
            match = re.search(r'data-enrollment-id="([A-Za-z0-9_-]+)"', page.text)
            assert match is not None
            enrollment_id = match.group(1)

            with SessionLocal() as db:
                enrollment = db.get(RemoteEnrollment, enrollment_id)
                assert enrollment is not None
                assert enrollment.used_at is None
                secret = decrypt(enrollment.secret_enc)
                assert secret
                assert "nasitron-enrollment-" in enrollment.public_key

            payload = json.dumps(
                {
                    "host": "127.0.0.254",
                    "host_key_fingerprint": "SHA256:test-fingerprint",
                    "hostname": "auto-enrolled-test",
                    "port": 22,
                    "username": "nasitron",
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            signature = hmac.new(
                secret.encode(),
                payload,
                hashlib.sha256,
            ).hexdigest()

            callback = client.post(
                f"/api/enroll/{enrollment_id}/complete",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-NASitron-Enrollment-Signature": signature,
                },
            )
            assert callback.status_code == 200
            result = callback.json()
            assert result["status"] == "registered"
            created_server_id = result["server_id"]

            status = client.get(f"/api/enrollments/{enrollment_id}/status")
            assert status.status_code == 200
            assert status.json() == {
                "status": "complete",
                "server_id": created_server_id,
            }

            servers_page = client.get("/servers")
            assert servers_page.status_code == 200
            assert "auto-enrolled-test" in servers_page.text

            replay = client.post(
                f"/api/enroll/{enrollment_id}/complete",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-NASitron-Enrollment-Signature": signature,
                },
            )
            assert replay.status_code == 409

            with SessionLocal() as db:
                server = db.get(Server, created_server_id)
                assert server is not None
                assert server.host == "127.0.0.254"
                assert server.auth_type == "key"
                assert server.sudo_for_smart is True
                assert decrypt(server.private_key_enc)
    finally:
        with SessionLocal() as db:
            if enrollment_id:
                enrollment = db.get(RemoteEnrollment, enrollment_id)
                if enrollment:
                    db.delete(enrollment)
            if created_server_id:
                server = db.get(Server, created_server_id)
                if server:
                    db.delete(server)
            db.commit()



def test_settings_page_uses_distinct_tabs_and_section_saves_are_isolated():
    with TestClient(app) as client:
        _login(client)

        with SessionLocal() as db:
            set_setting(db, "smtp_host", "smtp.keep.example")
            set_setting(db, "pool_capacity_warning", "81")
            set_setting(db, "pool_capacity_critical", "91")
            set_setting(db, "metric_retention_days", "90")
            db.commit()

        response = client.get("/settings?tab=history")
        assert response.status_code == 200
        assert 'role="tablist"' in response.text
        for label in ("Email", "Health", "History", "Enrollment", "HTTPS"):
            assert f">{label}<" in response.text
        assert 'data-settings-panel="history"' in response.text
        assert 'id="panel-history"' in response.text
        assert 'id="panel-smtp"' in response.text
        assert 'id="panel-tls"' in response.text

        save = client.post(
            "/settings",
            data={
                "csrf_token": csrf_token(),
                "section": "history",
                "metric_retention_days": "120",
                "snapshot_retention_days": "45",
                "full_snapshot_interval_minutes": "20",
            },
            follow_redirects=False,
        )
        assert save.status_code == 303
        assert save.headers["location"].startswith(
            "/settings?tab=history&message="
        )

        with SessionLocal() as db:
            assert get_setting(db, "metric_retention_days") == "120"
            assert get_setting(db, "snapshot_retention_days") == "45"
            assert get_setting(db, "full_snapshot_interval_minutes") == "20"
            assert get_setting(db, "smtp_host") == "smtp.keep.example"
            assert get_setting(db, "pool_capacity_warning") == "81"
            assert get_setting(db, "pool_capacity_critical") == "91"

            set_setting(db, "smtp_host", "")
            set_setting(db, "pool_capacity_warning", "80")
            set_setting(db, "pool_capacity_critical", "90")
            set_setting(db, "metric_retention_days", "90")
            set_setting(db, "snapshot_retention_days", "30")
            set_setting(db, "full_snapshot_interval_minutes", "15")
            db.commit()



def test_server_and_pool_pages_show_zfs_properties():
    payload = {
        "system": {
            "hostname": "property-nas",
            "memory": {"used_pct": 10, "used_bytes": 100, "total_bytes": 1000},
            "zfs_version": "zfs-2.2",
            "load1": 0,
            "uptime_seconds": 100,
            "services": "zfs.target=active",
        },
        "arc": {
            "hit_rate_pct": 99.0,
            "size_bytes": 1,
            "target_bytes": 1,
            "min_bytes": 1,
            "max_bytes": 1,
            "hits": 1,
            "misses": 0,
            "l2_size_bytes": 0,
            "l2_hit_rate_pct": 0,
        },
        "pools": [
            {
                "name": "tank",
                "size_bytes": 1000,
                "alloc_bytes": 400,
                "free_bytes": 600,
                "fragmentation_pct": 10,
                "capacity_pct": 40,
                "dedup_ratio": 1,
                "compression_ratio": 1.75,
                "health": "ONLINE",
                "io": {},
                "status": {"scan": "", "vdevs": []},
                "properties": [
                    {
                        "property": "autotrim",
                        "value": "on",
                        "source": "local",
                        "is_set": True,
                    },
                    {
                        "property": "comment",
                        "value": "-",
                        "source": "default",
                        "is_set": False,
                    },
                ],
            }
        ],
        "datasets": [
            {
                "name": "tank",
                "type": "filesystem",
                "used_bytes": 400,
                "available_bytes": 600,
                "referenced_bytes": 350,
                "logical_used_bytes": 700,
                "compression_ratio": 1.75,
                "mountpoint": "/tank",
                "properties": [
                    {
                        "property": "recordsize",
                        "value": "1M",
                        "source": "local",
                        "is_set": True,
                    }
                ],
            },
            {
                "name": "tank/data",
                "type": "filesystem",
                "used_bytes": 100,
                "available_bytes": 900,
                "referenced_bytes": 100,
                "logical_used_bytes": 100,
                "compression_ratio": 1.25,
                "mountpoint": "/tank/data",
                "properties": [
                    {
                        "property": "compression",
                        "value": "zstd",
                        "source": "local",
                        "is_set": True,
                    }
                ],
            }
        ],
        "drives": [],
        "collection": {"stale_subsystems": [], "errors": []},
    }

    with SessionLocal() as db:
        server = Server(
            name="zfs-property-test",
            host="127.0.0.3",
            username="nasitron",
            auth_type="password",
            enabled=False,
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        db.add(
            CurrentState(
                server_id=server.id,
                captured_at=datetime.utcnow(),
                payload_json=json.dumps(payload),
            )
        )
        db.commit()
        server_id = server.id

    try:
        with TestClient(app) as client:
            _login(client)

            detail = client.get(f"/servers/{server_id}")
            assert detail.status_code == 200
            assert "Pool properties" in detail.text
            assert "Compression Ratio" in detail.text
            assert "1.75x" in detail.text
            assert "1.25x" in detail.text
            assert "autotrim" in detail.text
            assert "compression" in detail.text
            assert "zstd" in detail.text

            pools_page = client.get("/pools")
            assert pools_page.status_code == 200
            assert "ZFS pool options" in pools_page.text
            assert "Compression Ratio" in pools_page.text
            assert "1.75x" in pools_page.text
            assert "autotrim" in pools_page.text

            tanks_page = client.get("/tanks")
            assert tanks_page.status_code == 200
            assert "<h1>Tanks</h1>" in tanks_page.text
            assert "tank" in tanks_page.text
            assert "zfs-property-test" in tanks_page.text
            assert "1.75x" in tanks_page.text
            assert "Logical used" in tanks_page.text
            assert "700 B" in tanks_page.text
            assert "/tank" in tanks_page.text
            assert "recordsize" in tanks_page.text
            assert "autotrim" in tanks_page.text

            dashboard = client.get("/")
            assert dashboard.status_code == 200
            assert "1.75x" in dashboard.text
    finally:
        with SessionLocal() as db:
            server = db.get(Server, server_id)
            if server:
                db.delete(server)
                db.commit()



def test_admin_can_manage_users_and_change_passwords():
    created_user_id = None
    username = "operator-test"
    old_password = "operator-password-old"
    new_password = "operator-password-new"

    try:
        with TestClient(app) as client:
            _login(client)

            users_page = client.get("/users")
            assert users_page.status_code == 200
            assert "ci-admin" in users_page.text
            assert "Add User" in users_page.text

            create = client.post(
                "/users/new",
                data={
                    "csrf_token": csrf_token(),
                    "username": username,
                    "password": old_password,
                    "password_confirm": old_password,
                    "enabled": "on",
                },
                follow_redirects=False,
            )
            assert create.status_code == 303

            with SessionLocal() as db:
                user = db.scalar(
                    __import__("sqlalchemy").select(WebUser).where(
                        WebUser.username == username
                    )
                )
                assert user is not None
                assert user.enabled is True
                assert user.is_admin is False
                created_user_id = user.id

            client.post(
                "/logout",
                data={"csrf_token": csrf_token()},
                follow_redirects=False,
            )

            login_user = client.post(
                "/login",
                data={
                    "username": username,
                    "password": old_password,
                    "csrf_token": csrf_token(),
                    "next": "/",
                },
                follow_redirects=False,
            )
            assert login_user.status_code == 303
            assert client.get("/").status_code == 200
            assert client.get("/users").status_code == 403

            client.post(
                "/logout",
                data={"csrf_token": csrf_token()},
                follow_redirects=False,
            )
            _login(client)

            update = client.post(
                f"/users/{created_user_id}/edit",
                data={
                    "csrf_token": csrf_token(),
                    "username": username,
                    "password": new_password,
                    "password_confirm": new_password,
                    "enabled": "on",
                },
                follow_redirects=False,
            )
            assert update.status_code == 303

            client.post(
                "/logout",
                data={"csrf_token": csrf_token()},
                follow_redirects=False,
            )

            old_login = client.post(
                "/login",
                data={
                    "username": username,
                    "password": old_password,
                    "csrf_token": csrf_token(),
                    "next": "/",
                },
                follow_redirects=False,
            )
            assert old_login.status_code == 401

            new_login = client.post(
                "/login",
                data={
                    "username": username,
                    "password": new_password,
                    "csrf_token": csrf_token(),
                    "next": "/",
                },
                follow_redirects=False,
            )
            assert new_login.status_code == 303
            assert client.get("/").status_code == 200
    finally:
        with SessionLocal() as db:
            if created_user_id:
                user = db.get(WebUser, created_user_id)
                if user:
                    db.delete(user)
                    db.commit()


def test_last_admin_and_self_lockout_are_blocked():
    with TestClient(app) as client:
        _login(client)

        with SessionLocal() as db:
            admin = db.scalar(
                __import__("sqlalchemy").select(WebUser).where(
                    WebUser.username == "ci-admin"
                )
            )
            assert admin is not None
            admin_id = admin.id

        disable_self = client.post(
            f"/users/{admin_id}/edit",
            data={
                "csrf_token": csrf_token(),
                "username": "ci-admin",
                "is_admin": "on",
            },
        )
        assert disable_self.status_code == 400
        assert "cannot disable your own account" in disable_self.text

        demote_self = client.post(
            f"/users/{admin_id}/edit",
            data={
                "csrf_token": csrf_token(),
                "username": "ci-admin",
                "enabled": "on",
            },
        )
        assert demote_self.status_code == 400
        assert "cannot remove your own administrator role" in demote_self.text

        delete_self = client.post(
            f"/users/{admin_id}/delete",
            data={"csrf_token": csrf_token()},
        )
        assert delete_self.status_code == 400
        assert "cannot delete your own account" in delete_self.text



def test_email_settings_support_mailjet_transport():
    with TestClient(app) as client:
        _login(client)

        page = client.get("/settings?tab=smtp")
        assert page.status_code == 200
        assert "Mailjet-compatible API" in page.text
        assert 'name="mailjet_api_url"' in page.text
        assert 'name="mailjet_api_key"' in page.text
        assert 'name="mailjet_secret_key"' in page.text

        save = client.post(
            "/settings",
            data={
                "csrf_token": csrf_token(),
                "section": "smtp",
                "smtp_enabled": "on",
                "email_transport": "mailjet",
                "mailjet_api_url": "https://mail.example.test/v3.1/send",
                "mailjet_api_key": "mailjet-public",
                "mailjet_secret_key": "mailjet-secret",
                "smtp_from": "nasitron@example.test",
                "smtp_to": "admin@example.test",
                "smtp_port": "587",
            },
            follow_redirects=False,
        )
        assert save.status_code == 303
        assert save.headers["location"].startswith("/settings?tab=smtp&message=")

        with SessionLocal() as db:
            assert get_setting(db, "email_transport") == "mailjet"
            assert get_setting(db, "mailjet_api_url") == "https://mail.example.test/v3.1/send"
            assert get_setting(db, "mailjet_api_key") == "mailjet-public"
            assert get_setting(db, "mailjet_secret_key") == "mailjet-secret"

            set_setting(db, "smtp_enabled", "false")
            set_setting(db, "email_transport", "smtp")
            set_setting(db, "mailjet_api_url", "https://api.mailjet.com/v3.1/send")
            set_setting(db, "smtp_from", "")
            set_setting(db, "smtp_to", "")
            db.commit()
