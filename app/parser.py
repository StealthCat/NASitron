from __future__ import annotations

from .devices import is_zvol

from .disk_io import parse_disk_io
from .pool_capacity import parse_capacity

import json
import re
from datetime import datetime, timezone
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
        pools.append(
            {
                "name": parts[0],
                "size_bytes": _int(parts[1]),
                "alloc_bytes": _int(parts[2]),
                "free_bytes": _int(parts[3]),
                "fragmentation_pct": _float(parts[4]),
                "capacity_pct": _float(parts[5]),
                "dedup_ratio": _float(parts[6], 1.0),
                "health": parts[7],
            }
        )
    return pools


def _scrub_finished(scan: str) -> str | None:
    match = re.search(
        r"\bon\s+([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})",
        scan,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%a %b %d %H:%M:%S %Y").isoformat()
    except ValueError:
        return None


_ZFS_STATES = {
    "ONLINE",
    "HEALTHY",
    "DEGRADED",
    "FAULTED",
    "OFFLINE",
    "UNAVAIL",
    "REMOVED",
    "AVAIL",
}


def _normalize_vdev_state(value: Any) -> str:
    state = str(value or "").upper()
    return "ONLINE" if state == "HEALTHY" else state


def parse_status_sections(text: str) -> list[dict[str, str]]:
    """Keep all human-readable status sections, including verbose error paths."""
    sections = []
    current = None
    for line in text.expandtabs(8).splitlines():
        header = re.match(r"^ {0,6}([a-z][a-z0-9_ ]*):\s*(.*)$", line)
        if header:
            key, value = header.groups()
            current = {"key": key, "text": value}
            if key not in {"pool", "state", "config"}:
                sections.append(current)
        elif current is not None and line.strip():
            current["text"] += "\n" + line.strip()
    return sections


def parse_pool_status(text: str) -> dict[str, Any]:
    state = ""
    scan = ""
    errors = ""
    scan_lines = []
    in_scan = False
    config: list[dict[str, Any]] = []
    in_config = False
    role = "data"
    role_markers = {
        "logs": "log",
        "cache": "cache",
        "special": "special",
        "dedup": "dedup",
        "spares": "spare",
    }

    for line in text.splitlines():
        line = line.expandtabs(8)
        stripped = line.strip()
        if in_scan and stripped and not re.match(r"[a-z]+:", stripped):
            scan_lines.append(stripped)
        elif re.match(r"[a-z]+:", stripped):
            in_scan = False
        if stripped.startswith("state:"):
            state = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("scan:"):
            scan = stripped.split(":", 1)[1].strip()
            scan_lines.append(scan)
            in_scan = True
        elif stripped.startswith("errors:"):
            errors = stripped.split(":", 1)[1].strip()
            in_config = False
        elif stripped == "config:":
            in_config = True
            role = "data"
        elif in_config and stripped in role_markers:
            role = role_markers[stripped]
        elif in_config and stripped.startswith("NAME") and "CKSUM" in stripped:
            continue
        elif in_config and stripped:
            parts = stripped.split()
            state_index = next(
                (idx for idx, part in enumerate(parts) if part.upper() in _ZFS_STATES),
                None,
            )
            if state_index is None or state_index == 0 or len(parts) < state_index + 1:
                continue
            config.append(
                {
                    "name": " ".join(parts[:state_index]),
                    "state": _normalize_vdev_state(parts[state_index]),
                    "read_errors": _int(parts[state_index + 1]) if len(parts) > state_index + 1 else None,
                    "write_errors": _int(parts[state_index + 2]) if len(parts) > state_index + 2 else None,
                    "checksum_errors": _int(parts[state_index + 3]) if len(parts) > state_index + 3 else None,
                    "role": role,
                    "indent": len(line) - len(line.lstrip()),
                    "detail": " ".join(parts[state_index + 4:]),
                    "guid": None,
                    "size_bytes": 0,
                    "leaf": False,
                }
            )

    for index, entry in enumerate(config):
        next_indent = config[index + 1]["indent"] if index + 1 < len(config) else -1
        entry["leaf"] = (next_indent <= entry["indent"] or
                         (index + 1 < len(config) and config[index + 1]["role"] != entry["role"]))

    return {
        "state": state,
        "scan": scan,
        "scan_detail": "\n".join(scan_lines),
        "errors": errors,
        "scrub_finished_at": _scrub_finished(scan),
        "vdevs": config,
        "raw": text,
        "structured": False,
        "sections": parse_status_sections(text),
    }


def _json_children(node: dict[str, Any]) -> dict[str, Any]:
    children = node.get("vdevs")
    return children if isinstance(children, dict) else {}


def _flatten_json_vdevs(
    nodes: dict[str, Any],
    *,
    role: str,
    depth: int,
    output: list[dict[str, Any]],
) -> None:
    for key, value in nodes.items():
        if not isinstance(value, dict):
            continue
        path = str(value.get("path") or "")
        name = path or str(value.get("name") or key)
        children = _json_children(value)
        output.append(
            {
                "name": name,
                "state": _normalize_vdev_state(value.get("state")),
                "read_errors": _int(value.get("read_errors")),
                "write_errors": _int(value.get("write_errors")),
                "checksum_errors": _int(value.get("checksum_errors")),
                "role": role,
                "indent": depth * 2,
                "guid": str(value.get("guid")) if value.get("guid") is not None else None,
                "size_bytes": _int(
                    value.get("rep_dev_size")
                    or value.get("phys_space")
                    or value.get("total_space")
                ),
                "leaf": not bool(children),
                "vdev_type": value.get("vdev_type"),
            }
        )
        if children:
            _flatten_json_vdevs(children, role=role, depth=depth + 1, output=output)


def parse_pool_status_json(
    json_text: str,
    pool_name: str,
    text_fallback: str = "",
) -> dict[str, Any] | None:
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return None

    pools = data.get("pools")
    if not isinstance(pools, dict) or not pools:
        return None
    pool = pools.get(pool_name)
    if not isinstance(pool, dict):
        pool = next((value for value in pools.values() if isinstance(value, dict)), None)
    if not isinstance(pool, dict):
        return None

    fallback = parse_pool_status(text_fallback) if text_fallback else {
        "scan": "",
        "errors": "",
        "scrub_finished_at": None,
    }
    vdevs: list[dict[str, Any]] = []

    root_nodes = pool.get("vdevs")
    if isinstance(root_nodes, dict):
        _flatten_json_vdevs(root_nodes, role="data", depth=0, output=vdevs)

    for key, role in (
        ("logs", "log"),
        ("cache", "cache"),
        ("special", "special"),
        ("dedup", "dedup"),
        ("spares", "spare"),
    ):
        nodes = pool.get(key)
        if isinstance(nodes, dict):
            _flatten_json_vdevs(nodes, role=role, depth=0, output=vdevs)

    text_nodes = {v["name"]: v for v in fallback.get("vdevs", [])}
    for vdev in vdevs:
        vdev["detail"] = text_nodes.get(vdev["name"], {}).get("detail", "")

    return {
        "state": str(pool.get("state") or fallback.get("state") or ""),
        "scan": fallback.get("scan", ""),
        "scan_detail": fallback.get("scan_detail", ""),
        "errors": fallback.get("errors", ""),
        "scrub_finished_at": fallback.get("scrub_finished_at"),
        "vdevs": vdevs,
        "raw": text_fallback,
        "structured": True,
        "sections": fallback.get("sections", []),
    }


def parse_zfs_list(text: str) -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        datasets.append(
            {
                "name": parts[0],
                "type": parts[1],
                "used_bytes": _int(parts[2]),
                "available_bytes": _int(parts[3]),
                "referenced_bytes": _int(parts[4]),
                "mountpoint": parts[5],
                "compression_ratio": _float(parts[6], 1.0),
                "logical_used_bytes": _int(parts[7]),
                "snapshot_used_bytes": _int(parts[8]),
            }
        )
    return datasets


def parse_property_rows(text: str) -> dict[str, list[dict[str, Any]]]:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        name, prop, value, source = parts[0], parts[1], parts[2], parts[3]
        row = {
            "property": prop,
            "value": value,
            "source": source,
            "is_set": source.lower() not in {"default", "-", "none"},
        }
        by_name.setdefault(name, []).append(row)

    for rows in by_name.values():
        rows.sort(
            key=lambda row: (
                not bool(row["is_set"]),
                str(row["property"]).lower(),
            )
        )
    return by_name


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
            result.append(
                {
                    "name": node.get("name"),
                    "kname": node.get("kname") or node.get("name"),
                    "path": node.get("path")
                    or (f"/dev/{node.get('name')}" if node.get("name") else ""),
                    "size_bytes": _int(node.get("size")),
                    "rotational": bool(node.get("rota")),
                    "transport": node.get("tran"),
                    "model": (node.get("model") or "").strip(),
                    "serial": (node.get("serial") or "").strip(),
                    "fstype": node.get("fstype"),
                    "uuid": node.get("uuid"),
                    "pttype": node.get("pttype"),
                    "parttype": node.get("parttype"),
                    "mountpoints": node.get("mountpoints") or [],
                }
            )
        result.extend(_flatten_disks(node.get("children") or []))
    return result


SMART_EXIT_BITS = {
    0: "command_line_error",
    1: "device_open_or_identity_error",
    2: "smart_command_or_checksum_error",
    3: "disk_failing",
    4: "prefail_attribute",
    5: "past_threshold_attribute",
    6: "error_log_records",
    7: "self_test_errors",
}


def parse_smart(text: str, command_exit: int | None = None) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        status = int(command_exit or 0)
        return {
            "data_available": False,
            "command_exit": status,
            "exit_findings": [
                label for bit, label in SMART_EXIT_BITS.items() if status & (1 << bit)
            ],
        }

    embedded_exit = ((data.get("smartctl") or {}).get("exit_status"))
    status = _int(embedded_exit, int(command_exit or 0))
    result: dict[str, Any] = {
        "data_available": True,
        "command_exit": status,
        "exit_findings": [
            label for bit, label in SMART_EXIT_BITS.items() if status & (1 << bit)
        ],
        "model": data.get("model_name") or data.get("model_family"),
        "serial": data.get("serial_number"),
        "firmware": data.get("firmware_version"),
        "smart_passed": (data.get("smart_status") or {}).get("passed"),
    }
    if result["smart_passed"] is None and status & (1 << 3):
        result["smart_passed"] = False

    temp = (data.get("temperature") or {}).get("current")
    nvme = data.get("nvme_smart_health_information_log") or {}
    if temp is None:
        temp = nvme.get("temperature")
    result["temperature_c"] = temp
    result["power_on_hours"] = (data.get("power_on_time") or {}).get("hours")
    result["percentage_used"] = nvme.get("percentage_used")
    result["media_errors"] = nvme.get("media_errors")

    attrs = ((data.get("ata_smart_attributes") or {}).get("table") or [])
    by_id = {a.get("id"): a for a in attrs}
    for smart_id, key in (
        (5, "reallocated_sectors"),
        (197, "pending_sectors"),
        (198, "offline_uncorrectable"),
    ):
        raw = (by_id.get(smart_id) or {}).get("raw") or {}
        result[key] = raw.get("value")
    return result


def _device_matches_disk(vdev_name: str, disk_path: str) -> bool:
    if vdev_name == disk_path:
        return True
    if not vdev_name.startswith(disk_path):
        return False
    suffix = vdev_name[len(disk_path) :]
    return bool(suffix) and (suffix[0].isdigit() or suffix.startswith("p"))


def build_snapshot(raw: dict[str, Any], captured_at: datetime | None = None) -> dict[str, Any]:
    captured_at = captured_at or datetime.utcnow()
    sampled_at = captured_at.isoformat() + "Z"

    os_release = parse_os_release(raw.get("os_release", {}).get("stdout", ""))
    mem = parse_meminfo(raw.get("meminfo", {}).get("stdout", ""))
    pools = parse_pool_list(raw.get("zpool_list", {}).get("stdout", ""))
    pool_names = {p["name"] for p in pools}
    iostat = parse_iostat(raw.get("zpool_iostat", {}).get("stdout", ""), pool_names)
    pool_property_results = raw.get("zpool_get", {})
    dataset_properties = parse_property_rows(
        raw.get("zfs_get", {}).get("stdout", "")
    )
    datasets = parse_zfs_list(raw.get("zfs_list", {}).get("stdout", ""))
    for dataset in datasets:
        dataset["properties"] = dataset_properties.get(dataset["name"], [])
    datasets_by_name = {
        str(dataset.get("name")): dataset
        for dataset in datasets
        if dataset.get("name")
    }

    errors: list[dict[str, str]] = []
    freshness: dict[str, str] = {}
    checked_commands = {
        "hostname": "system.hostname",
        "os_release": "system.os",
        "kernel": "system.kernel",
        "uptime": "system.uptime",
        "loadavg": "system.load",
        "meminfo": "system.memory",
        "zfs_version": "zfs.version",
        "zfs_list": "zfs.datasets",
        "zfs_get": "zfs.dataset_properties",
        "arcstats": "zfs.arc",
        "zpool_iostat": "zfs.iostat",
        "lsblk": "drives.inventory",
        "disk_io": "drives.io",
        "services": "zfs.services",
    }
    failed_subsystems: set[str] = set()
    for key, subsystem in checked_commands.items():
        result = raw.get(key, {})
        failed = (
            result.get("exit") != 0
            or result.get("stdout_truncated")
            or result.get("stderr_truncated")
        )
        if failed:
            failed_subsystems.add(subsystem)
            reason = result.get("stderr") or result.get("stdout") or f"{key} failed"
            if result.get("stdout_truncated") or result.get("stderr_truncated"):
                reason = f"{key} output exceeded NASitron's configured capture limit"
            errors.append({"subsystem": subsystem, "message": reason[:1000]})
        else:
            freshness[subsystem] = sampled_at

    text_statuses = raw.get("zpool_status", {})
    json_statuses = raw.get("zpool_status_json", {})
    pool_status_ok: list[str] = []
    for pool in pools:
        name = pool["name"]
        capacity_result = raw.get("zpool_list_verbose", {}).get(name, {})
        capacity_key = f"pool.capacity:{name}"
        pool["capacity_detail"] = {"fresh": False, "rows": []}
        try:
            if (capacity_result.get("exit") != 0 or capacity_result.get("stdout_truncated")
                    or capacity_result.get("stderr_truncated")):
                raise ValueError("Verbose pool capacity collection failed or was truncated.")
            capacity = parse_capacity(capacity_result.get("stdout", ""), name)
            pool["capacity_detail"] = {"fresh": True, "rows": capacity, "captured_at": sampled_at,
                                       "raw": capacity_result.get("stdout", "")}
            freshness[capacity_key] = sampled_at
        except (ValueError, OverflowError):
            errors.append({"subsystem": capacity_key,
                           "message": "Verbose pool capacity was unavailable, truncated or malformed."})
            failed_subsystems.add(capacity_key)
        text_result = text_statuses.get(name, {})
        json_result = json_statuses.get(name, {})
        text = text_result.get("stdout", "")

        parsed_json = None
        json_valid = (
            json_result.get("exit") == 0
            and not json_result.get("stdout_truncated")
            and not json_result.get("stderr_truncated")
        )
        if json_valid:
            parsed_json = parse_pool_status_json(
                json_result.get("stdout", ""), name, text
            )
            json_valid = bool(
                parsed_json
                and parsed_json.get("state")
                and parsed_json.get("vdevs")
            )

        parsed_text = None
        text_valid = (
            text_result.get("exit") == 0
            and not text_result.get("stdout_truncated")
            and not text_result.get("stderr_truncated")
        )
        if text_valid:
            parsed_text = parse_pool_status(text)
            text_valid = bool(
                parsed_text.get("state")
                and parsed_text.get("vdevs")
            )

        if json_valid:
            parsed = parsed_json
        elif text_valid:
            parsed = parsed_text
        else:
            parsed = parsed_json or parsed_text or parse_pool_status(text)
            subsystem = f"pool.status:{name}"
            failed_subsystems.add(subsystem)
            errors.append(
                {
                    "subsystem": subsystem,
                    "message": "Detailed pool status was missing, truncated, or malformed.",
                }
            )

        parsed["verbose_complete"] = text_valid
        pool["io"] = iostat.get(name, {})
        pool["status"] = parsed
        root_dataset = datasets_by_name.get(name)
        pool["compression_ratio"] = (
            root_dataset.get("compression_ratio")
            if root_dataset is not None
            else None
        )

        property_result = pool_property_results.get(name, {})
        property_valid = (
            property_result.get("exit") == 0
            and not property_result.get("stdout_truncated")
            and not property_result.get("stderr_truncated")
        )
        if property_valid:
            pool["properties"] = parse_property_rows(
                property_result.get("stdout", "")
            ).get(name, [])
            freshness[f"pool.properties:{name}"] = sampled_at
        else:
            pool["properties"] = []
            subsystem = f"pool.properties:{name}"
            failed_subsystems.add(subsystem)
            reason = (
                property_result.get("stderr")
                or property_result.get("stdout")
                or "Pool property query failed."
            )
            errors.append({"subsystem": subsystem, "message": reason[:1000]})

        if json_valid or text_valid:
            pool_status_ok.append(name)
            freshness[f"pool.status:{name}"] = sampled_at

    try:
        block = json.loads(raw.get("lsblk", {}).get("stdout", "{}"))
    except json.JSONDecodeError:
        block = {}
        if "drives.inventory" not in failed_subsystems:
            failed_subsystems.add("drives.inventory")
            errors.append(
                {
                    "subsystem": "drives.inventory",
                    "message": "lsblk returned malformed JSON.",
                }
            )
    disks = _flatten_disks(block.get("blockdevices", []))

    disk_io = {}
    if "drives.io" not in failed_subsystems:
        try:
            disk_io = parse_disk_io(raw["disk_io"].get("stdout", ""))
        except (ValueError, IndexError):
            failed_subsystems.add("drives.io")
            freshness.pop("drives.io", None)
            errors.append({"subsystem": "drives.io", "message": "Disk I/O samples were malformed or incomplete."})
    for disk in disks:
        disk["io"] = disk_io.get(str(disk.get("kname") or "").removeprefix("/dev/"), {})

    smart_map = raw.get("smart", {})
    smart_sampled = bool(raw.get("smart_sampled"))
    smart_inventory_ok = bool(raw.get("smart_inventory_ok"))
    smart_attempted_count = int(raw.get("smart_attempted_count") or 0)
    smart_refreshed: list[str] = []

    for disk in disks:
        smart_result = smart_map.get(disk["path"])
        if is_zvol(disk):
            smart = {"data_available": False, "not_applicable": True}
        elif smart_sampled:
            if smart_result:
                smart = parse_smart(
                    smart_result.get("stdout", ""),
                    smart_result.get("exit"),
                )
                smart_refreshed.append(disk.get("serial") or disk["path"])
            else:
                smart = {
                    "data_available": False,
                    "command_exit": 255,
                    "exit_findings": ["not_attempted"],
                }
            smart["sampled_at"] = sampled_at
            smart["stale"] = False
        else:
            smart = {}
        disk["smart"] = smart

        memberships = []
        for pool in pools:
            for vdev in pool.get("status", {}).get("vdevs", []):
                if _device_matches_disk(
                    str(vdev.get("name", "")), str(disk.get("path", ""))
                ):
                    memberships.append(
                        {
                            "pool": pool["name"],
                            "role": vdev.get("role", "data"),
                            "vdev": vdev.get("name"),
                            "guid": vdev.get("guid"),
                        }
                    )
        disk["zfs_memberships"] = memberships

    if smart_sampled:
        if not smart_inventory_ok:
            errors.append(
                {
                    "subsystem": "smart.inventory",
                    "message": "SMART sampling was requested but disk inventory was unavailable.",
                }
            )
            failed_subsystems.add("smart.inventory")
        for disk in disks:
            smart = disk.get("smart") or {}
            if not smart.get("data_available"):
                subsystem = f"smart:{disk.get('serial') or disk.get('path')}"
                errors.append(
                    {
                        "subsystem": subsystem,
                        "message": "SMART data could not be read for this drive.",
                    }
                )
                failed_subsystems.add(subsystem)
            else:
                freshness[f"smart:{disk.get('serial') or disk.get('path')}"] = sampled_at

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
        "datasets": datasets,
        "snapshot_inventory": parse_snapshot_inventory(raw.get("zfs_snapshots"), sampled_at),
        "drives": disks,
        "collection": {
            "captured_at": sampled_at,
            "smart_sampled": smart_sampled,
            "smart_inventory_ok": smart_inventory_ok,
            "smart_attempted_count": smart_attempted_count,
            "smart_refreshed": smart_refreshed,
            "pool_status_ok": pool_status_ok,
            "freshness": freshness,
            "stale_subsystems": [],
            "errors": errors,
            "partial": bool(errors),
        },
        "capabilities": raw.get("capabilities", {}),
    }


def parse_snapshot_inventory(result, captured_at):
    if result is None:
        return {"fresh": False, "rows": []}
    if result.get("exit") != 0 or result.get("stdout_truncated"):
        return {"fresh": False, "rows": [], "error": "Snapshot inventory unavailable or truncated"}
    rows = []
    for line in result.get("stdout", "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or "@" not in parts[0]:
            return {"fresh": False, "rows": [], "error": "Malformed snapshot inventory"}
        try:
            rows.append({"name": parts[0], "created": datetime.fromtimestamp(int(parts[1]), timezone.utc).isoformat(),
                         "used": int(parts[2]), "referenced": int(parts[3])})
        except (ValueError, OverflowError):
            return {"fresh": False, "rows": [], "error": "Malformed snapshot accounting"}
    return {"fresh": True, "captured_at": captured_at, "rows": rows}
