#!/bin/sh
# Every handpicked command, the container's own and docker exec's alike, runs
# as PUID:PGID (default 99:100, Unraid's nobody:users). A command run as root
# would leave root-owned database files the scheduled runs can't write.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
HANDPICKED=/app/.venv/bin/handpicked

if [ "$(id -u)" != 0 ]; then
    exec "$HANDPICKED" "$@"
fi
# Docker creates a missing appdata folder, or a new volume, owned by root,
# and a restored backup may be too.
find "$(dirname "$HANDPICKED_CONFIG")" \( ! -user "$PUID" -o ! -group "$PGID" \) \
    -exec chown -h "$PUID:$PGID" {} +
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$HANDPICKED" "$@"
