#!/usr/bin/env bash
set -Eeuo pipefail

PROGRAM="NASitron remote NAS installer"
DEFAULT_USER="nasitron"
DEFAULT_HELPER="/usr/local/sbin/nasitron-root-helper"
NASITRON_USER="${NASITRON_REMOTE_USER:-$DEFAULT_USER}"
HELPER_PATH="${NASITRON_REMOTE_HELPER_PATH:-$DEFAULT_HELPER}"
PUBLIC_KEY="${NASITRON_SSH_PUBLIC_KEY:-}"
PUBLIC_KEY_FILE=""
GENERATED_PRIVATE_KEY=""
GENERATED_KEY=0
ENROLL_URL="${NASITRON_ENROLL_URL:-}"
ENROLL_SECRET="${NASITRON_ENROLL_SECRET:-}"
ENROLL_INSECURE=0
ENROLLMENT_MANAGED_KEY=0
SKIP_PACKAGES=0
SKIP_SSH_HARDENING=0
QUIET=0
TMP_DIR=""

log() {
  if [[ "$QUIET" -eq 0 ]]; then
    printf '[NASitron] %s\n' "$*"
  fi
}

die() {
  printf '[NASitron] ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "${TMP_DIR:-}" && -d "$TMP_DIR" ]]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

on_error() {
  local rc=$?
  local line="${BASH_LINENO[0]:-${LINENO:-unknown}}"
  local command="${BASH_COMMAND:-unknown}"
  printf '[NASitron] ERROR: installer command failed at line %s (exit %s): %s\n' "$line" "$rc" "$command" >&2
  exit "$rc"
}
trap on_error ERR

usage() {
  cat <<'EOF'
NASitron remote NAS installer

Configures an Ubuntu/Debian OpenZFS NAS for agentless NASitron monitoring.

Usage:
  curl -fsSL <installer-url> | sudo bash
  sudo ./scripts/install-remote.sh
  sudo ./scripts/install-remote.sh --public-key-file /path/to/nasitron.pub
  sudo ./scripts/install-remote.sh --public-key 'ssh-ed25519 AAAA...'

Options:
  --public-key KEY          Use an existing public key instead of generating one.
  --public-key-file FILE    Read an existing public key from FILE.
  --user USER               Remote monitoring account (default: nasitron).
  --helper-path PATH        Root helper install path
                            (default: /usr/local/sbin/nasitron-root-helper).
  --enroll-url URL          NASitron one-time enrollment callback URL.
  --enroll-secret SECRET    One-time HMAC enrollment secret.
  --enroll-insecure         Allow callback TLS verification bypass. Intended only
                            for NASitron internal-CA enrollment commands.
  --skip-packages           Do not apt-install OpenSSH/ZFS/SMART dependencies.
  --skip-ssh-hardening      Do not install the per-user sshd hardening drop-in.
  --quiet                   Reduce installer output.
  -h, --help                Show this help.

Piped install:
  This installer is self-contained. If no public key is supplied, it generates a
  dedicated Ed25519 keypair, installs the public key, prints the private key once at
  completion for entry into NASitron, and removes the temporary key files.

Security:
  The installer uses SSH public-key authentication. It locks password login for the
  dedicated account and grants passwordless sudo only to NASitron's root helper.
  Re-running without an explicit public key replaces only a prior installer-generated
  NASitron key; unrelated authorized_keys entries are preserved.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --public-key)
      [[ $# -ge 2 ]] || die "--public-key requires a value"
      PUBLIC_KEY="$2"
      shift 2
      ;;
    --public-key-file)
      [[ $# -ge 2 ]] || die "--public-key-file requires a path"
      PUBLIC_KEY_FILE="$2"
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || die "--user requires a value"
      NASITRON_USER="$2"
      shift 2
      ;;
    --helper-path)
      [[ $# -ge 2 ]] || die "--helper-path requires a value"
      HELPER_PATH="$2"
      shift 2
      ;;
    --enroll-url)
      [[ $# -ge 2 ]] || die "--enroll-url requires a value"
      ENROLL_URL="$2"
      shift 2
      ;;
    --enroll-secret)
      [[ $# -ge 2 ]] || die "--enroll-secret requires a value"
      ENROLL_SECRET="$2"
      shift 2
      ;;
    --enroll-insecure)
      ENROLL_INSECURE=1
      shift
      ;;
    --skip-packages)
      SKIP_PACKAGES=1
      shift
      ;;
    --skip-ssh-hardening)
      SKIP_SSH_HARDENING=1
      shift
      ;;
    --quiet)
      QUIET=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1 (use --help)"
      ;;
  esac
done

[[ "$EUID" -eq 0 ]] || die "Run this installer as root (for example: sudo $0 ...)"
[[ "$NASITRON_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "Invalid monitoring username: $NASITRON_USER"
[[ "$HELPER_PATH" == /* && "$HELPER_PATH" != *[[:space:]]* ]] || die "--helper-path must be an absolute path without whitespace"
if [[ -n "$ENROLL_URL" || -n "$ENROLL_SECRET" ]]; then
  [[ -n "$ENROLL_URL" && -n "$ENROLL_SECRET" ]] || die "--enroll-url and --enroll-secret must be supplied together"
  [[ "$ENROLL_URL" == https://* || "$ENROLL_URL" == http://* ]] || die "--enroll-url must be HTTP(S)"
  [[ "$ENROLL_SECRET" =~ ^[A-Za-z0-9_-]{32,128}$ ]] || die "--enroll-secret is malformed"
fi

TMP_DIR="$(mktemp -d -t nasitron-install.XXXXXX)"

if [[ -n "$PUBLIC_KEY_FILE" ]]; then
  [[ -r "$PUBLIC_KEY_FILE" ]] || die "Cannot read public key file: $PUBLIC_KEY_FILE"
  PUBLIC_KEY="$(cat "$PUBLIC_KEY_FILE")"
fi

PUBLIC_KEY="$(printf '%s' "$PUBLIC_KEY" | tr -d '\r' | awk 'NF { print; exit }')"
[[ "$PUBLIC_KEY" != *$'\n'* ]] || die "Only one SSH public key may be installed per invocation"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
else
  die "Unable to identify the operating system"
fi

if [[ "$SKIP_PACKAGES" -eq 0 ]]; then
  command -v apt-get >/dev/null 2>&1 || die "This installer currently supports apt-based Ubuntu/Debian systems"
  log "Installing required packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends     openssh-server     sudo     python3     zfsutils-linux     smartmontools     util-linux     curl     ca-certificates
fi

for command in sshd ssh-keygen sudo visudo python3 zpool zfs smartctl lsblk; do
  command -v "$command" >/dev/null 2>&1 || die "Required command is missing after setup: $command"
done

if [[ -z "$PUBLIC_KEY" ]]; then
  GENERATED_KEY=1
  GENERATED_KEY_PATH="$TMP_DIR/nasitron-monitoring"
  log "Generating a dedicated Ed25519 SSH keypair for NASitron"
  ssh-keygen -q -t ed25519 -N "" -C "nasitron-installer-generated" -f "$GENERATED_KEY_PATH"
  PUBLIC_KEY="$(cat "$GENERATED_KEY_PATH.pub")"
  GENERATED_PRIVATE_KEY="$(cat "$GENERATED_KEY_PATH")"
else
  KEY_CHECK="$TMP_DIR/nasitron.pub"
  printf '%s\n' "$PUBLIC_KEY" > "$KEY_CHECK"
  chmod 0600 "$KEY_CHECK"
  ssh-keygen -l -f "$KEY_CHECK" >/dev/null 2>&1 || die "The supplied SSH public key is not valid"
  if [[ "$PUBLIC_KEY" == *" nasitron-enrollment-"* ]]; then
    ENROLLMENT_MANAGED_KEY=1
  fi
fi

if ! getent passwd "$NASITRON_USER" >/dev/null; then
  log "Creating dedicated account: $NASITRON_USER"
  useradd --create-home --user-group --shell /bin/bash "$NASITRON_USER"
else
  log "Using existing account: $NASITRON_USER"
fi

USER_HOME="$(getent passwd "$NASITRON_USER" | cut -d: -f6)"
[[ -n "$USER_HOME" && "$USER_HOME" == /* ]] || die "Unable to determine home directory for $NASITRON_USER"
USER_GROUP="$(id -gn "$NASITRON_USER")"

# Lock local password authentication for the service account. SSH public-key access
# remains available and is explicitly enforced by the sshd drop-in below.
passwd -l "$NASITRON_USER" >/dev/null 2>&1 || true

SSH_DIR="$USER_HOME/.ssh"
AUTHORIZED_KEYS="$SSH_DIR/authorized_keys"
install -d -o "$NASITRON_USER" -g "$USER_GROUP" -m 0700 "$SSH_DIR"
touch "$AUTHORIZED_KEYS"
chown "$NASITRON_USER:$USER_GROUP" "$AUTHORIZED_KEYS"
chmod 0600 "$AUTHORIZED_KEYS"

if [[ "$GENERATED_KEY" -eq 1 || "$ENROLLMENT_MANAGED_KEY" -eq 1 ]]; then
  TMP_AUTHORIZED="$TMP_DIR/authorized_keys"
  grep -vE '[[:space:]](nasitron-installer-generated|nasitron-enrollment-[A-Za-z0-9_-]+)$' "$AUTHORIZED_KEYS" > "$TMP_AUTHORIZED" || true
  install -o "$NASITRON_USER" -g "$USER_GROUP" -m 0600 "$TMP_AUTHORIZED" "$AUTHORIZED_KEYS"
fi

if ! grep -Fqx -- "$PUBLIC_KEY" "$AUTHORIZED_KEYS"; then
  log "Adding NASitron SSH public key"
  printf '%s\n' "$PUBLIC_KEY" >> "$AUTHORIZED_KEYS"
else
  log "SSH public key is already authorized"
fi

if [[ "$SKIP_SSH_HARDENING" -eq 0 ]]; then
  SSHD_DIR="/etc/ssh/sshd_config.d"
  SSHD_DROPIN="$SSHD_DIR/99-nasitron.conf"
  install -d -o root -g root -m 0755 "$SSHD_DIR"

  OLD_DROPIN=""
  if [[ -f "$SSHD_DROPIN" ]]; then
    OLD_DROPIN="$TMP_DIR/99-nasitron.conf.old"
    cp -a "$SSHD_DROPIN" "$OLD_DROPIN"
  fi

  cat > "$TMP_DIR/99-nasitron.conf" <<EOF
# Managed by the NASitron remote installer.
Match User $NASITRON_USER
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowAgentForwarding no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
Match all
EOF
  install -o root -g root -m 0644 "$TMP_DIR/99-nasitron.conf" "$SSHD_DROPIN"

  if ! sshd -t; then
    if [[ -n "$OLD_DROPIN" ]]; then
      install -o root -g root -m 0644 "$OLD_DROPIN" "$SSHD_DROPIN"
    else
      rm -f "$SSHD_DROPIN"
    fi
    die "sshd rejected the NASitron hardening configuration; previous configuration was restored"
  fi
fi

log "Installing embedded NASitron root helper"
cat > "$TMP_DIR/nasitron-root-helper" <<'__NASITRON_ROOT_HELPER__'
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
                        "realpath": os.path.realpath(BY_ID_DIR / label) if label and not group and (BY_ID_DIR / label).is_symlink() else "",
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
            "capabilities": capabilities(),
            "actions": [{"id": key, "label": value[0], "warning": value[1]} for key, value in ACTION_SPECS.items() if key not in STORAGE_ACTIONS]}


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
    if action in STORAGE_ACTIONS:
        return _storage_plan(data)
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
    if action == "attach" and selected and selected["id"].startswith("raidz"):
        feature = _run([ZPOOL(), "get", "-H", "-o", "value", "feature@raidz_expansion", pool], timeout=15)
        if feature.returncode or feature.stdout.strip() not in {"enabled", "active"}:
            raise HelperError("RAIDZ expansion is not supported and enabled for this pool")
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
        return _forward(_execute_plan(plan))


# NASitron 1.0 storage protocol. This remains embedded in the standalone helper.
HELPER_VERSION = "1.0.1"
DATASET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*$")
SNAP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,200}$")
DATASET_PROPS = {'compression', 'recordsize', 'quota', 'refquota', 'reservation', 'refreservation', 'atime', 'readonly', 'sync', 'mountpoint'}
STORAGE_ACTIONS = {
    'dataset-mount': ('Mount filesystem', 'Mounts the filesystem at its configured mountpoint.'),
    'dataset-unmount': ('Unmount filesystem', 'Unmounts the filesystem; busy filesystems are not forced.'),
    'dataset-create': ('Create filesystem', 'Creates a child filesystem.'),
    'zvol-create': ('Create zvol', 'Creates a volume with the specified capacity; space is reserved by default.'),
    'dataset-set': ('Set dataset property', 'Changes this property locally; children may inherit it. Record size and compression affect new writes.'),
    'dataset-inherit': ('Inherit dataset property', 'Removes the local override and uses the inherited property.'),
    'snapshot-create': ('Create snapshot', 'Creates a point-in-time snapshot of one dataset.'),
    'snapshot-destroy': ('Delete snapshot', 'Permanently deletes this recovery point. Holds and clones block deletion.'),
    'snapshot-hold': ('Hold snapshot', 'Protects this snapshot from deletion with a NASitron hold.'),
    'snapshot-release': ('Release snapshot hold', 'Removes the NASitron hold; other holds are preserved.'),
    'snapshot-clone': ('Clone for recovery', 'Creates an unmounted clone for inspection; the original dataset is unchanged.'),
    'snapshot-rollback': ('Rollback dataset', 'Discards all changes since this snapshot. Newer snapshots are never automatically deleted.'),
    'smart-short': ('Start short SMART test', 'Starts a self-test on this physical disk.'),
    'smart-long': ('Start extended SMART test', 'Starts a potentially lengthy self-test on this physical disk.'),
    'helper-update': ('Update remote helper', 'Installs the reviewed helper from StealthCat/NASitron main, retaining a root-owned rollback copy.'),
    'helper-rollback': ('Roll back remote helper', 'Restores the previous root-owned helper. Older helpers may not support current features.'),
}
ACTION_SPECS.update(STORAGE_ACTIONS)


def ZFS():
    return _tool('zfs', ['/usr/sbin/zfs', '/usr/bin/zfs'])


def _dataset(name, snapshot=False):
    if not isinstance(name, str) or len(name) > 255:
        raise HelperError('Invalid dataset name')
    fields = name.split('@')
    if len(fields) != (2 if snapshot else 1) or not DATASET_RE.fullmatch(fields[0]):
        raise HelperError('Choose an exact dataset name')
    if snapshot and not SNAP_RE.fullmatch(fields[1]):
        raise HelperError('Invalid snapshot name')
    return name


def _zfs(args):
    result = _run([ZFS()] + args, timeout=120)
    if result.returncode:
        if args[0] == 'list' and 'no datasets available' in (result.stdout + result.stderr).lower():
            return ''
        raise HelperError(result.stderr.strip() or 'ZFS command failed')
    return result.stdout


def _get_property(name, prop):
    return _zfs(['get', '-Hp', '-o', 'value', prop, name]).strip()


def storage_inventory():
    columns = ['name', 'type', 'used', 'available', 'referenced', 'usedbysnapshots', 'usedbydataset', 'usedbychildren', 'usedbyrefreservation', 'quota', 'refquota', 'reservation', 'refreservation', 'mountpoint', 'guid']
    datasets = []
    for line in _zfs(['list', '-Hp', '-t', 'filesystem,volume', '-o', ','.join(columns)]).splitlines():
        values = line.split('\t')
        if len(values) == len(columns):
            datasets.append(dict(zip(columns, values)))
    props = []
    if datasets:
        for line in _zfs(['get', '-Hp', '-t', 'filesystem,volume', '-o', 'name,property,value,source', ','.join(sorted(DATASET_PROPS))]).splitlines():
            values = line.split('\t')
            if len(values) == 4:
                props.append(dict(zip(['name', 'property', 'value', 'source'], values)))
    snaps = []
    for line in _zfs(['list', '-Hp', '-t', 'snapshot', '-o', 'name,creation,used,referenced,userrefs,clones,guid', '-s', 'creation']).splitlines():
        values = line.split('\t')
        if len(values) == 7:
            snaps.append(dict(zip(['name', 'creation', 'used', 'referenced', 'holds', 'clones', 'guid'], values)))
    events = _run([ZPOOL(), 'events', '-v'], timeout=20)
    return {'protocol': 2, 'helper_version': HELPER_VERSION, 'datasets': datasets, 'properties': props,
            'snapshots': snaps, 'events': events.stdout[-200000:], 'capabilities': capabilities(),
            'actions': [{'id': k, 'label': v[0], 'warning': v[1]} for k, v in STORAGE_ACTIONS.items()]}


def capabilities():
    version = _run([ZPOOL(), '--version'], timeout=10)
    commands = {}
    for command in ['attach', 'remove', 'trim', 'initialize', 'checkpoint', 'resilver', 'split']:
        result = _run([ZPOOL(), command, '-?'], timeout=10)
        text = result.stdout + result.stderr
        commands[command] = bool(re.search(r'usage:.*', text, re.I)) and 'unrecognized command' not in text.lower()
    features = _run([ZPOOL(), 'get', '-H', '-o', 'name,property,value', 'feature@raidz_expansion'], timeout=10)
    return {'version': version.stdout.strip() or version.stderr.strip(), 'commands': commands,
            'raidz_expansion': [line.split('\t') for line in features.stdout.splitlines() if len(line.split('\t')) == 3]}


def _property_value(prop, value):
    if prop not in DATASET_PROPS:
        raise HelperError('Unsupported dataset property')
    if prop in {'quota', 'refquota', 'reservation', 'refreservation'}:
        valid = value == 'none' or bool(re.fullmatch(r'[0-9]+(?:[KMGTPE]i?B?)?', value, re.I))
    elif prop == 'recordsize':
        valid = value in {'4K', '8K', '16K', '32K', '64K', '128K', '256K', '512K', '1M'}
    elif prop == 'compression':
        valid = value in {'on', 'off', 'lz4', 'zstd', 'gzip', 'lzjb', 'zle'} or bool(re.fullmatch(r'(gzip-[1-9]|zstd-(?:[1-9]|1[0-9]))', value))
    elif prop in {'atime', 'readonly'}:
        valid = value in {'on', 'off'}
    elif prop == 'sync':
        valid = value in {'standard', 'always'}
    else:
        valid = value in {'none', 'legacy'} or bool(re.fullmatch(r'/(?:mnt|srv)/[A-Za-z0-9_./-]+', value)) and '..' not in value.split('/')
    if not valid:
        raise HelperError('Unsupported property value')
    return f'{prop}={value}'


def _release_candidate():
    import hashlib
    import urllib.request
    def download(url, limit):
        req = urllib.request.Request(url, headers={'User-Agent': 'NASitron-helper/1.0'})
        with urllib.request.urlopen(req, timeout=20) as response:
            content = response.read(limit + 1)
        if len(content) > limit:
            raise HelperError('Release exceeds size limit')
        return content
    metadata = json.loads(download('https://api.github.com/repos/StealthCat/NASitron/commits/main', 2000000))
    commit = metadata.get('sha', '')
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise HelperError('Invalid release commit')
    content = download(f'https://raw.githubusercontent.com/StealthCat/NASitron/{commit}/remote/nasitron_root_helper.py', 1000000)
    compile(content, '<NASitron release>', 'exec')
    return content, {'commit': commit, 'sha256': hashlib.sha256(content).hexdigest()}


def _helper_backup():
    path = Path(__file__).resolve().with_suffix('.previous')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise HelperError('Rollback file must be a root-owned regular file without group/other write permission')
    return path


def _storage_plan(data):
    import hashlib
    import shlex
    action = data['action']
    target = data.get('target', '')
    identity = {}
    kind = 'zfs'
    noop = False
    if action.startswith('helper-'):
        kind = 'helper'
        if action == 'helper-update':
            _content, identity = _release_candidate()
        else:
            identity = {'sha256': hashlib.sha256(_helper_backup().read_bytes()).hexdigest()}
        args = [action]
    elif action.startswith('smart-'):
        kind = 'smart'
        _by_id(target)
        _path, real = _device(str(BY_ID_DIR / target), require_by_id=True)
        if re.fullmatch(r'zd\d+', Path(real).name):
            raise HelperError('Virtual volumes cannot run physical SMART tests')
        identity = {'real': real, 'disk': _lsblk_disk(real)}
        if identity['disk'].get('type') != 'disk':
            raise HelperError('SMART tests require a whole physical disk')
        for pool in _pool_names():
            if 'in progress' in _scan_in_progress(pool).lower():
                raise HelperError('Wait for active pool scans before running disk tests')
        status = _run([SMARTCTL(), '-c', '-j', real], timeout=20)
        if 'in progress' in status.stdout.lower():
            raise HelperError('A SMART test is already in progress')
        args = ['-t', action.split('-')[1], target]
    else:
        snap = action.startswith('snapshot-')
        _dataset(target, snapshot=snap)
        parent = target.split('@')[0]
        if parent.split('/')[0] != data['pool']:
            raise HelperError('Dataset must belong to the selected pool')
        _validate_pool(data['pool'])
        if action in {'dataset-create', 'zvol-create'}:
            if '/' not in target:
                raise HelperError('Create a child dataset, not a pool root')
            identity['parent'] = _get_property(target.rsplit('/', 1)[0], 'guid')
            if action == 'zvol-create':
                size = data.get('value', '')
                if not re.fullmatch(r'[1-9][0-9]*(?:[KMGT]i?B?)?', size, re.I):
                    raise HelperError('Specify a positive zvol size, for example 100G')
                args = ['create', '-V', size, target]
            else:
                args = ['create', target]
        else:
            identity['guid'] = _get_property(parent if action == 'snapshot-create' else target, 'guid')
            if action == 'dataset-set':
                prop = data.get('property', '')
                args = ['set', _property_value(prop, data.get('value', '')), target]
                identity['old_value'] = _get_property(target, prop)
            elif action == 'dataset-inherit':
                prop = data.get('property', '')
                if prop not in DATASET_PROPS:
                    raise HelperError('Unsupported inherited property')
                args = ['inherit', prop, target]
            elif action == 'snapshot-create':
                args = ['snapshot', target]
            elif action == 'snapshot-destroy':
                args = ['destroy', target]
            elif action in {'snapshot-hold', 'snapshot-release'}:
                tag = data.get('value') or 'nasitron'
                if tag != 'nasitron' and not re.fullmatch(r'nasitron-[a-f0-9]{32}', tag):
                    raise HelperError('Invalid NASitron hold tag')
                holds = _zfs(['holds', '-H', target])
                present = any(len(row.split('\t')) >= 2 and row.split('\t')[1] == tag for row in holds.splitlines())
                noop = present if action == 'snapshot-hold' else not present
                identity['tag_present'] = present
                args = [action.split('-')[1], tag, target]
            elif action in {'dataset-mount', 'dataset-unmount'}:
                args = [action.split('-')[1], target]
            elif action == 'snapshot-clone':
                destination = _dataset(data.get('new_pool', ''))
                if destination.split('/')[0] != data['pool'] or '/' not in destination:
                    raise HelperError('Clone must be a new child dataset in the source pool')
                args = ['clone', '-o', 'readonly=on', target, destination] if _get_property(parent, 'type') == 'volume' else ['clone', '-o', 'canmount=noauto', '-o', 'mountpoint=none', target, destination]
            else:
                args = ['rollback', target]
    signature = json.dumps({'request': data, 'identity': identity}, sort_keys=True)
    return {'request': data, 'kind': kind, 'args': args, 'identity': identity, 'noop': noop,
            'target_id': target, 'fingerprint': hashlib.sha256(signature.encode()).hexdigest(),
            'command': shlex.join(([{'zfs': 'zfs', 'smart': 'smartctl', 'helper': 'nasitron-root-helper'}[kind]]) + args),
            'confirmation': f'{action.upper()} {data["pool"]}', 'warning': STORAGE_ACTIONS[action][1],
            'dry_run': json.dumps(identity, indent=2) if kind == 'helper' else ''}


def _execute_plan(plan):
    if plan.get('kind') == 'zfs':
        if plan.get('noop'):
            return subprocess.CompletedProcess([], 0, 'Requested hold state already applied.\n', '')
        return _run([ZFS()] + plan['args'])
    if plan.get('kind') == 'smart':
        result = subprocess.run([SMARTCTL()] + plan['args'], cwd=str(BY_ID_DIR), env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        # SMART health bits do not mean the command failed; preserve reported health output.
        return subprocess.CompletedProcess(result.args, result.returncode & 7, result.stdout, result.stderr)
    if plan.get('kind') == 'helper':
        import hashlib
        import tempfile
        path = Path(__file__).resolve()
        if plan['request']['action'] == 'helper-update':
            content, identity = _release_candidate()
            if identity != plan['identity']:
                raise HelperError('Release changed after review; prepare a new preview')
        else:
            content = _helper_backup().read_bytes()
        if hashlib.sha256(content).hexdigest() != plan['identity']['sha256']:
            raise HelperError('Helper digest changed')
        previous = path.read_bytes()
        def atomic_write(dest, data):
            fd, temporary = tempfile.mkstemp(prefix='.nasitron-', dir=dest.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o755)
                os.chown(temporary, 0, 0)
                os.replace(temporary, dest)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        atomic_write(path.with_suffix('.previous'), previous)
        atomic_write(path, content)
        return subprocess.CompletedProcess([], 0, 'Verified helper installed. Previous helper retained for rollback.\n', '')
    return _run_action(plan['args'])


def cmd_storage(args):
    if args != ['inventory']:
        raise HelperError('Usage: storage inventory')
    print(json.dumps(storage_inventory()))
    return 0


def cmd_stream(args):
    with open(LOCK_PATH, 'a+', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _stream_locked(args)


def _stream_locked(args):
    # No shell, no force, no recursive send, no receive-side mount or overwrite.
    if not args or args[0] not in {'send', 'receive'}:
        raise HelperError('Invalid replication command')
    if args[0] == 'receive' and len(args) == 3:
        target, owner = _dataset(args[1]), args[2]
        if '/' not in target or not re.fullmatch(r'[a-f0-9]{32}', owner):
            raise HelperError('Replication requires a dedicated child dataset and policy ID')
        existing = _run([ZFS(), 'list', '-H', '-o', 'name', target])
        if existing.returncode and 'does not exist' not in existing.stderr.lower():
            raise HelperError(existing.stderr.strip() or 'Cannot verify replication destination')
        if existing.returncode == 0 and _get_property(target, 'org.nasitron:replication') != owner:
            raise HelperError('Destination is not owned by this replication policy')
        command = [ZFS(), 'receive', '-u', '-s', '-o', 'readonly=on', '-o', f'org.nasitron:replication={owner}', target]
    elif args[0] == 'send' and len(args) == 4:
        target, base, token = args[1:]
        _dataset(target, snapshot=True)
        if token:
            if len(token) > 16384 or not re.fullmatch(r'[A-Za-z0-9_-]+', token):
                raise HelperError('Invalid resume token')
            command = [ZFS(), 'send', '-t', token]
        else:
            command = [ZFS(), 'send', '-w']
            if base:
                _dataset(base, snapshot=True)
                if base.split('@')[0] != target.split('@')[0]:
                    raise HelperError('Incremental base must belong to the same dataset')
                command += ['-i', base]
            command.append(target)
    else:
        raise HelperError('Invalid stream arguments')
    return subprocess.call(command, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})


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
        "storage": cmd_storage,
        "stream": cmd_stream,
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
__NASITRON_ROOT_HELPER__

log "Validating embedded root helper"
python3 -m py_compile "$TMP_DIR/nasitron-root-helper"
log "Installing root helper at $HELPER_PATH"
install -d -o root -g root -m 0755 "$(dirname "$HELPER_PATH")"
install -o root -g root -m 0755 "$TMP_DIR/nasitron-root-helper" "$HELPER_PATH"

log "Installing restricted sudo policy"
SUDOERS_PATH="/etc/sudoers.d/nasitron"
cat > "$TMP_DIR/nasitron.sudoers" <<EOF
# Managed by the NASitron remote installer.
# The helper is the privilege boundary and validates every supported operation.
$NASITRON_USER ALL=(root) NOPASSWD: $HELPER_PATH
EOF
chmod 0440 "$TMP_DIR/nasitron.sudoers"
visudo -cf "$TMP_DIR/nasitron.sudoers" >/dev/null || die "Generated sudoers policy failed validation"
install -o root -g root -m 0440 "$TMP_DIR/nasitron.sudoers" "$SUDOERS_PATH"
visudo -cf "$SUDOERS_PATH" >/dev/null || die "Installed sudoers policy failed validation"

if command -v systemctl >/dev/null 2>&1; then
  log "Ensuring SSH service is running"
  systemctl enable --now ssh >/dev/null 2>&1 || true
  if [[ "$SKIP_SSH_HARDENING" -eq 0 ]]; then
    systemctl reload ssh >/dev/null 2>&1 || systemctl restart ssh >/dev/null 2>&1 || true
  fi
fi

log "Detecting SSH endpoint and host identity"
SSH_PORT="$(sshd -T 2>/dev/null | awk '$1 == "port" { print $2; exit }' || true)"
SSH_PORT="${SSH_PORT:-22}"
HOST_FQDN="$(hostname -f 2>/dev/null || hostname || true)"
HOST_FQDN="${HOST_FQDN:-$(hostname 2>/dev/null || printf 'nas-server')}"
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
HOST_IP="${HOST_IP:-unknown}"

FINGERPRINTS=""
for hostkey in /etc/ssh/ssh_host_*_key.pub; do
  [[ -r "$hostkey" ]] || continue
  fingerprint_line="$(ssh-keygen -lf "$hostkey" 2>/dev/null || true)"
  if [[ -n "$fingerprint_line" ]]; then
    FINGERPRINTS+="$fingerprint_line"$'\n'
  fi
done

HOST_KEY_FINGERPRINT=""
if [[ -r /etc/ssh/ssh_host_ed25519_key.pub ]]; then
  HOST_KEY_FINGERPRINT="$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 2>/dev/null | awk '{print $2}' || true)"
else
  first_host_key="$(find /etc/ssh -maxdepth 1 -type f -name 'ssh_host_*_key.pub' 2>/dev/null | sort | head -n1 || true)"
  if [[ -n "$first_host_key" ]]; then
    HOST_KEY_FINGERPRINT="$(ssh-keygen -lf "$first_host_key" -E sha256 2>/dev/null | awk '{print $2}' || true)"
  fi
fi

if [[ -n "$ENROLL_URL" ]]; then
  log "Registering this NAS with NASitron"
  ENROLL_PAYLOAD="$TMP_DIR/enrollment.json"
  python3 - "$HOST_FQDN" "$HOST_IP" "$SSH_PORT" "$NASITRON_USER" "$HOST_KEY_FINGERPRINT" > "$ENROLL_PAYLOAD" <<'PY'
import json
import sys

hostname, host, port, username, fingerprint = sys.argv[1:]
if not host or host == "unknown":
    host = hostname
print(json.dumps({
    "hostname": hostname,
    "host": host,
    "port": int(port),
    "username": username,
    "host_key_fingerprint": fingerprint,
}, separators=(",", ":"), sort_keys=True))
PY
  ENROLL_SIGNATURE="$(python3 - "$ENROLL_SECRET" "$ENROLL_PAYLOAD" <<'PY'
import hashlib
import hmac
import pathlib
import sys

secret, path = sys.argv[1:]
payload = pathlib.Path(path).read_bytes()
print(hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest())
PY
)"
  CURL_ENROLL=(curl -fsS --retry 4 --retry-delay 2 --retry-all-errors --connect-timeout 10 --max-time 30 -X POST
    -H "Content-Type: application/json"
    -H "X-NASitron-Enrollment-Signature: $ENROLL_SIGNATURE"
    --data-binary "@$ENROLL_PAYLOAD"
  )
  if [[ "$ENROLL_INSECURE" -eq 1 ]]; then
    CURL_ENROLL+=(-k)
  fi
  ENROLL_RESPONSE="$("${CURL_ENROLL[@]}" "$ENROLL_URL")" || die "NASitron enrollment callback failed. Verify this host can reach $ENROLL_URL"
  SERVER_ID="$(python3 - "$ENROLL_RESPONSE" <<'PY'
import json
import sys

try:
    payload = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
if payload.get("status") != "registered" or not payload.get("server_id"):
    raise SystemExit(1)
print(payload["server_id"])
PY
)" || die "NASitron enrollment response was invalid"
  log "NASitron registration complete (server ID $SERVER_ID)"
fi

log "Validating NASitron account permissions"
VALIDATION_WARNINGS=0
if ! runuser -u "$NASITRON_USER" -- zpool list -H -o name >/dev/null 2>&1; then
  printf '[NASitron] WARNING: %s cannot currently read the ZFS pool inventory; NASitron will report the collection error.\n' "$NASITRON_USER" >&2
  VALIDATION_WARNINGS=$((VALIDATION_WARNINGS + 1))
fi
if ! runuser -u "$NASITRON_USER" -- lsblk -J -b -o NAME,KNAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,FSTYPE,UUID,PTTYPE,PARTTYPE,MOUNTPOINTS >/dev/null 2>&1; then
  printf '[NASitron] WARNING: %s cannot currently read the block-device inventory; NASitron will report the collection error.\n' "$NASITRON_USER" >&2
  VALIDATION_WARNINGS=$((VALIDATION_WARNINGS + 1))
fi
if ! sudo -u "$NASITRON_USER" sudo -n "$HELPER_PATH" dmesg >/dev/null 2>&1; then
  printf '[NASitron] WARNING: diagnostic dmesg access validation failed; core enrollment remains valid and NASitron will show any diagnostic limitation.\n' >&2
  VALIDATION_WARNINGS=$((VALIDATION_WARNINGS + 1))
fi

cat <<EOF

NASitron remote NAS setup complete.

Connection details:
  Hostname:        $HOST_FQDN
  Detected IP:     $HOST_IP
  SSH port:        $SSH_PORT
  SSH username:    $NASITRON_USER
  Authentication:  key
  SMART via sudo:  enabled/recommended
  Root helper:     $HELPER_PATH

SSH host-key fingerprints:
$FINGERPRINTS
EOF

if [[ -n "$ENROLL_URL" ]]; then
  cat <<EOF

This NAS has been registered automatically with NASitron.
Refresh the Servers page if it does not appear immediately.
EOF
fi

if [[ "$GENERATED_KEY" -eq 1 ]]; then
  cat <<EOF

======================================================================
NASITRON GENERATED PRIVATE KEY
======================================================================
Copy the complete OpenSSH private key below into the Private key field
on NASitron's Add Server page. This installer does not retain the private
key after it exits, so save it before closing this terminal.

$GENERATED_PRIVATE_KEY
======================================================================
END NASITRON GENERATED PRIVATE KEY
======================================================================
EOF
fi
