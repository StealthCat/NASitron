import base64
import json

from app.alerts import evaluate_snapshot, send_email
from app.db import SessionLocal, init_db
from app.models import Alert, Server
from app.settings_store import set_setting


def test_partial_collection_does_not_resolve_unrefreshed_vdev_alert():
    init_db()
    with SessionLocal() as db:
        server = Server(
            name="partial-alert-test",
            host="127.0.0.1",
            username="nasitron",
            auth_type="password",
            enabled=False,
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        existing = Alert(
            server_id=server.id,
            key="vdev.errors:tank:/dev/sda",
            severity="warning",
            title="Existing error",
            message="Existing error",
            active=True,
        )
        db.add(existing)
        db.commit()

        snapshot = {
            "pools": [
                {
                    "name": "tank",
                    "health": "ONLINE",
                    "capacity_pct": 10,
                    "status": {"vdevs": [], "scrub_finished_at": None},
                }
            ],
            "drives": [],
            "collection": {
                "errors": [{"subsystem": "pool.status:tank", "message": "failed"}],
                "pool_status_ok": [],
            },
        }
        evaluate_snapshot(db, server, snapshot)
        db.commit()
        db.refresh(existing)
        assert existing.active is True

        snapshot["collection"] = {"errors": [], "pool_status_ok": ["tank"]}
        evaluate_snapshot(db, server, snapshot)
        db.commit()
        db.refresh(existing)
        assert existing.active is False

        db.delete(server)
        db.commit()



def test_mailjet_transport_uses_basic_auth_and_v31_payload(monkeypatch):
    init_db()
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit=None):
            return json.dumps(
                {
                    "Messages": [
                        {"Status": "success"},
                        {"Status": "success"},
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("app.alerts.urllib.request.urlopen", fake_urlopen)

    with SessionLocal() as db:
        set_setting(db, "email_transport", "mailjet")
        set_setting(db, "mailjet_api_url", "https://mail.example.test/v3.1/send")
        set_setting(db, "mailjet_api_key", "public-key", secret=True)
        set_setting(db, "mailjet_secret_key", "secret-key", secret=True)
        set_setting(db, "smtp_from", "nasitron@example.test")
        set_setting(db, "smtp_to", "one@example.test,two@example.test")
        db.commit()

        assert send_email(db, "Test subject", "Test body", force=True) is True

        request = captured["request"]
        assert request.full_url == "https://mail.example.test/v3.1/send"
        assert captured["timeout"] == 15
        assert request.get_header("Authorization") == (
            "Basic "
            + base64.b64encode(b"public-key:secret-key").decode("ascii")
        )

        payload = json.loads(request.data)
        assert len(payload["Messages"]) == 2
        assert payload["Messages"][0]["From"]["Email"] == "nasitron@example.test"
        assert payload["Messages"][0]["To"] == [{"Email": "one@example.test"}]
        assert payload["Messages"][1]["To"] == [{"Email": "two@example.test"}]
        assert payload["Messages"][0]["Subject"] == "Test subject"
        assert payload["Messages"][0]["TextPart"] == "Test body"

        set_setting(db, "email_transport", "smtp")
        set_setting(db, "mailjet_api_url", "https://api.mailjet.com/v3.1/send")
        set_setting(db, "smtp_from", "")
        set_setting(db, "smtp_to", "")
        db.commit()
