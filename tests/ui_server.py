"""Synthetic UI fixture. Never polls a real host. For CI/manual preview only."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.db import init_db, SessionLocal
from app.parser import parse_pool_status
from app.models import Server, CurrentState, Metric, Alert
from app.security import ensure_bootstrap_admin
from app.settings_store import ensure_defaults

init_db()
payload = json.loads((Path(__file__).parent / "fixtures/preview.json").read_text())
now = datetime.utcnow()
payload["collection"]["captured_at"] = now.isoformat() + "Z"
payload["snapshot_inventory"] = {
    "fresh": True,
    "captured_at": now.isoformat() + "Z",
    "rows": [
        {
            "name": "tank/data@daily",
            "created": now.isoformat() + "Z",
            "used": 1024**3,
            "referenced": 512 * 1024**3,
        }
    ],
}
payload["drives"] = [
    {
        "path": f"/dev/sd{chr(97 + i)}",
        "serial": f"DEMO-{i:03d}",
        "model": "Example NAS HDD",
        "size_bytes": 12 * 1024**4,
        "rotational": True,
        "transport": "sata",
        "zfs_memberships": [{"pool": "tank", "role": "data"}],
        "smart": {
            "smart_passed": None if i == 0 else True,
            "temperature_c": 30 + i % 10,
            "power_on_hours": 12000,
            "sampled_at": now.isoformat() + "Z",
        },
    }
    for i in range(25)
]
payload["pools"][0].update(
    size_bytes=120 * 1024**4,
    alloc_bytes=85 * 1024**4,
    free_bytes=35 * 1024**4,
    capacity_pct=70.8,
)

topology_lines = ["config:", "  tank ONLINE 0 0 0"]
for group in range(3):
    topology_lines.append(f"    raidz2-{group} ONLINE 0 0 0")
    for member in range(6):
        topology_lines.append(f"      /dev/sd{chr(97 + group * 6 + member)} ONLINE 0 0 0")
topology_lines[-1] = "      scsi-SATA_WDC_WD60EFAX-68S_WD-WX31D49KSZTT ONLINE 0 0 0"
payload["pools"][0]["status"] = parse_pool_status("\n".join(topology_lines))
with SessionLocal() as db:
    ensure_defaults(db)
    ensure_bootstrap_admin(db)
    server = Server(
        name="Athena Demo",
        host="192.0.2.10",
        username="nasitron",
        enabled=True,
        last_collection_state="ok",
        last_ok_at=now,
        last_poll_at=now,
    )
    db.add(server)
    db.flush()
    sid = server.id
    db.add(CurrentState(server_id=sid, payload_json=json.dumps(payload)))
    db.add(
        Alert(
            server_id=sid,
            key="drive.smart:DEMO-000",
            title="SMART status unknown",
            message="No valid SMART result for /dev/sda.",
            severity="warning",
        )
    )
    for i in range(96):
        for name, value, scope in [
            ("arc.hit_rate_pct", 93 + i % 6, ""),
            ("pool.capacity_pct", 70 + i / 100, "tank"),
            ("pool.read_bps", 10000000 + i * 100000, "tank"),
            ("pool.write_bps", 3000000 + i * 10000, "tank"),
            ("system.memory_used_pct", 45 + i % 5, ""),
            ("drive.temperature_c", 34 + i % 3, "DEMO-001"),
        ]:
            db.add(
                Metric(
                    server_id=sid,
                    name=name,
                    value=value,
                    scope=scope,
                    captured_at=now - timedelta(minutes=(96 - i) * 15),
                )
            )
    for minutes in range(120):
        for scope in ["DEMO-000", "DEMO-001"]:
            for key, value in [("read_bps", 8_000_000 + minutes * 10000),
                               ("write_bps", 2_000_000), ("read_iops", 120),
                               ("write_iops", 35), ("read_latency_ms", 4),
                               ("write_latency_ms", 6), ("busy_pct", 45), ("queue_depth", 0.8)]:
                db.add(Metric(server_id=sid, name="drive.io." + key, scope=scope,
                              value=value, captured_at=now - timedelta(minutes=minutes)))
    db.commit()

if __name__ == "__main__":
    import app.main as main
    import uvicorn

    main.start_scheduler = lambda: None
    main.stop_scheduler = lambda: None
    uvicorn.run(main.app, host="127.0.0.1", port=8765, log_level="warning")
