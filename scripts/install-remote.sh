#!/usr/bin/env bash
set -Eeuo pipefail

PROGRAM="NASitron remote NAS installer"
DEFAULT_USER="nasitron"
DEFAULT_HELPER="/usr/local/sbin/nasitron-root-helper"
DEFAULT_REPO="StealthCat/NASitron"
DEFAULT_REF="main"

NASITRON_USER="${NASITRON_REMOTE_USER:-$DEFAULT_USER}"
HELPER_PATH="${NASITRON_REMOTE_HELPER_PATH:-$DEFAULT_HELPER}"
REPO_SLUG="${NASITRON_REPO:-$DEFAULT_REPO}"
REPO_REF="${NASITRON_REPO_REF:-$DEFAULT_REF}"
PUBLIC_KEY="${NASITRON_SSH_PUBLIC_KEY:-}"
PUBLIC_KEY_FILE=""
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
  sudo ./scripts/install-remote.sh --public-key-file /path/to/nasitron.pub
  sudo ./scripts/install-remote.sh --public-key 'ssh-ed25519 AAAA...'
  sudo NASITRON_SSH_PUBLIC_KEY='ssh-ed25519 AAAA...' ./scripts/install-remote.sh

Options:
  --public-key KEY          Public key authorized for the NASitron SSH account.
  --public-key-file FILE    Read the public key from FILE.
  --user USER               Remote monitoring account (default: nasitron).
  --helper-path PATH        Root helper install path
                            (default: /usr/local/sbin/nasitron-root-helper).
  --repo OWNER/REPO         Repository used when local install assets are absent
                            (default: StealthCat/NASitron).
  --ref REF                 Repository ref used for downloads (default: main).
  --skip-packages           Do not apt-install OpenSSH/ZFS/SMART dependencies.
  --skip-ssh-hardening      Do not install the per-user sshd hardening drop-in.
  --quiet                   Reduce installer output.
  -h, --help                Show this help.

Private repository downloads:
  Run this script from a NASitron checkout (preferred), or set GITHUB_TOKEN /
  NASITRON_GITHUB_TOKEN so the helper can be downloaded if local assets are absent.

Security:
  The installer uses SSH public-key authentication. It locks password login for the
  dedicated account and grants passwordless sudo only to NASitron's root helper.
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
    --repo)
      [[ $# -ge 2 ]] || die "--repo requires OWNER/REPO"
      REPO_SLUG="$2"
      shift 2
      ;;
    --ref)
      [[ $# -ge 2 ]] || die "--ref requires a value"
      REPO_REF="$2"
      shift 2
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
[[ "$REPO_SLUG" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "--repo must be OWNER/REPO"
[[ -n "$REPO_REF" && "$REPO_REF" != *[[:space:]]* ]] || die "--ref must be non-empty and contain no whitespace"

if [[ -n "$PUBLIC_KEY_FILE" ]]; then
  [[ -r "$PUBLIC_KEY_FILE" ]] || die "Cannot read public key file: $PUBLIC_KEY_FILE"
  PUBLIC_KEY="$(cat "$PUBLIC_KEY_FILE")"
fi

PUBLIC_KEY="$(printf '%s' "$PUBLIC_KEY" | tr -d '\r' | awk 'NF { print; exit }')"
[[ -n "$PUBLIC_KEY" ]] || die "Provide the NASitron SSH public key with --public-key-file, --public-key, or NASITRON_SSH_PUBLIC_KEY"
[[ "$PUBLIC_KEY" != *$'\n'* ]] || die "Only one SSH public key may be installed per invocation"

TMP_DIR="$(mktemp -d -t nasitron-install.XXXXXX)"
KEY_CHECK="$TMP_DIR/nasitron.pub"
printf '%s\n' "$PUBLIC_KEY" > "$KEY_CHECK"
chmod 0600 "$KEY_CHECK"
ssh-keygen -l -f "$KEY_CHECK" >/dev/null 2>&1 || die "The supplied SSH public key is not valid"

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

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
LOCAL_HELPER="$REPO_ROOT/remote/nasitron_root_helper.py"

if [[ -r "$LOCAL_HELPER" ]]; then
  log "Installing root helper from local NASitron checkout"
  cp "$LOCAL_HELPER" "$TMP_DIR/nasitron-root-helper"
else
  RAW_URL="https://raw.githubusercontent.com/$REPO_SLUG/$REPO_REF/remote/nasitron_root_helper.py"
  log "Local helper not found; downloading from $REPO_SLUG@$REPO_REF"
  CURL_ARGS=(--fail --silent --show-error --location --proto '=https' --tlsv1.2)
  TOKEN="${NASITRON_GITHUB_TOKEN:-${GITHUB_TOKEN:-}}"
  if [[ -n "$TOKEN" ]]; then
    CURL_ARGS+=(-H "Authorization: Bearer $TOKEN")
  fi
  if ! curl "${CURL_ARGS[@]}" "$RAW_URL" -o "$TMP_DIR/nasitron-root-helper"; then
    die "Unable to download the root helper. Run from a NASitron checkout or provide GITHUB_TOKEN/NASITRON_GITHUB_TOKEN for a private repository."
  fi
fi

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

cat <<EOF

NASitron remote NAS setup complete.

Use these values when adding the server in NASitron:
  Hostname:        $HOST_FQDN
  Detected IP:     $HOST_IP
  SSH port:        $SSH_PORT
  SSH username:    $NASITRON_USER
  Authentication:  key
  SMART via sudo:  enabled/recommended
  Root helper:     $HELPER_PATH

Paste the PRIVATE key corresponding to the public key supplied to this installer
into NASitron's server form. Do not paste the public key into that field.

SSH host-key fingerprints (verify one through this trusted console before enabling
strict host-key checking in NASitron):
$FINGERPRINTS
EOF
