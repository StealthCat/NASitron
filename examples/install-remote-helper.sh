#!/bin/sh
set -eu

SOURCE="${1:-remote/nasitron_root_helper.py}"
DEST="/usr/local/sbin/nasitron-root-helper"
SUDOERS="/etc/sudoers.d/nasitron"

if [ ! -f "$SOURCE" ]; then
  echo "Helper source not found: $SOURCE" >&2
  exit 1
fi

sudo install -o root -g root -m 0755 "$SOURCE" "$DEST"
sudo install -o root -g root -m 0440 examples/nasitron.sudoers "$SUDOERS"
sudo visudo -cf "$SUDOERS"

echo "Installed $DEST and validated $SUDOERS"
