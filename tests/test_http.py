from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import CurrentState, Metric, Server
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
        assert "Install NASitron access on this server" in response.text
        assert "/install-remote.sh" in response.text
        assert "curl --cacert /tmp/nasitron-caddy-root.crt" in response.text
        assert "docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt" in response.text
        assert "Do not use" in response.text
        assert "curl -k" in response.text

        with SessionLocal() as db:
            set_setting(db, "tls_mode", "acme")
            set_setting(db, "tls_domain", "nas.example.com")
            db.commit()

        response = client.get("/servers/new")
        assert response.status_code == 200
        assert "Trusted HTTPS" in response.text
        assert "curl -fsSL" in response.text
        assert "/install-remote.sh" in response.text
        assert "--cacert /tmp/nasitron-caddy-root.crt" not in response.text

        with SessionLocal() as db:
            set_setting(db, "tls_mode", "internal")
            set_setting(db, "tls_domain", "localhost")
            db.commit()
