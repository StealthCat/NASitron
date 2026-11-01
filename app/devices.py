"""Identify Linux ZFS volume devices, including retained inventory snapshots."""
import re


def is_zvol(disk):
    return any(
        re.fullmatch(r"(?:/dev/)?zd\d+(?:p\d+)?", str(disk.get(key) or ""))
        or str(disk.get(key) or "").startswith("/dev/zvol/")
        for key in ("kname", "name", "path")
    )


def is_same_or_partition(name, disk_path):
    """True when ``name`` is ``disk_path`` itself or one of its partitions.

    Linux appends a bare partition number to names ending in a letter
    (sda -> sda1) and inserts ``p`` after names ending in a digit
    (nvme0n1 -> nvme0n1p1). Prefix matching alone would wrongly map
    /dev/sdap to /dev/sda or /dev/nvme0n10 to /dev/nvme0n1.
    """
    name, disk_path = str(name or ""), str(disk_path or "")
    if not disk_path:
        return False
    if name == disk_path:
        return True
    if not name.startswith(disk_path):
        return False
    suffix = name[len(disk_path):]
    pattern = r"(?:p\d+|-part\d+)" if disk_path[-1].isdigit() else r"(?:\d+|-part\d+)"
    return re.fullmatch(pattern, suffix) is not None


def device_label(disk):
    if is_zvol(disk):
        return "ZFS virtual volume (zvol)"
    return "HDD" if disk.get("rotational") else "SSD/NVMe"


def drive_health(disk):
    from .experience import smart_state
    return ("unknown", "SMART not applicable") if is_zvol(disk) else smart_state(disk.get("smart"))
