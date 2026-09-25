from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace("%", "").replace("x", "")))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("%", "").replace("x", ""))
    except (TypeError, ValueError):
        return default


def parse_os_release(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value.strip().strip('"')
    return result


def parse_meminfo(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2:
            mult = 1024 if len(parts) > 2 and parts[2].lower() == "kb" else 1
            values[parts[0]] = _int(parts[1]) * mult
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": max(total - available, 0),
        "used_pct": round((total - available) * 100 / total, 2) if total else 0.0,
        "swap_total_bytes": values.get("SwapTotal", 0),
        "swap_free_bytes": values.get("SwapFree", 0),
    }


def parse_arcstats(text: str) -> dict[str, Any]:
    stats: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] not in {"13", "name"}:
            try:
                stats[parts[0]] = int(parts[2])
            except ValueError:
                continue
    hits = stats.get("hits", 0)
    misses = stats.get("misses", 0)
    total = hits + misses
    l2_hits = stats.get("l2_hits", 0)
    l2_misses = stats.get("l2_misses", 0)
    l2_total = l2_hits + l2_misses
    return {
        "size_bytes": stats.get("size", 0),
        "target_bytes": stats.get("c", 0),
        "min_bytes": stats.get("c_min", 0),
        "max_bytes": stats.get("c_max", 0),
        "hits": hits,
        "misses": misses,
        "hit_rate_pct": round(hits * 100 / total, 2) if total else 0.0,
        "mru_hits": stats.get("mru_hits", 0),
        "mfu_hits": stats.get("mfu_hits", 0),
        "prefetch_data_hits": stats.get("prefetch_data_hits", 0),
        "prefetch_metadata_hits": stats.get("prefetch_metadata_hits", 0),
        "l2_size_bytes": stats.get("l2_size", stats.get("l2_asize", 0)),
        "l2_hits": l2_hits,
        "l2_misses": l2_misses,
        "l2_hit_rate_pct": round(l2_hits * 100 / l2_total, 2) if l2_total else 0.0,
        "raw": stats,
    }


def parse_pool_list(text: str) -> list[dict[str, Any]]:
    pools: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 8:
            parts = line.split()
        if len(parts) < 8:
            continue
        pools.append({
            "name": parts[0],
            "size_bytes": _int(parts[1]),
            "alloc_bytes": _int(parts[2]),
            "free_bytes": _int(parts[3]),
            "fragmentation_pct": _float(parts[4]),
            "capacity_pct": _float(parts[5]),
            "dedup_ratio": _float(parts[6], 1.0),
            "health": parts[7],
        })
    return pools


def _scrub_finished(scan: str) -> str | None:
    match = re.search(r"\bon\s+([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})", scan)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%a %b %d %H:%M:%S %Y").isoformat()
    except ValueError:
        return None


def parse_pool_status(text: str) -> dict[str, Any]:
    state = ""
    scan = ""
    errors = ""
    config: list[dict[str, Any]] = []
    in_config = False
    role = "data"
    role_markers = {"logs": "log", "cache": "cache", "special": "special", "dedup": "dedup", "spares": "spare"}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("state:"):
            state = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("scan:"):
            scan = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("errors:"):
            errors = stripped.split(":", 1)[1].strip()
        elif stripped == "config:":
            in_config = True
            role = "data"
        elif in_config and stripped in role_markers:
            role = role_markers[stripped]
        elif in_config and stripped.startswith("NAME") and "CKSUM" in stripped:
            continue
        elif in_config and stripped:
            parts = stripped.split()
            if len(parts) >= 5 and parts[-4] in {
                "ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED", "AVAIL"
            }:
                config.append({
                    "name": " ".join(parts[:-4]),
                    "state": parts[-4],
                    "read_errors": _int(parts[-3]),
                    "write_errors": _int(parts[-2]),
                    "checksum_errors": _int(parts[-1]),
                    "role": role,
                    "indent": len(line) - len(line.lstrip()),
                })
    return {
        "state": state,
        "scan": scan,
        "errors": errors,
        "scrub_finished_at": _scrub_finished(scan),
        "vdevs": config,
        "raw": text,
    }


def parse_zfs_list(text: str) -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        datasets.append({
            "name": parts[0],
            "type": parts[1],
            "used_bytes": _int(parts[2]),
            "available_bytes": _int(parts[3]),
            "referenced_bytes": _int(parts[4]),
            "mountpoint": parts[5],
            "compression_ratio": _float(parts[6], 1.0),
            "logical_used_bytes": _int(parts[7]),
            "snapshot_used_bytes": _int(parts[8]),
        })
    return datasets


def parse_iostat(text: str, pool_names: set[str]) -> dict[str, dict[str, int]]:
    latest: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[0] in pool_names:
            latest[parts[0]] = {
                "alloc_bytes": _int(parts[1]),
                "free_bytes": _int(parts[2]),
                "read_iops": _int(parts[3]),
                "write_iops": _int(parts[4]),
                "read_bps": _int(parts[5]),
                "write_bps": _int(parts[6]),
            }
    return latest


def _flatten_disks(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("type") == "disk":
            result.append({
                "name": node.get("name"),
                "path": node.get("path") or (f"/dev/{node.get('name')}" if node.get("name") else ""),
                "size_bytes": _int(node.get("size")),
                "rotational": bool(node.get("rota")),
                "transport": node.get("tran"),
                "model": (node.get("model") or "").strip(),
                "serial": (node.get("serial") or "").strip(),
                "fstype": node.get("fstype"),
                "mountpoints": node.get("mountpoints") or [],
            })
        result.extend(_flatten_disks(node.get("children") or []))
    return result


def parse_smart(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    result: dict[str, Any] = {
        "model": data.get("model_name") or data.get("model_family"),
        "serial": data.get("serial_number"),
        "firmware": data.get("firmware_version"),
        "smart_passed": (data.get("smart_status") or {}).get("passed"),
    }
    temp = (data.get("temperature") or {}).get("current")
    nvme = data.get("nvme_smart_health_information_log") or {}
    if temp is None:
        temp = nvme.get("temperature")
    result["temperature_c"] = temp
    power = data.get("power_on_time") or {}
    result["power_on_hours"] = power.get("hours")
    result["percentage_used"] = nvme.get("percentage_used")
    result["media_errors"] = nvme.get("media_errors")
    attrs = ((data.get("ata_smart_attributes") or {}).get("table") or [])
    by_id = {a.get("id"): a for a in attrs}
    for smart_id, key in [(5, "reallocated_sectors"), (197, "pending_sectors"), (198, "offline_uncorrectable")]:
        raw = (by_id.get(smart_id) or {}).get("raw") or {}
        result[key] = raw.get("value")
    return result


def _device_matches_disk(vdev_name: str, disk_path: str) -> bool:
    if vdev_name == disk_path:
        return True
    if not vdev_name.startswith(disk_path):
        return False
    suffix = vdev_name[len(disk_path):]
    return bool(suffix) and (suffix[0].isdigit() or suffix.startswith("p"))


def build_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    os_release = parse_os_release(raw.get("os_release", {}).get("stdout", ""))
    mem = parse_meminfo(raw.get("meminfo", {}).get("stdout", ""))
    pools = parse_pool_list(raw.get("zpool_list", {}).get("stdout", ""))
    pool_names = {p["name"] for p in pools}
    iostat = parse_iostat(raw.get("zpool_iostat", {}).get("stdout", ""), pool_names)
    statuses = raw.get("zpool_status", {})
    for pool in pools:
        pool["io"] = iostat.get(pool["name"], {})
        pool["status"] = parse_pool_status(statuses.get(pool["name"], {}).get("stdout", ""))

    try:
        block = json.loads(raw.get("lsblk", {}).get("stdout", "{}"))
    except json.JSONDecodeError:
        block = {}
    disks = _flatten_disks(block.get("blockdevices", []))
    smart_map = raw.get("smart", {})
    for disk in disks:
        smart_result = smart_map.get(disk["path"], {})
        disk["smart"] = parse_smart(smart_result.get("stdout", "")) if smart_result and smart_result.get("exit") == 0 else {}
        memberships = []
        for pool in pools:
            for vdev in pool.get("status", {}).get("vdevs", []):
                if _device_matches_disk(str(vdev.get("name", "")), str(disk.get("path", ""))):
                    memberships.append({"pool": pool["name"], "role": vdev.get("role", "data"), "vdev": vdev.get("name")})
        disk["zfs_memberships"] = memberships

    load_parts = raw.get("loadavg", {}).get("stdout", "").split()
    try:
        uptime_seconds = float(raw.get("uptime", {}).get("stdout", "0").split()[0])
    except (ValueError, IndexError):
        uptime_seconds = 0.0

    return {
        "system": {
            "hostname": raw.get("hostname", {}).get("stdout", "").strip(),
            "os": os_release.get("PRETTY_NAME", os_release.get("NAME", "Unknown")),
            "kernel": raw.get("kernel", {}).get("stdout", "").strip(),
            "zfs_version": raw.get("zfs_version", {}).get("stdout", "").strip(),
            "uptime_seconds": uptime_seconds,
            "load1": _float(load_parts[0]) if load_parts else 0.0,
            "load5": _float(load_parts[1]) if len(load_parts) > 1 else 0.0,
            "load15": _float(load_parts[2]) if len(load_parts) > 2 else 0.0,
            "memory": mem,
            "services": raw.get("services", {}).get("stdout", "").strip(),
        },
        "arc": parse_arcstats(raw.get("arcstats", {}).get("stdout", "")),
        "pools": pools,
        "datasets": parse_zfs_list(raw.get("zfs_list", {}).get("stdout", "")),
        "drives": disks,
        "collection": {"smart_included": bool(raw.get("smart"))},
    }
