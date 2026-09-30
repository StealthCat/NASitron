from datetime import datetime, timedelta
import json

from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Server, CurrentState, Metric, DriveLabel, MonitorEvent
from app.security import csrf_token


def test_monitoring_pages_batch_history_and_snapshot_paging():
    with TestClient(app) as client:
        client.post(
            "/login",
            data={
                "username": "ci-admin",
                "password": "ci-password-strong",
                "csrf_token": csrf_token(),
                "next": "/",
            },
        )
        with SessionLocal() as db:
            now = datetime.utcnow()
            s = Server(
                name="observability-test",
                host="localhost",
                username="test",
                enabled=False,
            )
            db.add(s)
            db.flush()
            sid = s.id
            db.add(
                CurrentState(
                    server_id=sid,
                    payload_json=json.dumps(
                        {
                            "drives": [
                                {
                                    "path": "/dev/sda",
                                    "serial": "bay-test",
                                    "size_bytes": 100,
                                    "smart": {"smart_passed": False, "stale": True},
                                }
                            ],
                            "snapshot_inventory": {
                                "rows": [
                                    {
                                        "name": f"tank@review-{i:03}",
                                        "created": now.isoformat(),
                                        "used": i,
                                        "referenced": 100,
                                    }
                                    for i in range(60)
                                ]
                            },
                        }
                    ),
                )
            )
            db.add(
                DriveLabel(
                    server_id=sid, identity="bay-test", label="Enclosure A · Bay 1"
                )
            )
            db.add(
                MonitorEvent(
                    server_id=sid,
                    kind="collection",
                    severity="critical",
                    message="Synthetic collection failure",
                )
            )
            db.add(
                Metric(
                    server_id=sid,
                    name="drive.io.read_bps",
                    scope="bay-test",
                    captured_at=now - timedelta(minutes=1),
                    value=7,
                )
            )
            db.commit()
        for url in [
            "/diagnostics",
            "/drive-bays?server=",
            "/timeline?server=",
            "/settings/database",
            "/settings/database?analyze=1",
            "/forecasts",
        ]:
            response = client.get(url)
            assert response.status_code == 200, (url, response.text)
        bays = client.get(f"/drive-bays?server={sid}").text
        assert "Enclosure A · Bay 1" in bays and "Failed" in bays
        assert (
            "Synthetic collection failure" in client.get(f"/timeline?server={sid}").text
        )
        page = client.get("/snapshots?q=review-&size=25&page=2&sort=used&direction=asc")
        assert page.status_code == 200
        assert "tank@review-025" in page.text and "tank@review-049" in page.text
        assert "tank@review-024" not in page.text and "tank@review-050" not in page.text
        params = [
            ("names", "drive.io.read_bps"),
            ("names", "drive.io.write_bps"),
            ("scopes", "bay-test"),
            ("scopes", "absent"),
        ]
        response = client.get(f"/api/servers/{sid}/metrics/batch", params=params)
        assert response.status_code == 200
        data = response.json()["series"]
        assert len(data) == 4 and data[0]["points"][0]["v"] == 7
        assert data[1]["points"] == []
        bad = client.get(
            f"/api/servers/{sid}/metrics/batch",
            params=params + [("scopes", str(i)) for i in range(4)],
        )
        assert bad.status_code == 400
        client.post("/logout", data={"csrf_token": csrf_token()})
        assert (
            client.get(f"/api/servers/{sid}/metrics/batch", params=params).status_code
            == 401
        )
        with SessionLocal() as db:
            db.delete(db.get(Server, sid))
            db.commit()
