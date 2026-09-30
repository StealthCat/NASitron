"""Synthetic UI fixture. Never polls a real host. For CI/manual preview only."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.db import init_db, SessionLocal
from app.parser import parse_pool_status
from app.pool_capacity import parse_capacity
from app.models import Server, CurrentState, Metric, Alert, DriveLabel, MonitorEvent
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

topology_lines = ["  pool: tank", " state: ONLINE", "  scan: scrub repaired 0B in 01:25:00 with 0 errors on Sun Sep 27 10:00:00 2026", "config:", "  tank ONLINE 0 0 0"]
for group in range(3):
    topology_lines.append(f"    raidz2-{group} ONLINE 0 0 0")
    for member in range(6):
        topology_lines.append(f"      /dev/sd{chr(97 + group * 6 + member)} ONLINE 0 0 0")
topology_lines[-1] = "      scsi-SATA_WDC_WD60EFAX-68S_WD-WX31D49KSZTT ONLINE 0 0 0"
topology_lines.append("errors: No known data errors")
payload["pools"][0]["status"] = parse_pool_status("\n".join(topology_lines))
capacity_lines = ["tank\t131941395333120\t93458488360960\t38482906972160\t-\t-\t12\t70.8\t1.00x\tONLINE\t-"]
for group in range(3):
    capacity_lines.append(f"\traidz2-{group}\t43980465111040\t31152829453653\t12827635657387\t0\t0\t12\t70.8\t-\tONLINE\t-")
    for member in range(6):
        capacity_lines.append(f"\t/dev/sd{chr(97 + group * 6 + member)}\t-\t-\t-\t-\t0\t-\t-\t-\tONLINE\t-")
capacity_lines.extend(["cache                   -      -      -      -      -      -      -      -      -", "\t/dev/cache-test\t1000\t400\t600\t-\t-\t0\t40\t-\tONLINE"])
payload["pools"][0]["capacity_detail"] = {"fresh": True, "captured_at": now.isoformat()+"Z", "rows": parse_capacity("\n".join(capacity_lines), "tank")}
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
    for i in range(25):
        db.add(DriveLabel(server_id=sid, identity=f"DEMO-{i:03}", label=f"Enclosure A · Bay {i+1}"))
    for day in range(1,11):
        db.add(Metric(server_id=sid,name="pool.capacity_pct",scope="tank",captured_at=now-timedelta(days=day),value=71-day*1.2))
    db.add(MonitorEvent(server_id=sid,kind="collection",message="Collection recovered",captured_at=now-timedelta(minutes=20)))
    db.add(MonitorEvent(server_id=sid,kind="scan",message="tank: scrub completed with 0 errors",captured_at=now-timedelta(hours=2)))
    db.get(Server,sid).last_collection_seconds=2.4
    db.get(Server,sid).last_smart_at=now
    other = Server(name="Boreas Demo", host="192.0.2.11", username="nasitron", enabled=True,
                   last_collection_state="ok", last_ok_at=now, last_poll_at=now)
    db.add(other)
    db.flush()
    damaged = json.loads(json.dumps(payload))
    damaged["drives"] = []
    damaged["datasets"] = []
    damaged["snapshot_inventory"] = {"fresh": True, "rows": []}
    damaged["pools"] = [dict(payload["pools"][0], health="DEGRADED", status=parse_pool_status("""  pool: tank
 state: DEGRADED
status: One or more devices has experienced an error.
        The pool can still be used.
action: Replace the affected device after checking its connections.
   see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-9P
  scan: resilver in progress since Sun Sep 27 10:00:00 2026
        120G scanned, 60G issued, 50.00% done, 00:12:00 to go
config:
        NAME STATE READ WRITE CKSUM
        tank DEGRADED 0 0 3
          mirror-0 DEGRADED 0 0 3
            /dev/sda ONLINE 0 0 0 (resilvering)
            /dev/sdb FAULTED 0 0 3 too many errors
errors: Permanent errors have been detected in the following files:
        /tank/photos/family photo.jpg
        tank/data:<0xdeadbeef>
"""))]
    damaged["pools"][0].pop("capacity_detail", None)
    db.add(CurrentState(server_id=other.id, payload_json=json.dumps(damaged)))
    db.commit()

if __name__ == "__main__":
    import app.main as main
    import uvicorn

    main.start_scheduler = lambda: None
    main.stop_scheduler = lambda: None
    uvicorn.run(main.app, host="127.0.0.1", port=8765, log_level="warning")
