from app.parser import parse_arcstats, parse_pool_list, parse_pool_status, parse_zfs_list


def test_pool_list():
    pools = parse_pool_list("tank\t1000\t400\t600\t12\t40\t1.00x\tONLINE\n")
    assert pools[0]["name"] == "tank"
    assert pools[0]["capacity_pct"] == 40
    assert pools[0]["health"] == "ONLINE"


def test_pool_status_roles_and_errors():
    text = """
  pool: tank
 state: ONLINE
  scan: scrub repaired 0B in 00:10:00 with 0 errors on Sun Sep 20 01:02:03 2026
config:

        NAME          STATE     READ WRITE CKSUM
        tank          ONLINE       0     0     0
          mirror-0    ONLINE       0     0     0
            /dev/sda  ONLINE       0     0     1
            /dev/sdb  ONLINE       0     0     0
        logs
          /dev/nvme0n1p1 ONLINE    0     0     0
        cache
          /dev/nvme0n1p2 ONLINE    0     0     0

errors: No known data errors
"""
    status = parse_pool_status(text)
    assert status["state"] == "ONLINE"
    assert status["scrub_finished_at"].startswith("2026-09-20")
    by_name = {v["name"]: v for v in status["vdevs"]}
    assert by_name["/dev/sda"]["checksum_errors"] == 1
    assert by_name["/dev/nvme0n1p1"]["role"] == "log"
    assert by_name["/dev/nvme0n1p2"]["role"] == "cache"


def test_zfs_list_nine_columns():
    rows = parse_zfs_list("tank/data\tfilesystem\t100\t900\t80\t/tank/data\t1.25x\t125\t10\n")
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
