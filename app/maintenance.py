from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

from .collector import CollectorError, SSHCollector
from .models import Server
from .parser import parse_pool_status

FAILED_STATES = {"DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"}


@dataclass(frozen=True)
class ReplacementRequest:
    pool: str
    old_device: str
    new_device: str


def _node_has_usage(node: dict[str, Any]) -> bool:
    if node.get("fstype"):
        return True
    if any(x for x in (node.get("mountpoints") or []) if x):
        return True
    # Be deliberately conservative: a disk with partitions or mapped children
    # is not offered as a replacement candidate even if those children are not mounted.
    if node.get("children"):
        return True
    return False


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


def _best_stable_path(paths: list[str], fallback: str) -> str:
    if not paths:
        return fallback
    preferred = sorted(
        paths,
        key=lambda p: (
            0 if "/wwn-" in p else 1 if "/nvme-" in p else 2 if "/ata-" in p else 3,
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


def _failed_leaf_vdevs(pool: str, status_text: str) -> list[dict[str, Any]]:
    status = parse_pool_status(status_text)
    entries = status.get("vdevs", [])
    failed: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        next_indent = entries[index + 1].get("indent", -1) if index + 1 < len(entries) else -1
        has_child = next_indent > entry.get("indent", 0)
        name = str(entry.get("name", ""))
        if has_child or name == pool:
            continue
        if str(entry.get("state", "")).upper() not in FAILED_STATES:
            continue
        failed.append(
            {
                "pool": pool,
                "device": name,
                "state": entry.get("state", "UNKNOWN"),
                "role": entry.get("role", "data"),
                "read_errors": entry.get("read_errors", 0),
                "write_errors": entry.get("write_errors", 0),
                "checksum_errors": entry.get("checksum_errors", 0),
            }
        )
    return failed


def discover_replacement_options(server: Server) -> dict[str, Any]:
    with SSHCollector(server) as ssh:
        pool_result = ssh.run("zpool list -H -o name", timeout=20)
        if pool_result["exit"] != 0:
            raise CollectorError(
                "Unable to list ZFS pools: "
                + (pool_result["stderr"] or pool_result["stdout"]).strip()
            )

        pools = [line.strip() for line in pool_result["stdout"].splitlines() if line.strip()]
        failed: list[dict[str, Any]] = []
        used_vdev_paths: set[str] = set()
        statuses: dict[str, str] = {}
        for pool in pools:
            result = ssh.run(f"zpool status -P -L {shlex.quote(pool)}", timeout=20)
            if result["exit"] != 0:
                raise CollectorError(
                    f"Unable to read zpool status for {pool}: "
                    + (result["stderr"] or result["stdout"]).strip()
                )
            statuses[pool] = result["stdout"]
            parsed = parse_pool_status(result["stdout"])
            for entry in parsed.get("vdevs", []):
                name = str(entry.get("name", ""))
                if name.startswith("/dev/"):
                    used_vdev_paths.add(name)
            failed.extend(_failed_leaf_vdevs(pool, result["stdout"]))

        lsblk_result = ssh.run(
            "lsblk -J -b -o NAME,KNAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,FSTYPE,MOUNTPOINTS",
            timeout=20,
        )
        if lsblk_result["exit"] != 0:
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
  case "$(basename "$p")" in *-part*) continue ;; esac
  printf '%s\t%s\n' "$(readlink -f "$p")" "$p"
done""",
            timeout=20,
        )
        stable_map = _stable_id_map(by_id_result["stdout"]) if by_id_result["exit"] == 0 else {}

        candidates: list[dict[str, Any]] = []
        for disk in _whole_disks(block.get("blockdevices", [])):
            path = str(disk.get("path") or "")
            if not path or _node_has_usage(disk):
                continue
            if any(_is_path_on_disk(vdev, path) for vdev in used_vdev_paths):
                continue
            stable_path = _best_stable_path(stable_map.get(path, []), path)
            candidates.append(
                {
                    "path": path,
                    "device": stable_path,
                    "size_bytes": int(disk.get("size") or 0),
                    "rotational": bool(disk.get("rota")),
                    "transport": disk.get("tran") or "",
                    "model": (disk.get("model") or "").strip(),
                    "serial": (disk.get("serial") or "").strip(),
                }
            )

        candidates.sort(key=lambda d: (d["size_bytes"], d["device"]))
        return {
            "failed": failed,
            "candidates": candidates,
            "statuses": statuses,
        }


def validate_replacement_choice(
    inventory: dict[str, Any], request: ReplacementRequest
) -> tuple[dict[str, Any], dict[str, Any]]:
    failed = next(
        (
            item
            for item in inventory.get("failed", [])
            if item.get("pool") == request.pool and item.get("device") == request.old_device
        ),
        None,
    )
    if failed is None:
        raise ValueError(
            "The failed/offline device is no longer an eligible replacement target. Refresh the page."
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
            "The selected replacement disk is no longer blank and unallocated, or is no longer present."
        )
    return failed, candidate


def replace_drive(server: Server, request: ReplacementRequest) -> dict[str, Any]:
    # Re-discover immediately before mutation to prevent stale UI state from
    # authorizing a disk that became mounted, partitioned, or assigned meanwhile.
    inventory = discover_replacement_options(server)
    failed, candidate = validate_replacement_choice(inventory, request)

    command = (
        "sudo -n zpool replace "
        + shlex.quote(request.pool)
        + " "
        + shlex.quote(request.old_device)
        + " "
        + shlex.quote(request.new_device)
    )
    with SSHCollector(server) as ssh:
        result = ssh.run(command, timeout=120)
        status = ssh.run(f"zpool status -P -L {shlex.quote(request.pool)}", timeout=30)

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
