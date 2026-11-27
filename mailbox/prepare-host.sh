#!/bin/bash
# Idempotent host-side setup for a Pond deploy (run it next to compose.yaml).
#
# Docker auto-creates a bind-mount's source directory as root:root 755 the
# first time `docker compose up` runs, if it doesn't already exist yet, and
# `backups/` holds the mirrored mailbox token plus nightly snapshots -- so it
# has to be locked down on the host before the stack ever starts.
#
# Refuses to proceed unless .env exists, is mode 600, and has the required
# host settings and either both Telegram credentials or neither (values are never printed). Run this once, before the first
# `docker compose up --build -d`. Safe to re-run any time.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" -ne 0 ]; then
    echo "usage: $0" >&2
    exit 2
fi
ENV_FILE="$SCRIPT_DIR/.env"
BACKUPS_DIR="$SCRIPT_DIR/backups"
REQUIRED_KEYS="MAILBOX_CONTEXT TZ"

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

# Refuse without a private .env.
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

# Check presence without sourcing .env or passing its secrets as arguments.
# The final assignment wins. Empty quoted values and inline comments are allowed.
has_value() {
    awk -v key="$1" '
        $0 ~ "^[[:space:]]*(export[[:space:]]+)?" key "[[:space:]]*=" {
            value = $0
            sub(/^[^=]*=/, "", value)
            sub(/[[:space:]]+#.*/, "", value)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            present = value != "" && value !~ /^"[[:space:]]*"$/ && value !~ /^\047[[:space:]]*\047$/
        }
        END { exit !present }
    ' "$ENV_FILE"
}

for key in $REQUIRED_KEYS; do
    if ! has_value "$key"; then
        echo "ERROR: $ENV_FILE is missing required key $key" >&2
        exit 2
    fi
done

if has_value TELEGRAM_BOT_TOKEN; then has_token=1; else has_token=0; fi
if has_value MAILBOX_OWNER_ID; then has_owner=1; else has_owner=0; fi
if [ "$has_token" != "$has_owner" ]; then
    echo "ERROR: set both TELEGRAM_BOT_TOKEN and MAILBOX_OWNER_ID, or leave both empty to disable Telegram" >&2
    exit 2
fi
echo "[prepare-host] $ENV_FILE has all required settings"

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
