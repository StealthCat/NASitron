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
__NASITRON_ROOT_HELPER__

python3 -m py_compile "$TMP_DIR/nasitron-root-helper"
install -d -o root -g root -m 0755 "$(dirname "$HELPER_PATH")"
install -o root -g root -m 0755 "$TMP_DIR/nasitron-root-helper" "$HELPER_PATH"

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
  systemctl enable --now ssh >/dev/null 2>&1 || true
  if [[ "$SKIP_SSH_HARDENING" -eq 0 ]]; then
    systemctl reload ssh >/dev/null 2>&1 || systemctl restart ssh >/dev/null 2>&1 || true
  fi
fi

log "Validating NASitron account permissions"
runuser -u "$NASITRON_USER" -- zpool list -H -o name >/dev/null 2>&1 ||   die "$NASITRON_USER cannot read the ZFS pool inventory"
runuser -u "$NASITRON_USER" -- lsblk -J -b -o NAME,KNAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,FSTYPE,UUID,PTTYPE,PARTTYPE,MOUNTPOINTS >/dev/null 2>&1 ||   die "$NASITRON_USER cannot read the block-device inventory"
sudo -u "$NASITRON_USER" sudo -n "$HELPER_PATH" dmesg >/dev/null 2>&1 ||   die "Restricted passwordless sudo helper validation failed"

SSH_PORT="$(sshd -T 2>/dev/null | awk '$1 == "port" { print $2; exit }')"
SSH_PORT="${SSH_PORT:-22}"
HOST_FQDN="$(hostname -f 2>/dev/null || hostname)"
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-unknown}"

FINGERPRINTS=""
for hostkey in /etc/ssh/ssh_host_*_key.pub; do
  [[ -r "$hostkey" ]] || continue
  FINGERPRINTS+="$(ssh-keygen -lf "$hostkey")"$'\n'
done

HOST_KEY_FINGERPRINT=""
if [[ -r /etc/ssh/ssh_host_ed25519_key.pub ]]; then
  HOST_KEY_FINGERPRINT="$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 | awk '{print $2}')"
else
  first_host_key="$(find /etc/ssh -maxdepth 1 -type f -name 'ssh_host_*_key.pub' | sort | head -n1 || true)"
  if [[ -n "$first_host_key" ]]; then
    HOST_KEY_FINGERPRINT="$(ssh-keygen -lf "$first_host_key" -E sha256 | awk '{print $2}')"
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
  CURL_ENROLL=(curl -fsS -X POST
    -H "Content-Type: application/json"
    -H "X-NASitron-Enrollment-Signature: $ENROLL_SIGNATURE"
    --data-binary "@$ENROLL_PAYLOAD"
  )
  if [[ "$ENROLL_INSECURE" -eq 1 ]]; then
    CURL_ENROLL+=(-k)
  fi
  ENROLL_RESPONSE="$("${CURL_ENROLL[@]}" "$ENROLL_URL")" || die "NASitron enrollment callback failed"
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
