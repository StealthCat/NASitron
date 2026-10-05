from __future__ import annotations

from .devices import is_zvol

import copy
import json
import shlex
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .collector import CollectorError, SSHCollector
from .config import REMOTE_HELPER_PATH
from .models import Server
from .parser import parse_pool_status, parse_pool_status_json

FAILED_STATES = {"DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"}
_INVENTORY_TTL_SECONDS = 15.0

_lock_guard = threading.Lock()
_server_locks: dict[int, threading.Lock] = {}
_cache_lock = threading.Lock()
_inventory_cache: dict[int, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class ReplacementRequest:
    pool: str
    old_guid: str
    old_device: str
    new_device: str
    allow_conflicting_operation: bool = False


class MaintenanceBusy(RuntimeError):
    pass


@contextmanager
def maintenance_lock(server_id: int) -> Iterator[None]:
    with _lock_guard:
        lock = _server_locks.setdefault(server_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise MaintenanceBusy("Another maintenance operation is already running for this server.")
    try:
        yield
    finally:
        # Keep the lock object registered for the lifetime of the process.
        # Removing it after release creates an ABA race: another thread can
        # fetch the old lock just before removal while a third thread creates
        # a new lock for the same server.
        lock.release()


def _node_has_usage(node: dict[str, Any]) -> bool:
    if node.get("fstype") or node.get("uuid") or node.get("pttype") or node.get("parttype"):
        return True
    if any(x for x in (node.get("mountpoints") or []) if x):
        return True
    return bool(node.get("children"))


def _whole_disks(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("type") == "disk":
            disks.append(node)
        disks.extend(_whole_disks(node.get("children") or []))
    return disks


def _stable_id_map(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        real, stable = parts
        result.setdefault(real.strip(), []).append(stable.strip())
    return result


def _best_stable_path(paths: list[str]) -> str | None:
    if not paths:
        return None
    preferred = sorted(
        paths,
        key=lambda p: (
            0 if "/scsi-" in p else 1 if "/wwn-" in p else 2 if "/nvme-" in p else 3 if "/ata-" in p else 4,
            len(p),
            p,
        ),
    )
    return preferred[0]


def _is_path_on_disk(vdev_path: str, disk_path: str) -> bool:
    if vdev_path == disk_path:
        return True
    if not vdev_path.startswith(disk_path):
        return False
    suffix = vdev_path[len(disk_path) :]
    return bool(suffix) and (suffix[0].isdigit() or suffix.startswith("p"))


def _fallback_status_with_guids(
    text_status: str,
    guid_status: str,
) -> dict[str, Any]:
    parsed = parse_pool_status(text_status)
    guid_parsed = parse_pool_status(guid_status)
    normal_vdevs = parsed.get("vdevs", [])
    guid_vdevs = guid_parsed.get("vdevs", [])
    if len(normal_vdevs) == len(guid_vdevs):
        for normal, guid in zip(normal_vdevs, guid_vdevs):
            candidate = str(guid.get("name") or "")
            if candidate.isdigit():
                normal["guid"] = candidate
    return parsed


def _failed_leaf_vdevs(
    pool: str,
    status_text: str,
    json_text: str = "",
    guid_text: str = "",
) -> list[dict[str, Any]]:
    status = parse_pool_status_json(json_text, pool, status_text) if json_text else None
    if status is None:
        status = _fallback_status_with_guids(status_text, guid_text)

    failed: list[dict[str, Any]] = []
    for entry in status.get("vdevs", []):
        name = str(entry.get("name", ""))
        if not entry.get("leaf") or name == pool:
            continue
        if str(entry.get("state", "")).upper() not in FAILED_STATES:
            continue
        failed.append(
            {
                "pool": pool,
                "device": name,
                "guid": str(entry.get("guid") or ""),
                "state": entry.get("state", "UNKNOWN"),
                "role": entry.get("role", "data"),
                "read_errors": entry.get("read_errors", 0),
                "write_errors": entry.get("write_errors", 0),
                "checksum_errors": entry.get("checksum_errors", 0),
                "size_bytes": int(entry.get("size_bytes") or 0),
                "operation_active": "in progress" in str(status.get("scan", "")).lower(),
                "scan": status.get("scan", ""),
            }
        )
    return failed


def _discover_with_ssh(ssh: SSHCollector) -> dict[str, Any]:
    pool_result = ssh.run("zpool list -H -o name", timeout=20)
    if pool_result["exit"] != 0 or pool_result.get("stdout_truncated"):
        raise CollectorError(
            "Unable to list ZFS pools: "
            + (pool_result["stderr"] or pool_result["stdout"]).strip()
        )

    pools = [line.strip() for line in pool_result["stdout"].splitlines() if line.strip()]
    failed: list[dict[str, Any]] = []
    used_vdev_paths: set[str] = set()
    statuses: dict[str, str] = {}

    for pool in pools:
        quoted = shlex.quote(pool)
        text_result = ssh.run(f"zpool status -P -L {quoted}", timeout=20)
        if text_result["exit"] != 0 or text_result.get("stdout_truncated"):
            raise CollectorError(
                f"Unable to read zpool status for {pool}: "
                + (text_result["stderr"] or text_result["stdout"]).strip()
            )

        json_result = {"stdout": "", "exit": 2}
        guid_result = {"stdout": "", "exit": 1}
        if ssh.server.zpool_status_json_supported is not False:
            json_result = ssh.run(
                f"zpool status -j --json-int -P -L {quoted}",
                timeout=20,
            )
        if json_result.get("exit") != 0 or json_result.get("stdout_truncated"):
            guid_result = ssh.run(f"zpool status -g {quoted}", timeout=20)

        statuses[pool] = text_result["stdout"]
        parsed = (
            parse_pool_status_json(
                json_result.get("stdout", ""),
                pool,
                text_result["stdout"],
            )
            if json_result.get("exit") == 0 and not json_result.get("stdout_truncated")
            else None
        )
        if parsed is None:
            parsed = _fallback_status_with_guids(
                text_result["stdout"],
                guid_result.get("stdout", ""),
            )

        for entry in parsed.get("vdevs", []):
            name = str(entry.get("name", ""))
            if name.startswith("/dev/"):
                used_vdev_paths.add(name)

        stable_result = ssh.run(f"zpool status -P {quoted}", timeout=20)
        stable_names = {}
        if stable_result["exit"] == 0 and not stable_result.get("stdout_truncated"):
            resolved_rows = parse_pool_status(text_result["stdout"])["vdevs"]
            stable_rows = parse_pool_status(stable_result["stdout"])["vdevs"]
            if len(resolved_rows) == len(stable_rows):
                for resolved, stored in zip(resolved_rows, stable_rows):
                    if resolved["state"] != stored["state"] or resolved["indent"] != stored["indent"]:
                        continue
                    name = stored["detail"][4:] if stored["detail"].startswith("was ") else stored["name"]
                    if name.startswith("/dev/disk/by-id/"):
                        stable_names[resolved["name"]] = name.rsplit("/", 1)[-1]
        pool_failed = (
            _failed_leaf_vdevs(
                pool,
                text_result["stdout"],
                json_result.get("stdout", "")
                if json_result.get("exit") == 0
                else "",
                guid_result.get("stdout", "")
                if guid_result.get("exit") == 0
                else "",
            )
        )
        for entry in pool_failed:
            entry["by_id"] = stable_names.get(entry["device"], "")
        failed.extend(pool_failed)

    lsblk_result = ssh.run(
        "lsblk -J -b -o NAME,KNAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,FSTYPE,UUID,PTTYPE,PARTTYPE,MOUNTPOINTS",
        timeout=20,
    )
    if lsblk_result["exit"] != 0 or lsblk_result.get("stdout_truncated"):
        raise CollectorError(
            "Unable to inventory block devices: "
            + (lsblk_result["stderr"] or lsblk_result["stdout"]).strip()
        )
    try:
        block = json.loads(lsblk_result["stdout"])
    except json.JSONDecodeError as exc:
        raise CollectorError(f"Unable to parse lsblk JSON: {exc}") from exc

    by_id_result = ssh.run(
        r"""for p in /dev/disk/by-id/*; do
  [ -e "$p" ] || continue
  [ -b "$p" ] || continue
  printf '%s\t%s\n' "$(readlink -f "$p")" "$p"
done""",
        timeout=20,
    )
    stable_map = (
        _stable_id_map(by_id_result["stdout"])
        if by_id_result["exit"] == 0 and not by_id_result.get("stdout_truncated")
        else {}
    )

    disks = _whole_disks(block.get("blockdevices", []))
    disk_by_path = {str(d.get("path") or ""): d for d in disks}
    for failed_disk in failed:
        if not failed_disk.get("by_id"):
            stable = _best_stable_path(stable_map.get(str(failed_disk.get("device") or ""), []))
            failed_disk["by_id"] = stable.rsplit("/", 1)[-1] if stable else ""
        if failed_disk.get("size_bytes"):
            continue
        device = str(failed_disk.get("device") or "")
        for path, disk in disk_by_path.items():
            if path and _is_path_on_disk(device, path):
                failed_disk["size_bytes"] = int(disk.get("size") or 0)
                break

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    helper = shlex.quote(REMOTE_HELPER_PATH)
    for disk in disks:
        path = str(disk.get("path") or "")
        kname = str(disk.get("kname") or disk.get("name") or "")
        if not path:
            continue

        stable_path = _best_stable_path(stable_map.get(path, []))
        reason = ""
        if is_zvol(disk):
            reason = "ZFS virtual volume; not a physical replacement disk"
        elif stable_path is None:
            reason = "no stable /dev/disk/by-id whole-disk identifier"
        elif _node_has_usage(disk):
            reason = "filesystem, partition table, UUID, mountpoint, or child device present"
        elif any(_is_path_on_disk(vdev, path) for vdev in used_vdev_paths):
            reason = "already belongs to a ZFS pool"
        elif not kname.replace("-", "").replace("_", "").isalnum():
            reason = "unexpected kernel device name"
        else:
            holder_result = ssh.run(
                f'test -z "$(find /sys/class/block/{kname}/holders '
                '-mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)"',
                timeout=10,
            )
            if holder_result["exit"] != 0:
                reason = "device has active kernel holders"
            else:
                wipefs_result = ssh.run(
                    f"sudo -n {helper} wipefs-check {shlex.quote(stable_path)}",
                    timeout=20,
                )
                if wipefs_result["exit"] != 0:
                    reason = "could not prove disk is signature-free with the NASitron root helper"
                elif wipefs_result["stdout"].strip():
                    reason = "filesystem/RAID/partition signature detected"

        item = {
            "path": path,
            "device": stable_path or path,
            "size_bytes": int(disk.get("size") or 0),
            "rotational": bool(disk.get("rota")),
            "transport": disk.get("tran") or "",
            "model": (disk.get("model") or "").strip(),
            "serial": (disk.get("serial") or "").strip(),
        }
        if reason:
            item["reason"] = reason
            rejected.append(item)
        else:
            candidates.append(item)

    candidates.sort(key=lambda d: (d["size_bytes"], d["device"]))
    rejected.sort(key=lambda d: d["device"])
    return {
        "failed": failed,
        "candidates": candidates,
        "rejected_candidates": rejected,
        "statuses": statuses,
    }


def discover_replacement_options(
    server: Server,
    *,
    force: bool = False,
) -> dict[str, Any]:
    now = time.monotonic()
    if not force:
        with _cache_lock:
            cached = _inventory_cache.get(server.id)
            if cached and now - cached[0] <= _INVENTORY_TTL_SECONDS:
                return copy.deepcopy(cached[1])

    with SSHCollector(server) as ssh:
        inventory = _discover_with_ssh(ssh)

    with _cache_lock:
        _inventory_cache[server.id] = (now, copy.deepcopy(inventory))
    return inventory


def invalidate_inventory_cache(server_id: int) -> None:
    with _cache_lock:
        _inventory_cache.pop(server_id, None)


def validate_replacement_choice(
    inventory: dict[str, Any],
    request: ReplacementRequest,
) -> tuple[dict[str, Any], dict[str, Any]]:
    failed = next(
        (
            item
            for item in inventory.get("failed", [])
            if item.get("pool") == request.pool
            and item.get("guid") == request.old_guid
            and item.get("device") == request.old_device
        ),
        None,
    )
    if failed is None:
        raise ValueError(
            "The failed/offline vdev is no longer an eligible replacement target. Refresh the page."
        )
    if not failed.get("guid"):
        raise ValueError(
            "NASitron could not obtain an immutable ZFS vdev GUID for the failed device."
        )
    if failed.get("operation_active") and not request.allow_conflicting_operation:
        raise ValueError(
            "A scrub/resilver operation is already in progress. Wait for it to finish or explicitly allow the conflicting operation."
        )

    candidate = next(
        (
            item
            for item in inventory.get("candidates", [])
            if item.get("device") == request.new_device
        ),
        None,
    )
    if candidate is None:
        raise ValueError(
            "The selected replacement disk is no longer blank, stable-ID-addressable, "
            "unallocated, or present."
        )

    required_size = int(failed.get("size_bytes") or 0)
    candidate_size = int(candidate.get("size_bytes") or 0)
    if required_size and candidate_size < required_size:
        raise ValueError(
            "The replacement disk is smaller than the failed vdev's representative device size."
        )
    return failed, candidate


def replace_drive(server: Server, request: ReplacementRequest) -> dict[str, Any]:
    with maintenance_lock(server.id):
        with SSHCollector(server) as ssh:
            inventory = _discover_with_ssh(ssh)
            failed, candidate = validate_replacement_choice(inventory, request)

            helper = shlex.quote(REMOTE_HELPER_PATH)
            allow = "1" if request.allow_conflicting_operation else "0"
            command = (
                f"sudo -n {helper} replace "
                + shlex.quote(request.pool)
                + " "
                + shlex.quote(request.old_guid)
                + " "
                + shlex.quote(request.new_device)
                + " "
                + allow
            )
            result = ssh.run(command, timeout=150)
            status = ssh.run(
                f"zpool status -P -L {shlex.quote(request.pool)}",
                timeout=30,
            )

    invalidate_inventory_cache(server.id)
    return {
        "ok": result["exit"] == 0,
        "exit": result["exit"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "command": command,
        "pool_status": status["stdout"] if status["exit"] == 0 else status["stderr"],
        "failed": failed,
        "candidate": candidate,
    }
