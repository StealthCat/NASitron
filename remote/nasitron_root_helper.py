#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

FAILED_STATES = {"DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"}
LOCK_PATH = "/run/lock/nasitron-zpool-replace.lock"
POOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")


class HelperError(RuntimeError):
    pass


def _tool(name: str, candidates: list[str]) -> str:
    for path in candidates:
        if Path(path).is_file() and os.access(path, os.X_OK):
            return path
    raise HelperError(f"Required tool not found: {name}")


def ZPOOL() -> str:
    return _tool("zpool", ["/usr/sbin/zpool", "/usr/bin/zpool"])


def SMARTCTL() -> str:
    return _tool("smartctl", ["/usr/sbin/smartctl", "/usr/bin/smartctl"])


def WIPEFS() -> str:
    return _tool("wipefs", ["/usr/sbin/wipefs", "/usr/bin/wipefs"])


def LSBLK() -> str:
    return _tool("lsblk", ["/usr/bin/lsblk", "/bin/lsblk"])


def DMESG() -> str:
    return _tool("dmesg", ["/usr/bin/dmesg", "/bin/dmesg"])


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _forward(result: subprocess.CompletedProcess[str]) -> int:
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return int(result.returncode)


def _device(value: str, *, require_by_id: bool = False) -> tuple[str, str]:
    if not value.startswith("/dev/") or len(value) > 1024 or any(ord(ch) < 32 for ch in value):
        raise HelperError("Invalid block-device path")
    if require_by_id:
        if not value.startswith("/dev/disk/by-id/"):
            raise HelperError("Replacement device must use /dev/disk/by-id")
        if "-part" in Path(value).name:
            raise HelperError("Replacement device must be a whole-disk by-id path")
    real = os.path.realpath(value)
    if not real.startswith("/dev/"):
        raise HelperError("Block-device path resolves outside /dev")
    try:
        mode = os.stat(real).st_mode
    except OSError as exc:
        raise HelperError(f"Block device is not present: {value}") from exc
    if not stat.S_ISBLK(mode):
        raise HelperError(f"Not a block device: {value}")
    return value, real


def _pool_names() -> set[str]:
    result = _run([ZPOOL(), "list", "-H", "-o", "name"], timeout=20)
    if result.returncode != 0:
        raise HelperError(result.stderr.strip() or "Unable to list ZFS pools")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _validate_pool(pool: str) -> None:
    if not POOL_RE.fullmatch(pool):
        raise HelperError("Invalid pool name")
    if pool not in _pool_names():
        raise HelperError("Requested pool is not currently imported")


def _guid_is_failed(pool: str, guid: str) -> None:
    if not guid.isdigit() or len(guid) > 32:
        raise HelperError("Invalid ZFS vdev GUID")
    result = _run([ZPOOL(), "status", "-g", pool], timeout=30)
    if result.returncode != 0:
        raise HelperError(result.stderr.strip() or "Unable to read pool status by GUID")
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == guid:
            if parts[1].upper() not in FAILED_STATES:
                raise HelperError(f"Vdev {guid} is no longer failed/offline")
            return
    raise HelperError(f"Vdev GUID {guid} is no longer present in pool {pool}")


def _scan_in_progress(pool: str) -> str:
    result = _run([ZPOOL(), "status", pool], timeout=30)
    if result.returncode != 0:
        raise HelperError(result.stderr.strip() or "Unable to inspect pool scan state")
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("scan:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _lsblk_disk(real: str) -> dict[str, Any]:
    result = _run(
        [
            LSBLK(),
            "-J",
            "-b",
            "-o",
            "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,UUID,PTTYPE,PARTTYPE,MOUNTPOINTS",
            real,
        ],
        timeout=20,
    )
    if result.returncode != 0:
        raise HelperError(result.stderr.strip() or "Unable to inspect replacement disk")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HelperError("Unable to parse lsblk JSON") from exc
    devices = data.get("blockdevices") or []
    if len(devices) != 1 or not isinstance(devices[0], dict):
        raise HelperError("Replacement disk inventory is ambiguous")
    return devices[0]


def _validate_blank_disk(by_id: str) -> tuple[str, int]:
    _submitted, real = _device(by_id, require_by_id=True)
    node = _lsblk_disk(real)
    if node.get("type") != "disk":
        raise HelperError("Replacement target is not a whole disk")
    if node.get("children"):
        raise HelperError("Replacement disk has child partitions/devices")
    for key in ("fstype", "uuid", "pttype", "parttype"):
        if node.get(key):
            raise HelperError(f"Replacement disk has {key} metadata")
    if any(item for item in (node.get("mountpoints") or []) if item):
        raise HelperError("Replacement disk has active mountpoints")

    kname = str(node.get("kname") or node.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", kname):
        raise HelperError("Unexpected kernel block-device name")
    holders = Path("/sys/class/block") / kname / "holders"
    if holders.is_dir() and any(holders.iterdir()):
        raise HelperError("Replacement disk has active kernel holders")

    wipe = _run(
        [WIPEFS(), "--no-act", "--noheadings", "--output", "TYPE,UUID,LABEL", real],
        timeout=20,
    )
    if wipe.returncode != 0:
        raise HelperError(wipe.stderr.strip() or "wipefs could not inspect replacement disk")
    if wipe.stdout.strip():
        raise HelperError("Replacement disk contains filesystem/RAID/partition signatures")

    status = _run([ZPOOL(), "status", "-P", "-L"], timeout=30)
    if status.returncode != 0:
        raise HelperError(status.stderr.strip() or "Unable to verify ZFS membership")
    for token in status.stdout.split():
        if token == real or token.startswith(real + "p") or (
            token.startswith(real) and token[len(real):].isdigit()
        ):
            raise HelperError("Replacement disk is already a ZFS member")

    return real, int(node.get("size") or 0)


def _find_guid_size(obj: Any, guid: str) -> int:
    if isinstance(obj, dict):
        if str(obj.get("guid") or "") == guid:
            for key in ("rep_dev_size", "phys_space", "total_space"):
                try:
                    value = int(obj.get(key) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value:
                    return value
        for value in obj.values():
            found = _find_guid_size(value, guid)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_guid_size(value, guid)
            if found:
                return found
    return 0


def _required_vdev_size(pool: str, guid: str) -> int:
    result = _run([ZPOOL(), "status", "-j", "--json-int", "-P", "-L", pool], timeout=30)
    if result.returncode != 0:
        return 0
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return 0
    return _find_guid_size(payload, guid)


def cmd_smartctl(args: list[str]) -> int:
    if len(args) != 1:
        raise HelperError("Usage: smartctl <device>")
    _submitted, real = _device(args[0])
    return _forward(_run([SMARTCTL(), "-a", "-j", real], timeout=45))


def cmd_dmesg(args: list[str]) -> int:
    if args:
        raise HelperError("Usage: dmesg")
    return _forward(_run([DMESG()], timeout=30))


def cmd_wipefs_check(args: list[str]) -> int:
    if len(args) != 1:
        raise HelperError("Usage: wipefs-check <device>")
    _submitted, real = _device(args[0])
    return _forward(
        _run(
            [WIPEFS(), "--no-act", "--noheadings", "--output", "TYPE,UUID,LABEL", real],
            timeout=20,
        )
    )


def cmd_replace(args: list[str]) -> int:
    if len(args) != 4:
        raise HelperError(
            "Usage: replace <pool> <failed-guid> <replacement-by-id> <allow-conflict:0|1>"
        )
    pool, guid, replacement, allow_conflict = args
    if allow_conflict not in {"0", "1"}:
        raise HelperError("Conflict override must be 0 or 1")

    Path(LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HelperError("Another NASitron ZFS replacement is already running") from exc

        _validate_pool(pool)
        _guid_is_failed(pool, guid)
        scan = _scan_in_progress(pool)
        if "in progress" in scan.lower() and allow_conflict != "1":
            raise HelperError(f"A scrub/resilver is already in progress: {scan}")

        _real, candidate_size = _validate_blank_disk(replacement)
        required_size = _required_vdev_size(pool, guid)
        if required_size and candidate_size < required_size:
            raise HelperError(
                f"Replacement disk is too small ({candidate_size} < {required_size} bytes)"
            )

        return _forward(
            _run([ZPOOL(), "replace", pool, guid, replacement], timeout=120)
        )


def main() -> int:
    if os.geteuid() != 0:
        raise HelperError("nasitron-root-helper must run as root via sudo")
    if len(sys.argv) < 2:
        raise HelperError("Missing helper command")
    command, args = sys.argv[1], sys.argv[2:]
    handlers = {
        "smartctl": cmd_smartctl,
        "dmesg": cmd_dmesg,
        "wipefs-check": cmd_wipefs_check,
        "replace": cmd_replace,
    }
    handler = handlers.get(command)
    if handler is None:
        raise HelperError("Unknown helper command")
    return handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HelperError as exc:
        print(f"NASitron helper: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except subprocess.TimeoutExpired as exc:
        print(f"NASitron helper: command timed out: {exc}", file=sys.stderr)
        raise SystemExit(124)
