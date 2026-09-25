import json
from pathlib import Path

import paramiko
import pytest
from fastapi import HTTPException

from app.alerts import evaluate_snapshot
from app.collector import CollectorError, _load_host_keys, _save_host_keys_atomic
from app.db import SessionLocal, init_db
from app.instance_lock import InstanceLock
from app.models import Alert, Server
from app.service import _merge_previous_subsystems, _update_expected_pools
from app.validation import bounded_text
from remote.nasitron_root_helper import _find_guid_size


def test_scalar_text_rejects_control_characters():
    with pytest.raises(HTTPException):
        bounded_text("bad\nname", "Name")
    with pytest.raises(HTTPException):
        bounded_text("bad\tname", "Name")


def test_corrupt_known_hosts_fails_closed(tmp_path: Path):
    path = tmp_path / "known_hosts"
    path.write_text("not a valid known hosts record\n", encoding="utf-8")
    with pytest.raises(CollectorError):
        _load_host_keys(path)


def test_known_hosts_atomic_save_round_trip(tmp_path: Path):
    path = tmp_path / "known_hosts"
    keys = paramiko.HostKeys()
    key = paramiko.RSAKey.generate(1024)
    keys.add("nas.example", key.get_name(), key)
    _save_host_keys_atomic(keys, path)
    loaded = _load_host_keys(path)
    assert "nas.example" in loaded


def test_instance_lock_blocks_second_process_style_lock(tmp_path: Path):
    first = InstanceLock()
    second = InstanceLock()
    first.path = tmp_path / "instance.lock"
    second.path = first.path
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()


def test_expected_pool_inventory_never_auto_removes():
    server = Server(
        name="expected-pool-test",
        host="127.0.0.1",
        username="nasitron",
        auth_type="password",
        expected_pools_json='["tank","backup"]',
        enabled=False,
    )
    snapshot = {"pools": [{"name": "tank"}], "collection": {}}
    _update_expected_pools(server, snapshot)
    assert json.loads(server.expected_pools_json) == ["backup", "tank"]
    assert snapshot["collection"]["missing_pools"] == ["backup"]


def test_partial_arc_and_topology_keep_last_known_good():
    previous = {
        "system": {"hostname": "nas", "memory": {"used_pct": 25}},
        "arc": {"hit_rate_pct": 99.0},
        "datasets": [{"name": "tank/data"}],
        "pools": [
            {
                "name": "tank",
                "io": {"read_bps": 123},
                "status": {"state": "ONLINE", "vdevs": [{"name": "/dev/sda"}]},
            }
        ],
        "drives": [],
        "collection": {
            "freshness": {
                "zfs.arc": "2026-09-25T10:00:00Z",
                "pool.status:tank": "2026-09-25T10:00:00Z",
            }
        },
    }
    current = {
        "system": {"hostname": "nas", "memory": {"used_pct": 30}},
        "arc": {"hit_rate_pct": 0.0},
        "datasets": [],
        "pools": [{"name": "tank", "io": {}, "status": {"state": "", "vdevs": []}}],
        "drives": [],
        "collection": {
            "smart_sampled": False,
            "freshness": {},
            "stale_subsystems": [],
            "errors": [
                {"subsystem": "zfs.arc", "message": "failed"},
                {"subsystem": "pool.status:tank", "message": "failed"},
            ],
        },
    }
    _merge_previous_subsystems(current, previous)
    assert current["arc"]["hit_rate_pct"] == 99.0
    assert current["pools"][0]["status"]["state"] == "ONLINE"
    assert "zfs.arc" in current["collection"]["stale_subsystems"]
    assert "pool.status:tank" in current["collection"]["stale_subsystems"]


def test_missing_pool_and_bad_vdev_generate_alerts():
    init_db()
    with SessionLocal() as db:
        server = Server(
            name="alert-hardening-test",
            host="127.0.0.1",
            username="nasitron",
            auth_type="password",
            enabled=False,
        )
        db.add(server)
        db.commit()
        db.refresh(server)

        snapshot = {
            "pools": [
                {
                    "name": "tank",
                    "health": "ONLINE",
                    "capacity_pct": 10,
                    "status": {
                        "scrub_finished_at": None,
                        "vdevs": [
                            {
                                "name": "/dev/sda",
                                "guid": "123",
                                "state": "FAULTED",
                                "role": "data",
                                "leaf": True,
                                "read_errors": 0,
                                "write_errors": 0,
                                "checksum_errors": 0,
                            }
                        ],
                    },
                }
            ],
            "drives": [],
            "collection": {
                "errors": [],
                "pool_status_ok": ["tank"],
                "missing_pools": ["backup"],
            },
        }
        evaluate_snapshot(db, server, snapshot)
        db.commit()
        keys = {
            alert.key
            for alert in db.query(Alert).filter(Alert.server_id == server.id).all()
            if alert.active
        }
        assert "pool.missing:backup" in keys
        assert "vdev.state:tank:123" in keys
        db.delete(server)
        db.commit()


def test_smart_findings_and_nvme_media_errors_alert():
    init_db()
    with SessionLocal() as db:
        server = Server(
            name="smart-hardening-test",
            host="127.0.0.1",
            username="nasitron",
            auth_type="password",
            enabled=False,
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        snapshot = {
            "pools": [],
            "drives": [
                {
                    "path": "/dev/nvme0n1",
                    "serial": "NVME1",
                    "model": "NVMe",
                    "smart": {
                        "data_available": True,
                        "stale": False,
                        "smart_passed": True,
                        "exit_findings": ["self_test_errors"],
                        "media_errors": 2,
                        "percentage_used": 96,
                    },
                }
            ],
            "collection": {"errors": [], "pool_status_ok": [], "missing_pools": []},
        }
        evaluate_snapshot(db, server, snapshot)
        db.commit()
        keys = {
            alert.key
            for alert in db.query(Alert).filter(Alert.server_id == server.id).all()
            if alert.active
        }
        assert "drive.smart_findings:NVME1" in keys
        assert "drive.media_errors:NVME1" in keys
        assert "drive.nvme_endurance:NVME1" in keys
        db.delete(server)
        db.commit()


def test_root_helper_guid_size_search():
    payload = {
        "pools": {
            "tank": {
                "vdevs": {
                    "disk": {
                        "guid": 12345,
                        "rep_dev_size": 999999,
                    }
                }
            }
        }
    }
    assert _find_guid_size(payload, "12345") == 999999
