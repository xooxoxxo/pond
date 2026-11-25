#!/bin/bash
# Idempotent host-side setup for a Pond deploy (run it next to compose.yaml).
#
# Docker auto-creates a bind-mount's source directory as root:root 755 the
# first time `docker compose up` runs, if it doesn't already exist yet, and
# `backups/` holds the mirrored mailbox token plus nightly snapshots -- so it
# has to be locked down on the host before the stack ever starts.
#
# Refuses to proceed unless .env exists, is mode 600, and has all four
# required keys (values are never printed). Run this once, before the first
# `docker compose up --build -d`. Safe to re-run any time.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" -ne 0 ]; then
    echo "usage: $0" >&2
    exit 2
fi
ENV_FILE="$SCRIPT_DIR/.env"
BACKUPS_DIR="$SCRIPT_DIR/backups"
REQUIRED_KEYS="TELEGRAM_BOT_TOKEN MAILBOX_OWNER_ID MAILBOX_CONTEXT TZ"

# The real, non-root user who should own backups/. When invoked via sudo,
# that's the person who ran sudo (SUDO_USER) -- not root.
if [ "$(id -u)" = "0" ] && [ -n "${SUDO_USER:-}" ]; then
    TARGET_USER="$SUDO_USER"
else
    TARGET_USER="$(id -un)"
fi
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GID="$(id -g "$TARGET_USER")"

stat_mode() {
    # GNU stat (Linux) first, BSD stat (macOS) as a fallback.
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

stat_uid() {
    stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1"
}

# Refuse without a 600 .env carrying all four required keys.
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found -- copy $(basename "$ENV_FILE").example to $(basename "$ENV_FILE") and fill it in" >&2
    exit 2
fi

env_perms="$(stat_mode "$ENV_FILE")"
if [ "$env_perms" != "600" ]; then
    echo "ERROR: $ENV_FILE is mode $env_perms, not 600 -- run: chmod 600 $ENV_FILE" >&2
    exit 2
fi
echo "[prepare-host] $ENV_FILE is 600"

for key in $REQUIRED_KEYS; do
    if ! grep -q "^${key}=" "$ENV_FILE"; then
        echo "ERROR: $ENV_FILE is missing required key $key" >&2
        exit 2
    fi
done
echo "[prepare-host] $ENV_FILE has all required keys"

if [ ! -d "$BACKUPS_DIR" ]; then
    mkdir -p "$BACKUPS_DIR"
fi

current_uid="$(stat_uid "$BACKUPS_DIR")"
if [ "$current_uid" != "$TARGET_UID" ]; then
    if ! chown "$TARGET_UID:$TARGET_GID" "$BACKUPS_DIR" 2>/dev/null; then
        echo "ERROR: $BACKUPS_DIR is owned by uid $current_uid, not $TARGET_USER (uid $TARGET_UID)." >&2
        echo "       This happens when Docker created it as root before this script ran." >&2
        echo "       Re-run with sudo to fix ownership:" >&2
        echo "         sudo $SCRIPT_DIR/prepare-host.sh" >&2
        exit 1
    fi
fi

chmod 700 "$BACKUPS_DIR"
echo "[prepare-host] $BACKUPS_DIR -> 700, owned by $TARGET_USER"

echo "[prepare-host] setup complete -- ready for 'docker compose up --build -d'"
