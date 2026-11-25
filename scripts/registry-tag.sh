#!/usr/bin/env bash
# Look up or attach a version tag on ghcr.io/xooxoxxo/pond through the registry API, so the
# answer is a plain HTTP status and the tagged bytes are exactly the signed ones.
#
#   scripts/registry-tag.sh exists <version>          exit 0 free (404), 1 taken (200), 2 unclear
#   scripts/registry-tag.sh tag <version> <digest>    PUT the index bytes of <digest> as <version>
#
# Needs REGISTRY_USER and REGISTRY_TOKEN (in the workflow: github.actor and GITHUB_TOKEN),
# curl, jq, sha256sum or shasum. IMAGE_REPO overrides ghcr.io/xooxoxxo/pond.
set -euo pipefail

IMAGE_REPO="${IMAGE_REPO:-ghcr.io/xooxoxxo/pond}"
host="${IMAGE_REPO%%/*}"
name="${IMAGE_REPO#*/}"
accept="application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json"
accept="$accept,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json"

say() { echo "registry-tag: $*" >&2; }

# curl with a secret curl option (user = "..." or header = "...") read from stdin, so no
# credential ever appears on a command line (ps, /proc/*/cmdline).
curl_secret() {
  local option="$1" value; shift
  value="${option#*= \"}"; value="${value%\"}"
  # A quote, backslash or line break in the value would change the curl config line.
  [[ "$value" != *[\"\\]* && "$value" != *$'\n'* ]] || { say "refusing a credential with \" \\ or a line break"; return 1; }
  printf '%s\n' "$option" | curl --config - "$@"
}

# A registry token for pull,push; with no REGISTRY_TOKEN and ANONYMOUS=1, an anonymous pull
# token (for trying `exists` on a public image).
bearer() {
  if [[ -z "${REGISTRY_TOKEN:-}" && "${ANONYMOUS:-}" == 1 ]]; then
    curl -fsS "https://$host/token?service=$host&scope=repository:$name:pull" | jq -er .token
    return
  fi
  : "${REGISTRY_USER:?REGISTRY_USER is not set}" "${REGISTRY_TOKEN:?REGISTRY_TOKEN is not set}"
  curl_secret "user = \"$REGISTRY_USER:$REGISTRY_TOKEN\"" -fsS \
    "https://$host/token?service=$host&scope=repository:$name:pull,push" | jq -er .token
}

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | awk '{print $1}'; }

valid_version() { [[ "$1" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; }

# 0: no manifest under the tag (404). 1: there is one (200). 2: anything else.
exists() {
  local version="$1" token code
  valid_version "$version" || { say "version '$version' is not MAJOR.MINOR.PATCH"; return 2; }
  token="$(bearer)" || { say "no registry token"; return 2; }
  code="$(curl_secret "header = \"Authorization: Bearer $token\"" -sS -o /dev/null -w '%{http_code}' -I \
    -H "Accept: $accept" "https://$host/v2/$name/manifests/$version")" || code=000
  case "$code" in
    404) say "$IMAGE_REPO:$version is free (404)"; return 0 ;;
    200) say "$IMAGE_REPO:$version already exists"; return 1 ;;
    *)   say "cannot tell whether $IMAGE_REPO:$version exists (HTTP $code)"; return 2 ;;
  esac
}

# Fetch the index by digest, prove the bytes hash to that digest, and PUT them under the tag.
tag() {
  local version="$1" digest="$2" token tmp media got code
  valid_version "$version" || { say "version '$version' is not MAJOR.MINOR.PATCH"; return 1; }
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { say "digest '$digest' is not sha256:<64 hex>"; return 1; }
  token="$(bearer)"
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' RETURN
  curl_secret "header = \"Authorization: Bearer $token\"" -fsS -H "Accept: $accept" \
    "https://$host/v2/$name/manifests/$digest" -o "$tmp"
  got="sha256:$(sha256 "$tmp")"
  [[ "$got" == "$digest" ]] || { say "fetched bytes hash to $got, not $digest"; return 1; }
  media="$(jq -er .mediaType "$tmp")"
  case "$media" in
    application/vnd.oci.image.index.v1+json|application/vnd.docker.distribution.manifest.list.v2+json) ;;
    *) say "$digest is $media, not an index"; return 1 ;;
  esac
  code="$(curl_secret "header = \"Authorization: Bearer $token\"" -sS -o /dev/null -w '%{http_code}' -X PUT \
    -H "Content-Type: $media" --data-binary "@$tmp" "https://$host/v2/$name/manifests/$version")"
  [[ "$code" == 201 || "$code" == 200 ]] || { say "tagging $version failed (HTTP $code)"; return 1; }
  say "$IMAGE_REPO:$version -> $digest"
}

case "${1:-}" in
  exists) exists "${2:-}" ;;
  tag) tag "${2:-}" "${3:-}" ;;
  *) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
