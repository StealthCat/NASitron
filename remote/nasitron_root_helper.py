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
BY_ID_DIR = Path("/dev/disk/by-id")
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
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "ZPOOL_IMPORT_PATH": str(BY_ID_DIR)},
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
        submitted = Path(value)
        if submitted.parent != BY_ID_DIR or submitted.name in {"", ".", ".."}:
            raise HelperError(
                "Replacement device must be a direct /dev/disk/by-id entry"
            )
        if "-part" in submitted.name:
            raise HelperError("Replacement device must be a whole-disk by-id path")
        if not submitted.is_symlink():
            raise HelperError("Replacement device must be an existing /dev/disk/by-id symlink")
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
        if "no pools available" in (result.stderr + result.stdout).lower():
            return set()
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
            "NAME,KNAME,PATH,TYPE,SIZE,SERIAL,WWN,FSTYPE,UUID,PTTYPE,PARTTYPE,MOUNTPOINTS",
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
    if re.fullmatch(r"zd\d+(?:p\d+)?", Path(real).name):
        raise HelperError("ZFS virtual volumes are not physical disk candidates")
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
    if status.returncode != 0 and "no pools available" not in (status.stdout + status.stderr).lower():
        raise HelperError(status.stderr.strip() or "Unable to verify ZFS membership")
    for token in status.stdout.split():
        if token == real or token.startswith(real + "p") or (
            token.startswith(real) and token[len(real):].isdigit()
        ):
            raise HelperError("Replacement disk is already a ZFS member")

    size = int(node.get("size") or 0)
    if size <= 0:
        raise HelperError("Cannot determine disk capacity")
    return real, size


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
    _submitted, real = _device(args[0], require_by_id=True)
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

        member = next((m for m in _pool_members(pool, _aliases()) if m["guid"] == guid), None)
        if not member or not member["id"] or member["group"]:
            raise HelperError("Failed member has no stable by-id name; use the CLI to investigate")
        return _forward(_run_action(["replace", pool, _by_id(member["id"]), _by_id(Path(replacement).name)]))


# Structured maintenance protocol v1. No caller-supplied executable or flags.
ACTION_SPECS = {
    "add": ("Add a vdev / cache / log / spare", "Adds storage to an existing pool. Adding a data vdev changes pool topology permanently."),
    "attach": ("Attach disk / expand RAIDZ", "Creates or extends a mirror, or expands a RAIDZ group on supported OpenZFS versions."),
    "replace": ("Replace a disk", "Replaces a healthy or failed member and starts reconstruction."),
    "detach": ("Detach mirror member", "Removes a mirror member and reduces redundancy."),
    "remove": ("Remove device / top-level vdev", "Removes cache, spare or supported vdevs. Data evacuation may continue in the background."),
    "remove-cancel": ("Cancel vdev removal", "Stops an in-progress top-level vdev evacuation."),
    "offline": ("Offline a disk", "Makes a member unavailable until brought online; reduces redundancy."),
    "offline-temporary": ("Offline until reboot", "Temporarily offlines a member until the next import/reboot."),
    "online": ("Online a disk", "Brings a member online."),
    "expand": ("Expand disk capacity", "Brings a member online and expands its usable capacity (online -e)."),
    "clear": ("Clear error counters", "Clears recorded errors for the selected member or entire pool."),
    "scrub": ("Start / resume scrub", "Verifies pool data checksums and repairs recoverable errors."),
    "scrub-pause": ("Pause scrub", "Pauses the current scrub where supported."),
    "scrub-stop": ("Stop scrub", "Cancels the current scrub."),
    "trim": ("Start / resume TRIM", "Discards unused blocks on supporting devices."),
    "trim-pause": ("Pause TRIM", "Pauses TRIM."),
    "trim-stop": ("Stop TRIM", "Cancels TRIM."),
    "initialize": ("Initialize free space", "Writes to unallocated pool space; this is not disk formatting."),
    "initialize-pause": ("Pause initialization", "Pauses free-space initialization."),
    "initialize-stop": ("Stop initialization", "Cancels free-space initialization."),
    "resilver": ("Restart resilver", "Restarts deferred resilvering where supported."),
    "upgrade": ("Enable supported pool features", "Irreversibly enables all pool features supported by the installed OpenZFS version; older hosts may no longer import this pool."),
    "reguid": ("Regenerate pool GUID", "Changes the pool GUID; external references to the old GUID must be updated."),
    "reopen": ("Reopen pool devices", "Reopens all devices in the pool."),
    "sync": ("Sync pool", "Flushes dirty data to stable storage."),
    "checkpoint": ("Create checkpoint", "Pins a pool checkpoint and restricts subsequent topology changes."),
    "checkpoint-discard": ("Discard checkpoint", "Permanently removes the saved pool checkpoint."),
    "export": ("Export pool", "Unmounts datasets and makes the pool unavailable on this host."),
    "import": ("Import pool", "Imports an exported pool discovered through by-id paths, without force or recovery flags."),
    "create": ("Create pool", "Creates a new pool on selected blank disks."),
    "destroy": ("Destroy pool", "DESTROYS the pool and access to all of its datasets and data."),
    "split": ("Split mirrored pool", "Splits selected mirror members into a new exported pool and reduces source redundancy."),
    "set": ("Set pool property", "Changes a pool property from the supported settings list."),
}
TARGET_REQUIRED = {"attach", "replace", "detach", "remove", "offline", "offline-temporary", "online", "expand"}
TARGET_OPTIONAL = {"clear", "trim", "trim-pause", "trim-stop", "initialize", "initialize-pause", "initialize-stop"}
NEW_DISKS = {"create", "add", "attach", "replace"}
TOPOLOGY_ACTIONS = NEW_DISKS | {"detach", "remove", "offline", "offline-temporary", "split", "export", "destroy"}
PROPERTY_VALUES = {"autoexpand": {"on", "off"}, "autotrim": {"on", "off"},
                   "autoreplace": {"on", "off"}, "failmode": {"wait", "continue", "panic"},
                   "delegation": {"on", "off"}, "listsnapshots": {"on", "off"}}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,254}$")


def _by_id(value: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value) or value.isdigit():
        raise HelperError("Choose a by-id identifier without /dev/disk/by-id/ or any other path")
    return value


def _aliases() -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    if not BY_ID_DIR.is_dir():
        return aliases
    for link in BY_ID_DIR.iterdir():
        if link.is_symlink() and ID_RE.fullmatch(link.name):
            aliases.setdefault(os.path.realpath(link), []).append(link.name)
    for names in aliases.values():
        names.sort(key=lambda n: (not n.startswith("scsi-"), not n.startswith("wwn-"), n))
    return aliases


def _config_rows(text: str) -> list[dict[str, str]]:
    rows = []
    active = False
    role = "data"
    for line in text.splitlines():
        if line.strip() == "config:":
            active = True
            continue
        if line.strip().startswith("errors:"):
            break
        if active and line.strip() in {"logs", "cache", "spares", "special", "dedup"}:
            role = line.strip()
        parts = line.split()
        if active and len(parts) >= 2 and parts[1] in {"ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED", "AVAIL", "INUSE", "SUSPENDED"}:
            rows.append({"name": parts[0], "state": parts[1], "role": role,
                         "depth": len(line.expandtabs()) - len(line.expandtabs().lstrip()),
                         "was": line.split(" was ", 1)[1].strip() if " was " in line else ""})
    return rows


def _pool_members(pool: str, aliases: dict) -> list[dict]:
    named = _run([ZPOOL(), "status", "-P", pool], timeout=30)
    guids = _run([ZPOOL(), "status", "-g", pool], timeout=30)
    if named.returncode or guids.returncode:
        raise HelperError("Cannot read pool topology")
    rows, ids = _config_rows(named.stdout), _config_rows(guids.stdout)
    if len(rows) != len(ids) or not rows:
        raise HelperError("Pool topology changed or could not be parsed; refresh")
    # Two consecutive stable snapshots prevent pairing names with a changed GUID order.
    named_again = _run([ZPOOL(), "status", "-P", pool], timeout=30)
    guids_again = _run([ZPOOL(), "status", "-g", pool], timeout=30)
    if named_again.returncode or guids_again.returncode or _config_rows(named_again.stdout) != rows or _config_rows(guids_again.stdout) != ids:
        raise HelperError("Pool topology changed; refresh")
    members = []
    ancestors = []
    for row, guid in zip(rows[1:], ids[1:]):
        if row["state"] != guid["state"] or row["depth"] != guid["depth"] or row["role"] != guid["role"] or not guid["name"].isdigit():
            raise HelperError("Pool topology changed; refresh")
        name = row["was"] if row["name"].isdigit() and row["was"].startswith(str(BY_ID_DIR) + "/") else row["name"]
        group = bool(re.fullmatch(r"(?:mirror|raidz[123]?|draid[123](?::[0-9]+[dcs])+|replacing|spare)-[0-9]+", name))
        if Path(name).parent == BY_ID_DIR:
            label = Path(name).name
        elif not name.startswith("/") and ID_RE.fullmatch(name) and (BY_ID_DIR / name).is_symlink():
            label = name
        elif name.startswith("/dev/"):
            label = next(iter(aliases.get(os.path.realpath(name), [])), "")
        else:
            label = name if group else ""
        while ancestors and (ancestors[-1]["depth"] >= row["depth"] or ancestors[-1]["role"] != row["role"]):
            ancestors.pop()
        parent_guid = ancestors[-1]["guid"] if ancestors else ""
        top_level_guid = ancestors[0]["guid"] if ancestors else guid["name"]
        members.append({"parent_guid": parent_guid, "top_level_guid": top_level_guid, "guid": guid["name"], "id": label, "state": row["state"], "group": group,
                        "depth": row["depth"], "role": row["role"], "display": label or f"Unresolved member (GUID {guid['name']})"})
        if group:
            ancestors.append(members[-1])
    return members


def action_inventory() -> dict:
    aliases = _aliases()
    pools = [{"name": pool, "members": _pool_members(pool, aliases)} for pool in sorted(_pool_names())]
    disks = []
    for real, names in aliases.items():
        whole = [n for n in names if not re.search(r"-part\d+$", n)]
        if not whole:
            continue
        name = whole[0]
        try:
            _real, size = _validate_blank_disk(str(BY_ID_DIR / name))
            disks.append({"id": name, "size": size, "aliases": whole})
        except (HelperError, OSError):
            continue
    return {"protocol": 1, "pools": pools, "disks": disks, "importable": _importable_pools(),
            "actions": [{"id": key, "label": value[0], "warning": value[1]} for key, value in ACTION_SPECS.items()]}


def _importable_pools() -> list[dict]:
    imported = _run([ZPOOL(), "import", "-d", str(BY_ID_DIR)], timeout=30)
    if imported.returncode and "no pools available" not in (imported.stdout + imported.stderr).lower():
        raise HelperError(imported.stderr.strip() or "Unable to discover exported pools")
    importable = []
    for block in re.split(r"(?m)(?=^[ \t]*pool:)", imported.stdout):
        name = re.search(r"(?m)^[ \t]*pool:[ \t]*(.+)$", block)
        guid = re.search(r"(?m)^[ \t]*id:[ \t]*([0-9]+)[ \t]*$", block)
        if name and guid:
            importable.append({"name": name.group(1).strip(), "guid": guid.group(1),
                               "members": _config_rows(block)})
    return importable


def _action_request(raw: str) -> dict:
    if len(raw) > 16384:
        raise HelperError("Action request is too large")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HelperError("Invalid action request") from exc
    fields = {"action", "pool", "target", "disks", "layout", "role", "new_pool", "property", "value", "ashift"}
    if not isinstance(data, dict) or set(data) - fields:
        raise HelperError("Unknown action fields")
    for key, value in data.items():
        if key == "disks":
            if not isinstance(value, list) or len(value) > 64 or any(not isinstance(v, str) for v in value):
                raise HelperError("Choose at most 64 disks")
        elif not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 for c in value):
            raise HelperError("Invalid action field")
    if data.get("action") not in ACTION_SPECS:
        raise HelperError("Unsupported ZFS action")
    if not POOL_RE.fullmatch(data.get("pool", "")):
        raise HelperError("Invalid pool name or import GUID")
    return data


def action_plan(data: dict) -> dict:
    import hashlib
    import shlex
    action, pool = data["action"], data["pool"]
    aliases = _aliases()
    pools = _pool_names()
    members = []
    if action in {"create", "import"}:
        if pool in pools:
            raise HelperError("A pool with this name is already imported")
        if action == "create" and pool.isdigit():
            raise HelperError("A new pool name must not be numeric")
    else:
        _validate_pool(pool)
        members = _pool_members(pool, aliases)
    new_pool = data.get("new_pool", "")
    if action == "split" and (not POOL_RE.fullmatch(new_pool) or new_pool.isdigit() or new_pool in pools):
        raise HelperError("Choose a distinct unused pool name for the split")
    target = data.get("target", "")
    selected = next((m for m in members if m["guid"] == target), None)
    if target and action not in TARGET_REQUIRED | TARGET_OPTIONAL:
        raise HelperError("This action does not accept a target member")
    if action in TARGET_REQUIRED or target:
        if not selected or not selected["id"]:
            raise HelperError("Select a current member with a stable by-id identifier, or a vdev group")
        if not selected["group"]:
            _by_id(selected["id"])
        if selected["group"] and action not in {"attach", "remove"}:
            raise HelperError("This action requires a disk, not a vdev group")
    ids = data.get("disks", [])
    if ids and action not in NEW_DISKS | {"split"}:
        raise HelperError("This action does not accept new disks")
    if action in NEW_DISKS and not ids:
        raise HelperError("Choose at least one blank disk")
    if action in {"attach", "replace"} and len(ids) != 1:
        raise HelperError("Choose exactly one new disk")
    identities = []
    for name in ids:
        _by_id(name)
        if action == "split":
            if not any(m["id"] == name and not m["group"] for m in members):
                raise HelperError("Split disks must be existing members of this pool")
            identities.append(name)
        else:
            real, size = _validate_blank_disk(str(BY_ID_DIR / name))
            if real in [i[0] for i in identities]:
                raise HelperError("Two selected identifiers refer to the same disk")
            identity = _lsblk_disk(real)
            identities.append((real, size, os.stat(real).st_rdev, identity.get("serial"), identity.get("wwn")))
            if action in {"attach", "replace"}:
                required = _required_vdev_size(pool, target)
                if required and size < required:
                    raise HelperError("New disk is smaller than the selected member")
    if len(ids) != len(set(ids)):
        raise HelperError("Select each disk only once")
    if action == "split":
        # ZFS silently chooses a disk for every unspecified mirror. Require complete
        # explicit coverage so execution never detaches an unreviewed disk.
        roots = [m for m in members if not m.get("parent_guid") and m.get("role", "data") in {"data", "special", "dedup"}]
        if not roots or any(not re.fullmatch(r"mirror-[0-9]+", m["id"]) for m in roots):
            raise HelperError("Split requires mirrored data/special/dedup vdevs")
        selected_members = [m for m in members if m["id"] in ids and not m["group"]]
        root_ids = {m["guid"] for m in roots}
        if any(m.get("parent_guid") not in root_ids for m in selected_members):
            raise HelperError("Select direct members of each top-level mirror")
        if any(sum(m.get("parent_guid") == root["guid"] for m in selected_members) != 1 for root in roots):
            raise HelperError("Select exactly one disk from every data/special/dedup mirror")
    if action in TOPOLOGY_ACTIONS and action != "create":
        scan = _scan_in_progress(pool)
        if "in progress" in scan.lower():
            raise HelperError("Wait for the active scrub/resilver to finish before changing pool topology")
    args = []
    member_id = selected["id"] if selected else ""
    if action in {"create", "add"}:
        layout = data.get("layout", "mirror")
        role = data.get("role", "data")
        minimum = {"stripe": 1, "mirror": 2, "raidz1": 2, "raidz2": 3, "raidz3": 4}
        if layout not in minimum or len(ids) < minimum[layout]:
            raise HelperError("Insufficient disks or unsupported vdev layout")
        if role not in {"data", "log", "cache", "spare", "special", "dedup"}:
            raise HelperError("Unsupported vdev role")
        if action == "create" and role != "data":
            raise HelperError("New pools require a data vdev")
        if role in {"cache", "spare"} and layout != "stripe":
            raise HelperError("Cache and spare devices must use the individual-disk layout")
        if role in {"log", "special", "dedup"} and layout not in {"stripe", "mirror"}:
            raise HelperError("This allocation class supports individual disks or mirrors")
        ashift = data.get("ashift", "12")
        if ashift not in {"9", "12", "13", "14", "15", "16"}:
            raise HelperError("Unsupported ashift")
        args = [action, "-o", f"ashift={ashift}", pool]
        if role != "data":
            args.append(role)
        if layout != "stripe":
            args.append(layout)
        args.extend(ids)
    elif action in {"attach", "replace"}:
        args = [action, pool, member_id, ids[0]]
    elif action in {"detach", "remove", "offline", "online"}:
        args = [action, pool, member_id]
    elif action in {"offline-temporary", "expand"}:
        args = ["offline", "-t", pool, member_id] if action == "offline-temporary" else ["online", "-e", pool, member_id]
    elif action == "clear":
        args = ["clear", pool] + ([member_id] if member_id else [])
    elif action.startswith(("scrub", "trim", "initialize")):
        base, _, mode = action.partition("-")
        flags = {"": [], "pause": ["-p" if base == "scrub" else "-s"], "stop": ["-s" if base == "scrub" else "-c"]}
        args = [base] + flags[mode] + [pool] + ([member_id] if member_id else [])
    elif action == "remove-cancel":
        args = ["remove", "-s", pool]
    elif action == "checkpoint-discard":
        args = ["checkpoint", "-d", pool]
    elif action == "import":
        # Import by numeric ID avoids duplicate-name ambiguity; discovery stays by-id.
        if not pool.isdigit() or len(pool) > 32:
            raise HelperError("Choose an exported pool by its numeric import GUID")
        imported = next((p for p in _importable_pools() if p["guid"] == pool), None)
        if not imported:
            raise HelperError("Selected pool is no longer available to import")
        members = [imported]
        args = ["import", "-d", str(BY_ID_DIR), pool]
    elif action == "split":
        args = ["split", pool, new_pool] + ids
    elif action == "set":
        prop, value = data.get("property", ""), data.get("value", "")
        if value not in PROPERTY_VALUES.get(prop, set()):
            raise HelperError("Unsupported pool property or value")
        args = ["set", f"{prop}={value}", pool]
    else:
        args = [action, pool]
    # Native dry runs supplement (not replace) validation. Never simulate by mutation.
    dry_output = ""
    if action in {"create", "add", "remove", "split"}:
        dry = _run_action([args[0], "-n"] + args[1:])
        if dry.returncode:
            raise HelperError(dry.stderr.strip() or dry.stdout.strip() or "ZFS rejected the proposed action")
        dry_output = dry.stdout
    signature = json.dumps({"request": data, "members": members, "disks": identities, "pools": sorted(pools)}, sort_keys=True)
    return {"request": data, "args": args, "fingerprint": hashlib.sha256(signature.encode()).hexdigest(),
            "command": shlex.join(["zpool"] + args), "warning": ACTION_SPECS[action][1],
            "target_id": member_id, "dry_run": dry_output, "confirmation": f"{action.upper()} {pool}"}


def _run_action(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([ZPOOL()] + args, cwd=str(BY_ID_DIR),
                          env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "ZPOOL_IMPORT_PATH": str(BY_ID_DIR)},
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=120, check=False)


def cmd_actions(args: list[str]) -> int:
    if args == ["inventory"]:
        print(json.dumps(action_inventory()))
        return 0
    if len(args) not in {2, 3} or args[0] not in {"preview", "execute"}:
        raise HelperError("Usage: actions inventory | preview <json> | execute <json> <fingerprint>")
    data = _action_request(args[1])
    Path(LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HelperError("Another NASitron ZFS operation is running") from exc
        plan = action_plan(data)
        if args[0] == "preview":
            if len(args) != 2:
                raise HelperError("Unexpected preview arguments")
            print(json.dumps(plan))
            return 0
        if len(args) != 3 or args[2] != plan["fingerprint"]:
            raise HelperError("Pool topology or disk identity changed. Generate a new preview.")
        return _forward(_run_action(plan["args"]))


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
        "actions": cmd_actions,
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
