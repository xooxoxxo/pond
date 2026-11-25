#!/usr/bin/env bash
# Test one platform image of a Pond build: it is that platform, GET /version answers
# pond <version> protocol 5, GET /health reports the database up, and no layer holds a
# file that must not ship.
#
#   scripts/test-image.sh <local image> <amd64|arm64> <version>
#
# Runs the image with --rm, no network and a placeholder Telegram token (the HTTP API
# answers while Telegram polling fails; the token never leaves the container), then
# removes the container and the saved archive.
set -euo pipefail

img="$1" arch="$2" version="$3"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

got="$(docker image inspect "$img" --format '{{.Os}}/{{.Architecture}}')"
[[ "$got" == "linux/$arch" ]] || { echo "test-image: $img is $got, not linux/$arch" >&2; exit 1; }

name="pondreltest-$arch-$$-$RANDOM"
tmp="$(mktemp -d)"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$tmp"; }
trap cleanup EXIT

docker run -d --rm --name "$name" --platform "linux/$arch" --network none \
  -e TELEGRAM_BOT_TOKEN=0:release-check -e MAILBOX_OWNER_ID=1 "$img" >/dev/null

body=""
for _ in $(seq 1 60); do
  # /version needs the API token, which the Pond writes to /data on first start.
  if body="$(docker exec "$name" sh -c \
      'wget -qO- --header "Authorization: Bearer $(cat /data/mailbox-token)" http://127.0.0.1:8790/version' \
      2>/dev/null)"; then break; fi
  body=""
  sleep 2
done
echo "test-image: linux/$arch /version: ${body:-<no answer>}"
jq -e --arg v "$version" '.service == "pond" and .version == $v and .protocol == 5' <<<"$body" >/dev/null \
  || { echo "test-image: linux/$arch /version is not pond $version protocol 5" >&2; { docker logs "$name" 2>&1 | tail -n 20 >&2; } || true; exit 1; }

# /health: with no network Telegram never answers, so it is 503 with telegram_ok false; the
# rest of the report must still be there, with the database up.
health="$(docker exec "$name" python3 -c '
import urllib.request, urllib.error
try:
    print(urllib.request.urlopen("http://127.0.0.1:8790/health", timeout=5).read().decode())
except urllib.error.HTTPError as err:
    print(err.read().decode())
' 2>/dev/null || true)"
echo "test-image: linux/$arch /health: ${health:-<no answer>}"
jq -e '.db_ok == true and (.telegram_ok | type) == "boolean" and has("ok")' <<<"$health" >/dev/null \
  || { echo "test-image: linux/$arch /health is not a health report with db_ok true" >&2; exit 1; }

docker save "$img" -o "$tmp/image.tar"
python3 "$here/check-image-files.py" "$tmp/image.tar" --dockerfile "$here/../mailbox/Dockerfile"
