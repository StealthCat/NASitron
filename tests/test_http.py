import base64
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Metric, Server


def _auth_header() -> dict[str, str]:
    token = base64.b64encode(b"ci-admin:ci-password-strong").decode("ascii")
    return {"Authorization": f"Basic {token}"}


def test_healthz_is_public_but_dashboard_requires_authentication():
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 401
        assert client.get("/", headers=_auth_header()).status_code == 200


def test_mutating_route_rejects_bad_csrf():
    with TestClient(app) as client:
        response = client.post(
            "/settings",
            headers=_auth_header(),
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
        response = client.get(
            f"/api/servers/{server_id}/metrics?name=system.load1&hours=24",
            headers=_auth_header(),
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
