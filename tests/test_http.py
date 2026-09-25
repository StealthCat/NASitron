from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re

from fastapi.testclient import TestClient
from app.crypto import decrypt
from app.db import SessionLocal
from app.main import app
from app.models import CurrentState, Metric, RemoteEnrollment, Server
from app.security import csrf_token
from app.settings_store import set_setting


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
        assert "/install-remote.sh" in response.text
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
