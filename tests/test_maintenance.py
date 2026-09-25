import pytest

from app.maintenance import (
    ReplacementRequest,
    _failed_leaf_vdevs,
    _stable_id_map,
    validate_replacement_choice,
)


def test_failed_leaf_vdevs_excludes_parent_vdevs():
    text = """
  pool: tank
 state: DEGRADED
config:

        NAME                     STATE     READ WRITE CKSUM
        tank                     DEGRADED     0     0     0
          mirror-0               DEGRADED     0     0     0
            /dev/sda             FAULTED      1     0     2
            /dev/sdb             ONLINE       0     0     0
        logs
          /dev/nvme0n1p1         ONLINE       0     0     0

errors: No known data errors
"""
    failed = _failed_leaf_vdevs("tank", text)
    assert len(failed) == 1
    assert failed[0]["device"] == "/dev/sda"
    assert failed[0]["state"] == "FAULTED"
    assert failed[0]["pool"] == "tank"


def test_failed_numeric_guid_is_eligible_leaf_target():
    text = """
  pool: tank
 state: DEGRADED
config:

        NAME                     STATE     READ WRITE CKSUM
        tank                     DEGRADED     0     0     0
          raidz1-0               DEGRADED     0     0     0
            1234567890123456789  UNAVAIL      0     0     0
            /dev/sdb             ONLINE       0     0     0
            /dev/sdc             ONLINE       0     0     0

errors: No known data errors
"""
    failed = _failed_leaf_vdevs("tank", text)
    assert failed[0]["device"] == "1234567890123456789"
    assert failed[0]["state"] == "UNAVAIL"


def test_validate_replacement_choice_requires_live_exact_match():
    inventory = {
        "failed": [{"pool": "tank", "device": "/dev/sda", "state": "FAULTED"}],
        "candidates": [
            {
                "device": "/dev/disk/by-id/wwn-new",
                "path": "/dev/sdz",
                "size_bytes": 10_000,
            }
        ],
    }
    failed, candidate = validate_replacement_choice(
        inventory,
        ReplacementRequest("tank", "/dev/sda", "/dev/disk/by-id/wwn-new"),
    )
    assert failed["state"] == "FAULTED"
    assert candidate["path"] == "/dev/sdz"

    with pytest.raises(ValueError):
        validate_replacement_choice(
            inventory,
            ReplacementRequest("tank", "/dev/sda", "/dev/sdy"),
        )


def test_stable_id_map():
    mapped = _stable_id_map(
        "/dev/sda\t/dev/disk/by-id/ata-drive-a\n"
        "/dev/sda\t/dev/disk/by-id/wwn-drive-a\n"
    )
    assert mapped["/dev/sda"] == [
        "/dev/disk/by-id/ata-drive-a",
        "/dev/disk/by-id/wwn-drive-a",
    ]
