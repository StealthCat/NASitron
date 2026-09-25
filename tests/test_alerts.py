from app.alerts import evaluate_snapshot
from app.db import SessionLocal, init_db
from app.models import Alert, Server


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
