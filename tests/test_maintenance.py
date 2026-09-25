import pytest

from app.maintenance import (
    ReplacementRequest,
    _failed_leaf_vdevs,
    _stable_id_map,
    validate_replacement_choice,
)


def test_failed_leaf_vdevs_excludes_parent_and_uses_guid():
    text = """
  pool: tank
 state: DEGRADED
config:

        NAME                     STATE     READ WRITE CKSUM
        tank                     DEGRADED     0     0     0
          mirror-0               DEGRADED     0     0     0
            /dev/sda             FAULTED      1     0     2
            /dev/sdb             ONLINE       0     0     0

errors: No known data errors
"""
    guid_text = """
  pool: tank
 state: DEGRADED
config:

        NAME                     STATE     READ WRITE CKSUM
        100                      DEGRADED     0     0     0
          200                    DEGRADED     0     0     0
            300                  FAULTED      1     0     2
            400                  ONLINE       0     0     0

errors: No known data errors
"""
    failed = _failed_leaf_vdevs("tank", text, guid_text=guid_text)
    assert len(failed) == 1
    assert failed[0]["device"] == "/dev/sda"
    assert failed[0]["guid"] == "300"
    assert failed[0]["state"] == "FAULTED"


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
    guid_text = text.replace("tank                     DEGRADED", "999                      DEGRADED").replace(
        "raidz1-0               DEGRADED", "888                      DEGRADED"
    )
    failed = _failed_leaf_vdevs("tank", text, guid_text=guid_text)
    assert failed[0]["device"] == "1234567890123456789"
    assert failed[0]["guid"] == "1234567890123456789"


def test_validate_replacement_requires_live_guid_and_size():
    inventory = {
        "failed": [
            {
                "pool": "tank",
                "device": "/dev/sda",
                "guid": "123",
                "state": "FAULTED",
                "size_bytes": 10_000,
                "operation_active": False,
            }
        ],
        "candidates": [
            {
                "device": "/dev/disk/by-id/wwn-new",
                "path": "/dev/sdz",
                "size_bytes": 20_000,
            },
            {
                "device": "/dev/disk/by-id/wwn-small",
                "path": "/dev/sdy",
                "size_bytes": 5_000,
            },
        ],
    }
    failed, candidate = validate_replacement_choice(
        inventory,
        ReplacementRequest(
            "tank",
            "123",
            "/dev/sda",
            "/dev/disk/by-id/wwn-new",
        ),
    )
    assert failed["state"] == "FAULTED"
    assert candidate["path"] == "/dev/sdz"

    with pytest.raises(ValueError, match="smaller"):
        validate_replacement_choice(
            inventory,
            ReplacementRequest(
                "tank",
                "123",
                "/dev/sda",
                "/dev/disk/by-id/wwn-small",
            ),
        )

    with pytest.raises(ValueError):
        validate_replacement_choice(
            inventory,
            ReplacementRequest(
                "tank",
                "999",
                "/dev/sda",
                "/dev/disk/by-id/wwn-new",
            ),
        )


def test_conflicting_operation_requires_explicit_override():
    inventory = {
        "failed": [
            {
                "pool": "tank",
                "device": "/dev/sda",
                "guid": "123",
                "state": "FAULTED",
                "size_bytes": 10_000,
                "operation_active": True,
            }
        ],
        "candidates": [
            {
                "device": "/dev/disk/by-id/wwn-new",
                "path": "/dev/sdz",
                "size_bytes": 20_000,
            }
        ],
    }
    with pytest.raises(ValueError, match="already in progress"):
        validate_replacement_choice(
            inventory,
            ReplacementRequest(
                "tank",
                "123",
                "/dev/sda",
                "/dev/disk/by-id/wwn-new",
            ),
        )

    validate_replacement_choice(
        inventory,
        ReplacementRequest(
            "tank",
            "123",
            "/dev/sda",
            "/dev/disk/by-id/wwn-new",
            allow_conflicting_operation=True,
        ),
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
