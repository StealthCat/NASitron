"""Identify Linux ZFS volume devices, including retained inventory snapshots."""
import re


def is_zvol(disk):
    return any(
        re.fullmatch(r"(?:/dev/)?zd\d+(?:p\d+)?", str(disk.get(key) or ""))
        or str(disk.get(key) or "").startswith("/dev/zvol/")
        for key in ("kname", "name", "path")
    )


def device_label(disk):
    if is_zvol(disk):
        return "ZFS virtual volume (zvol)"
    return "HDD" if disk.get("rotational") else "SSD/NVMe"


def drive_health(disk):
    from .experience import smart_state
    return ("unknown", "SMART not applicable") if is_zvol(disk) else smart_state(disk.get("smart"))
