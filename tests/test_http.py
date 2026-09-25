import base64

from fastapi.testclient import TestClient

from app.main import app


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
