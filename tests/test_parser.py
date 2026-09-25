from datetime import datetime

from app.parser import (
    build_snapshot,
    parse_arcstats,
    parse_pool_list,
    parse_pool_status,
    parse_pool_status_json,
    parse_smart,
    parse_zfs_list,
)


def test_pool_list():
    pools = parse_pool_list("tank\t1000\t400\t600\t12\t40\t1.00x\tONLINE\n")
    assert pools[0]["name"] == "tank"
    assert pools[0]["capacity_pct"] == 40
    assert pools[0]["health"] == "ONLINE"


def test_pool_status_roles_errors_and_trailing_resilver_text():
    text = """
  pool: tank
 state: DEGRADED
  scan: resilver in progress since Sun Sep 20 01:02:03 2026
config:

        NAME          STATE     READ WRITE CKSUM
        tank          DEGRADED     0     0     0
          mirror-0    DEGRADED     0     0     0
            /dev/sda  FAULTED      0     0     1  58.5K resilvered
            /dev/sdb  ONLINE       0     0     0
        logs
          /dev/nvme0n1p1 ONLINE    0     0     0
        cache
          /dev/nvme0n1p2 ONLINE    0     0     0

errors: No known data errors
"""
    status = parse_pool_status(text)
    by_name = {v["name"]: v for v in status["vdevs"]}
    assert by_name["/dev/sda"]["checksum_errors"] == 1
    assert by_name["/dev/sda"]["leaf"] is True
    assert by_name["mirror-0"]["leaf"] is False
    assert by_name["/dev/nvme0n1p1"]["role"] == "log"
    assert by_name["/dev/nvme0n1p2"]["role"] == "cache"


def test_pool_status_json_preserves_guid_path_and_size():
    text = """
  pool: tank
 state: DEGRADED
  scan: scrub repaired 0B in 00:10:00 with 0 errors on Sun Sep 20 01:02:03 2026
errors: No known data errors
"""
    payload = """{
      "pools": {
        "tank": {
          "state": "DEGRADED",
          "vdevs": {
            "tank": {
              "name": "tank",
              "guid": 1,
              "state": "DEGRADED",
              "vdevs": {
                "mirror-0": {
                  "name": "mirror-0",
                  "guid": 2,
                  "state": "DEGRADED",
                  "vdevs": {
                    "disk-a": {
                      "name": "disk-a",
                      "path": "/dev/sda",
                      "vdev_type": "disk",
                      "guid": 12345,
                      "state": "FAULTED",
                      "phys_space": 4000000000000,
                      "read_errors": 1,
                      "write_errors": 0,
                      "checksum_errors": 2
                    }
                  }
                }
              }
            }
          }
        }
      }
    }"""
    status = parse_pool_status_json(payload, "tank", text)
    assert status is not None
    leaf = next(v for v in status["vdevs"] if v["name"] == "/dev/sda")
    assert leaf["guid"] == "12345"
    assert leaf["size_bytes"] == 4000000000000
    assert leaf["leaf"] is True
    assert leaf["state"] == "FAULTED"
    assert status["scrub_finished_at"].startswith("2026-09-20")


def test_zfs_list_nine_columns():
    rows = parse_zfs_list(
        "tank/data\tfilesystem\t100\t900\t80\t/tank/data\t1.25x\t125\t10\n"
    )
    assert rows[0]["name"] == "tank/data"
    assert rows[0]["compression_ratio"] == 1.25


def test_arcstats_rates():
    text = """13 1 0x01 85 4080 123456
name                            type data
hits                            4    900
misses                          4    100
size                            4    1048576
c                               4    2097152
l2_hits                         4    30
l2_misses                       4    70
l2_size                         4    524288
"""
    arc = parse_arcstats(text)
    assert arc["hit_rate_pct"] == 90.0
    assert arc["l2_hit_rate_pct"] == 30.0
    assert arc["size_bytes"] == 1048576


def test_smart_nonzero_health_exit_still_parses_json():
    smart = parse_smart(
        '{"smartctl":{"exit_status":8},"smart_status":{"passed":false},'
        '"temperature":{"current":47},"serial_number":"ABC"}',
        command_exit=8,
    )
    assert smart["data_available"] is True
    assert smart["smart_passed"] is False
    assert smart["temperature_c"] == 47
    assert "disk_failing" in smart["exit_findings"]


def test_smart_invalid_json_is_unavailable_not_healthy():
    smart = parse_smart("permission denied", command_exit=2)
    assert smart["data_available"] is False
    assert "smart_passed" not in smart
    assert "device_open_or_identity_error" in smart["exit_findings"]


def test_build_snapshot_marks_partial_collection_and_current_smart():
    def ok(stdout=""):
        return {
            "stdout": stdout,
            "stderr": "",
            "exit": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    raw = {
        "smart_sampled": True,
        "hostname": ok("nas\n"),
        "os_release": ok('PRETTY_NAME="Ubuntu"\n'),
        "kernel": ok("6.8\n"),
        "uptime": ok("1000 0\n"),
        "loadavg": ok("1 2 3 1/1 1\n"),
        "meminfo": ok("MemTotal: 1000 kB\nMemAvailable: 500 kB\n"),
        "zfs_version": ok("zfs-2.2\n"),
        "zpool_list": ok("tank\t1000\t400\t600\t12\t40\t1.00x\tONLINE\n"),
        "zfs_list": ok(""),
        "arcstats": {
            **ok(""),
            "exit": 1,
            "stderr": "arc unavailable",
        },
        "zpool_iostat": ok(""),
        "lsblk": ok('{"blockdevices":[{"name":"sda","kname":"sda","path":"/dev/sda","type":"disk","size":1000,"rota":1,"mountpoints":[]}]}'),
        "services": ok("zfs.target=active\nzfs-zed.service=active\n"),
        "zpool_status": {"tank": ok("  pool: tank\n state: ONLINE\nerrors: No known data errors\n")},
        "zpool_status_json": {"tank": {"**": "unused", **ok(""), "exit": 1}},
        "smart": {
            "/dev/sda": {
                **ok('{"smartctl":{"exit_status":8},"smart_status":{"passed":false}}'),
                "exit": 8,
            }
        },
    }
    snapshot = build_snapshot(raw, datetime(2026, 9, 25, 12, 0, 0))
    assert snapshot["collection"]["partial"] is True
    assert snapshot["drives"][0]["smart"]["smart_passed"] is False
    assert snapshot["drives"][0]["smart"]["stale"] is False
