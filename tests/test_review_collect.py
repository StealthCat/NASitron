"""Regression tests for collector/parser/alert fixes found in code review."""

import json
from types import SimpleNamespace

import pytest

import app.collector as collector_module
from app.alerts import evaluate_snapshot
from app.collector import CollectorError, SSHCollector
from app.db import SessionLocal, init_db
from app.devices import is_same_or_partition
from app.models import Alert, CurrentState, Server, Snapshot
from app.parser import (
    build_snapshot,
    parse_property_rows,
    parse_pool_status,
    parse_pool_status_json,
    parse_smart,
)
from app.service import _merge_previous_subsystems
from app.topology import _disk_for


@pytest.mark.parametrize(
    "name,disk,expected",
    [
        ("/dev/sda", "/dev/sda", True),
        ("/dev/sda1", "/dev/sda", True),
        ("/dev/sdap", "/dev/sda", False),
        ("/dev/sdap1", "/dev/sda", False),
        ("/dev/nvme0n1p2", "/dev/nvme0n1", True),
        ("/dev/nvme0n10", "/dev/nvme0n1", False),
        ("/dev/nvme0n10p1", "/dev/nvme0n1", False),
        ("/dev/disk/by-id/ata-X-part1", "/dev/disk/by-id/ata-X", True),
        ("/dev/sda", "", False),
    ],
)
def test_partition_matching_does_not_confuse_sibling_devices(name, disk, expected):
    assert is_same_or_partition(name, disk) is expected


def test_topology_maps_nvme_namespace_ten_to_its_own_disk():
    drives = [{"path": "/dev/nvme0n1"}, {"path": "/dev/nvme0n10"}]
    assert _disk_for("/dev/nvme0n10", drives) is drives[1]
    assert _disk_for("/dev/nvme0n1p1", drives) is drives[0]
    sdx = [{"path": "/dev/sda"}, {"path": "/dev/sdap"}]
    assert _disk_for("/dev/sdap1", sdx) is sdx[1]


def test_build_snapshot_membership_does_not_bleed_into_sibling_disk():
    lsblk = {"blockdevices": [
        {"name": "sda", "path": "/dev/sda", "type": "disk", "serial": "A"},
        {"name": "sdap", "path": "/dev/sdap", "type": "disk", "serial": "B"},
    ]}
    raw = {
        "zpool_list": {"exit": 0, "stdout": "tank\t10\t1\t9\t0\t10\t1.00\tONLINE\n"},
        "zpool_status": {"tank": {"exit": 0, "stdout": STATUS_WITH_SPARES}},
        "lsblk": {"exit": 0, "stdout": json.dumps(lsblk)},
    }
    drives = {d["serial"]: d for d in build_snapshot(raw)["drives"]}
    assert drives["A"]["zfs_memberships"] == []
    assert {m["vdev"] for m in drives["B"]["zfs_memberships"]} == {"/dev/sdap"}


STATUS_WITH_SPARES = """
  pool: tank
 state: DEGRADED
  scan: resilvered 1.00T in 02:00:00 with 0 errors on Sun Sep 20 01:02:03 2026
config:

        NAME            STATE     READ WRITE CKSUM
        tank            DEGRADED     0     0     0
          mirror-0      DEGRADED     0     0     0
            spare-0     DEGRADED     0     0     0
              /dev/sdb  FAULTED   1.2K     0  3.5M  too many errors
              /dev/sdap ONLINE       0     0     0
            /dev/sdc    ONLINE       0     0     0
        spares
          /dev/sdap     INUSE     currently in use
          /dev/sde      AVAIL

errors: No known data errors
"""


def test_text_status_keeps_inuse_spares_and_human_error_counts():
    status = parse_pool_status(STATUS_WITH_SPARES)
    spares = [v for v in status["vdevs"] if v["role"] == "spare"]
    assert [(v["name"], v["state"]) for v in spares] == [
        ("/dev/sdap", "INUSE"),
        ("/dev/sde", "AVAIL"),
    ]
    assert spares[0]["detail"] == "currently in use"
    assert spares[0]["read_errors"] is None
    faulted = next(v for v in status["vdevs"] if v["name"] == "/dev/sdb")
    assert faulted["detail"] == "too many errors"
    assert faulted["read_errors"] == 1228
    assert faulted["checksum_errors"] == int(3.5 * 1024 * 1024)


@pytest.mark.parametrize(
    "scan,expected",
    [
        ("scrub repaired 0B in 00:10:00 with 0 errors on Sun Sep 20 01:02:03 2026", "2026-09-20T01:02:03"),
        ("scrub repaired 0B in 00:10:00 with 0 errors on Sun Sep  6 01:02:03 2026", "2026-09-06T01:02:03"),
        ("resilvered 1.00T in 02:00:00 with 0 errors on Sun Sep 20 01:02:03 2026", None),
        ("scrub canceled on Sun Sep 20 01:02:03 2026", None),
        ("resilver canceled on Sun Sep 20 01:02:03 2026", None),
        ("scrub in progress since Sun Sep 20 01:02:03 2026", None),
    ],
)
def test_only_completed_scrubs_count_as_last_scrub(scan, expected):
    status = parse_pool_status(f"  pool: tank\n state: ONLINE\n  scan: {scan}\n")
    assert status["scrub_finished_at"] == expected


def test_json_status_reads_l2cache_section():
    payload = {
        "pools": {
            "tank": {
                "state": "ONLINE",
                "vdevs": {"tank": {"name": "tank", "state": "ONLINE", "vdevs": {
                    "sda": {"name": "sda", "path": "/dev/sda", "state": "ONLINE"}}}},
                "l2cache": {"nvme0n1": {"name": "nvme0n1", "path": "/dev/nvme0n1",
                                        "state": "FAULTED"}},
            }
        }
    }
    status = parse_pool_status_json(json.dumps(payload), "tank")
    cache = [v for v in status["vdevs"] if v["role"] == "cache"]
    assert [(v["name"], v["state"]) for v in cache] == [("/dev/nvme0n1", "FAULTED")]


def test_smartctl_open_failure_json_is_not_reported_as_data():
    text = json.dumps({"smartctl": {"exit_status": 2, "messages": [
        {"string": "Smartctl open device: /dev/sdz failed: No such device",
         "severity": "error"}]}})
    assert parse_smart(text, 2)["data_available"] is False
    # Bit 2 (a SMART command failed) with real data stays available.
    ok = json.dumps({"smartctl": {"exit_status": 4}, "smart_status": {"passed": True}})
    assert parse_smart(ok, 4)["data_available"] is True
    # Non-object JSON must not crash snapshot building.
    assert parse_smart("[]", 0)["data_available"] is False


def test_sampled_zvols_do_not_make_collection_partial():
    nodes = [{"path": "/dev/zd0", "type": "disk"}]
    snapshot = build_snapshot({
        "lsblk": {"exit": 0, "stdout": json.dumps({"blockdevices": nodes})},
        "smart_sampled": True,
        "smart_inventory_ok": True,
    })
    assert not [e for e in snapshot["collection"]["errors"]
                if e["subsystem"].startswith("smart")]


class _FakeServer(SimpleNamespace):
    pass


def _fake_collector(monkeypatch, disks, smart_seconds):
    server = _FakeServer(zpool_status_json_supported=False, sudo_for_smart=False)
    collector = SSHCollector(server)
    collector.client = object()
    clock = {"now": 1000.0}
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: clock["now"])
    lsblk = json.dumps({"blockdevices": [
        {"path": path, "type": "disk"} for path in disks]})
    calls = []

    def run(command, timeout=30, **_kwargs):
        if command.startswith("smartctl"):
            calls.append(command.split()[-2])
            clock["now"] += smart_seconds
            return {"stdout": "{}", "stderr": "", "exit": 0}
        stdout = lsblk if command.startswith("lsblk") else ""
        return {"stdout": stdout, "stderr": "", "exit": 0}

    collector.run = run
    collector.collection_deadline = clock["now"] + 100
    return collector, calls


def test_slow_smart_defers_drives_instead_of_failing_collection(monkeypatch):
    disks = ["/dev/sda", "/dev/sdb", "/dev/sdc"]
    collector, calls = _fake_collector(monkeypatch, disks, smart_seconds=50)
    collector.previous = {"drives": [
        {"path": "/dev/sda", "smart": {"sampled_at": "2026-01-02T00:00:00Z"}},
        {"path": "/dev/sdb", "smart": {"sampled_at": "2026-01-01T00:00:00Z"}},
    ]}
    raw = collector.collect(include_smart=True)
    # Never-sampled sdc first, then the least recently sampled sdb.
    assert calls == ["/dev/sdc", "/dev/sdb"]
    assert raw["smart_deferred"] == ["/dev/sda"]

    raw["lsblk"]["stdout"] = json.dumps({"blockdevices": [
        {"path": path, "type": "disk", "serial": path[-3:]} for path in disks]})
    snapshot = build_snapshot(raw)
    by_path = {d["path"]: d for d in snapshot["drives"]}
    assert by_path["/dev/sda"]["smart"] == {}
    subsystems = {e["subsystem"] for e in snapshot["collection"]["errors"]}
    assert "smart.deferred" in subsystems
    assert "smart:sda" not in subsystems

    previous = {"drives": [{"path": "/dev/sda", "serial": "sda", "smart": {
        "data_available": True, "temperature_c": 30, "sampled_at": "old"}}]}
    _merge_previous_subsystems(snapshot, previous)
    carried = {d["path"]: d for d in snapshot["drives"]}["/dev/sda"]["smart"]
    assert carried["stale"] is True and carried["data_available"] is True


def test_fetch_host_key_connection_failure_is_collector_error():
    server = _FakeServer(host="127.0.0.1", port=1)
    with pytest.raises(CollectorError):
        SSHCollector.fetch_host_key(server)


def test_unsampled_new_drive_does_not_raise_smart_unavailable_alert():
    init_db()
    with SessionLocal() as db:
        server = Server(name="review-unsampled-drive", host="127.0.0.1",
                        username="n", auth_type="password", enabled=False)
        db.add(server)
        db.commit()
        snapshot = {
            "pools": [],
            "drives": [{"path": "/dev/sdx", "serial": "NEW", "smart": {}}],
            "collection": {"errors": [], "pool_status_ok": [], "missing_pools": []},
        }
        evaluate_snapshot(db, server, snapshot)
        db.commit()
        keys = {a.key for a in db.query(Alert).filter(Alert.server_id == server.id)}
        assert "drive.smart_unavailable:NEW" not in keys
        db.delete(server)
        db.commit()


def test_history_snapshot_omits_raw_detail_cache(monkeypatch):
    import app.service as service

    init_db()
    raw = {
        "zpool_list": {"exit": 0, "stdout": ""},
        "detail_cache": {"captured_at": 1.0, "commands": {"zfs_get": {"stdout": "x" * 64}}},
    }

    class FakeCollector:
        def __init__(self, server):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def collect(self, include_smart=False):
            return dict(raw)

    monkeypatch.setattr(service, "SSHCollector", FakeCollector)
    with SessionLocal() as db:
        server = Server(name="review-history-cache", host="127.0.0.1",
                        username="n", auth_type="password", enabled=False)
        db.add(server)
        db.commit()
        service.collect_server(db, server.id)
        row = db.query(Snapshot).filter(Snapshot.server_id == server.id).one()
        assert "_detail_cache" not in json.loads(row.payload_json)
        current = db.get(CurrentState, server.id)
        assert "_detail_cache" in json.loads(current.payload_json)
        db.delete(server)
        db.commit()


def test_property_values_containing_tabs_keep_their_source():
    rows = parse_property_rows("tank\tcom.example:note\ta\tb\tlocal\n")["tank"]
    assert rows[0]["value"] == "a\tb"
    assert rows[0]["source"] == "local" and rows[0]["is_set"] is True
