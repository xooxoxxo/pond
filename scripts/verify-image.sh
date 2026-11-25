#!/usr/bin/env bash
# Verify a published Pond image before you run it.
#
#   scripts/verify-image.sh 1.15.0 [--digest sha256:<release-notes digest>]
#   scripts/verify-image.sh 1.15.0 --digest-only sha256:<digest>   (release workflow, before tagging)
#   scripts/verify-image.sh --check-index <raw index JSON file>      (both platforms only, offline)
#   scripts/verify-image.sh --self-test
#
# Checks, and exits non-zero on the first failure:
#   1. ghcr.io/xooxoxxo/pond:<version> resolves to one OCI index (or manifest list),
#      and that digest equals --digest when you pass the one from the release notes
#      (--digest-only skips the tag and checks repo@digest: the tag doesn't exist yet);
#   2. the index holds a linux/amd64 and a linux/arm64 image;
#   3. cosign verifies a signature on that exact digest, made by this repository's
#      release workflow at the tag v<version>, through GitHub's OIDC issuer.
#
# Needs: docker (with buildx, for `imagetools`), cosign, jq. No login: the image is public.
# IMAGE_REPO overrides the repository (default ghcr.io/xooxoxxo/pond).
set -euo pipefail

IMAGE_REPO="${IMAGE_REPO:-ghcr.io/xooxoxxo/pond}"
WORKFLOW_REPO="https://github.com/xooxoxxo/pond"
WORKFLOW_PATH=".github/workflows/release.yml"
OIDC_ISSUER="https://token.actions.githubusercontent.com"
REQUIRED_PLATFORMS=("linux/amd64" "linux/arm64")

fail() { echo "verify-image: FAIL: $*" >&2; return 1; }
ok() { echo "verify-image: ok: $*"; }

# The only certificate identity a release of <version> may carry.
expected_identity() {
  printf '%s/%s@refs/tags/v%s\n' "$WORKFLOW_REPO" "$WORKFLOW_PATH" "$1"
}

valid_version() { [[ "$1" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; }

# The top-level digest from `docker buildx imagetools inspect <ref>` text output (file $1).
parse_index_digest() {
  local digest
  digest="$(awk '$1 == "Digest:" { print $2; exit }' "$1")"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { fail "no sha256 digest in the inspect output"; return 1; }
  printf '%s\n' "$digest"
}

# Raw index JSON (file $1, from `imagetools inspect --raw`): it is an index, and it holds
# every required platform. Prints "<platform> <digest>" per required platform.
check_platforms() {
  local raw="$1" media platform digest
  media="$(jq -r '.mediaType // ""' "$raw")" || { fail "the index is not JSON"; return 1; }
  case "$media" in
    application/vnd.oci.image.index.v1+json|application/vnd.docker.distribution.manifest.list.v2+json) ;;
    *) fail "expected one multi-arch index, got mediaType '${media}'"; return 1 ;;
  esac
  for platform in "${REQUIRED_PLATFORMS[@]}"; do
    digest="$(jq -r --arg os "${platform%/*}" --arg arch "${platform#*/}" '
      [.manifests[]? | select(.platform.os == $os and .platform.architecture == $arch
                              and ((.platform.variant // "") == "" or $arch == "arm64"))
       | .digest] | if length == 1 then .[0] else "" end' "$raw")"
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { fail "the index has no single ${platform} image"; return 1; }
    printf '%s %s\n' "$platform" "$digest"
  done
}

# `cosign verify --output json` (file $1) for the digest $2: every verified payload names
# that digest, and any certificate subject or issuer it reports is the expected one.
# cosign itself already refused any other identity (exact --certificate-identity and
# --certificate-oidc-issuer); this is a second look at what it reports.
check_cosign_output() {
  local out="$1" digest="$2" identity="$3" issuer="$4" bad
  jq -e 'type == "array" and length > 0' "$out" >/dev/null 2>&1 \
    || { fail "cosign reported no verified signature"; return 1; }
  bad="$(jq -r --arg d "$digest" --arg id "$identity" --arg iss "$issuer" '
    .[] |
    if .critical.image["docker-manifest-digest"] != $d then
      "signature is for \(.critical.image["docker-manifest-digest"]), not \($d)"
    elif (.optional.Subject // $id) != $id then
      "unexpected signer \(.optional.Subject)"
    elif (.optional.Issuer // $iss) != $iss then
      "unexpected issuer \(.optional.Issuer)"
    else empty end' "$out" | head -n 1)"
  [[ -z "$bad" ]] || { fail "$bad"; return 1; }
}

# The exact cosign arguments, into COSIGN_ARGS: an exact identity, never a regexp.
cosign_verify_args() {
  COSIGN_ARGS=(verify
    --certificate-identity "$(expected_identity "$2")"
    --certificate-oidc-issuer "$OIDC_ISSUER"
    --output json
    "$1")
}

verify() {
  local version="$1" want_digest="$2" mode="${3:-tag}" tmp digest platforms
  valid_version "$version" || { fail "version '$version' is not MAJOR.MINOR.PATCH (no leading v)"; return 1; }
  if [[ -n "$want_digest" && ! "$want_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fail "the digest '$want_digest' is not sha256:<64 hex>"; return 1
  fi
  for tool in docker jq cosign; do
    command -v "$tool" >/dev/null || { fail "$tool is not installed"; return 1; }
  done
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  if [[ "$mode" == digest-only ]]; then
    digest="$want_digest"
    ok "checking ${IMAGE_REPO}@${digest} by digest (no tag)"
  else
    local ref="${IMAGE_REPO}:${version}"
    docker buildx imagetools inspect "$ref" >"$tmp/inspect.txt" || { fail "cannot read $ref"; return 1; }
    digest="$(parse_index_digest "$tmp/inspect.txt")"
    if [[ -n "$want_digest" && "$digest" != "$want_digest" ]]; then
      fail "$ref points at $digest, the release names $want_digest"; return 1
    fi
    ok "$ref -> $digest"
  fi

  docker buildx imagetools inspect --raw "${IMAGE_REPO}@${digest}" >"$tmp/index.json" \
    || { fail "cannot read the index ${IMAGE_REPO}@${digest}"; return 1; }
  platforms="$(check_platforms "$tmp/index.json")"
  while read -r p d; do ok "$p $d"; done <<<"$platforms"

  cosign_verify_args "${IMAGE_REPO}@${digest}" "$version"
  cosign "${COSIGN_ARGS[@]}" >"$tmp/cosign.json" 2>"$tmp/cosign.err" \
    || { cat "$tmp/cosign.err" >&2; fail "cosign refused the signature"; return 1; }
  check_cosign_output "$tmp/cosign.json" "$digest" "$(expected_identity "$version")" "$OIDC_ISSUER"
  ok "signed by $(expected_identity "$version") ($OIDC_ISSUER)"
  echo "verify-image: ${IMAGE_REPO}@${digest} is the signed ${version} release"
}

# Fixture tests of the parsing above: no network, no docker, no cosign.
self_test() {
  local dir passed=0 failed=0
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fixtures/verify-image"
  local D="sha256:1111111111111111111111111111111111111111111111111111111111111111"
  local id iss
  id="$(expected_identity 1.15.0)"; iss="$OIDC_ISSUER"

  expect() {  # expect pass|fail <name> <command...>
    local want="$1" name="$2" got err; shift 2
    if err="$("$@" 2>&1 >/dev/null)"; then got=pass; else got=fail; fi
    if [[ "$got" == "$want" ]]; then
      passed=$((passed + 1)); echo "  ok   $name (expected $want)${err:+: ${err#verify-image: }}"
    else failed=$((failed + 1)); echo "  FAIL $name: expected $want, got $got"; fi
  }

  echo "verify-image self-test"
  expect pass "identity is the release workflow at the tag" \
    test "$id" = "https://github.com/xooxoxxo/pond/.github/workflows/release.yml@refs/tags/v1.15.0"
  cosign_verify_args "x@$D" 1.15.0
  expect pass "cosign args carry the exact identity and issuer, no regexp" \
    test "${COSIGN_ARGS[*]}" = "verify --certificate-identity $id --certificate-oidc-issuer $iss --output json x@$D"
  expect fail "version with a leading v" valid_version v1.15.0
  expect pass "inspect output digest" parse_index_digest "$dir/inspect-ok.txt"
  expect pass "inspect digest value" \
    test "$(parse_index_digest "$dir/inspect-ok.txt")" = "$D"
  expect fail "inspect output without a digest" parse_index_digest "$dir/inspect-nodigest.txt"
  expect pass "index with amd64, arm64 and attestations" check_platforms "$dir/index-ok.json"
  expect fail "index missing arm64" check_platforms "$dir/index-no-arm64.json"
  expect fail "index missing amd64" check_platforms "$dir/index-no-amd64.json"
  expect fail "single-platform manifest, not an index" check_platforms "$dir/manifest-single.json"
  expect pass "cosign output from the release workflow" check_cosign_output "$dir/cosign-ok.json" "$D" "$id" "$iss"
  expect pass "cosign output without certificate fields" check_cosign_output "$dir/cosign-nocert.json" "$D" "$id" "$iss"
  expect fail "cosign output signed by another workflow" check_cosign_output "$dir/cosign-other-signer.json" "$D" "$id" "$iss"
  expect fail "cosign output signed at another tag" check_cosign_output "$dir/cosign-ok.json" "$D" "$(expected_identity 1.14.0)" "$iss"
  expect fail "cosign output from another issuer" check_cosign_output "$dir/cosign-other-issuer.json" "$D" "$id" "$iss"
  expect fail "cosign output for another digest" check_cosign_output "$dir/cosign-ok.json" "sha256:$(printf '2%.0s' {1..64})" "$id" "$iss"
  expect pass "--check-index on a good index" bash "${BASH_SOURCE[0]}" --check-index "$dir/index-ok.json"
  expect fail "--check-index on an index missing arm64" bash "${BASH_SOURCE[0]}" --check-index "$dir/index-no-arm64.json"
  expect fail "--digest-only without a sha256 digest" verify 1.15.0 "latest" digest-only
  expect fail "cosign output with no signatures" check_cosign_output "$dir/cosign-empty.json" "$D" "$id" "$iss"
  echo "verify-image self-test: $passed passed, $failed failed"
  [[ "$failed" -eq 0 ]]
}

main() {
  case "${1:-}" in
    --self-test) self_test ;;
    --check-index)
      [[ -f "${2:-}" ]] || { fail "--check-index needs a file"; exit 2; }
      check_platforms "$2" ;;
    ""|-h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; [[ -n "${1:-}" ]] ;;
    *)
      local version="$1" want=""; shift
      local mode=tag
      case "${1:-}" in
        --digest) want="${2:-}" ;;
        --digest-only) want="${2:-}"; mode="digest-only" ;;
        "") ;;
        *) fail "unknown option $1"; exit 2 ;;
      esac
      if [[ -n "${1:-}" && -z "$want" ]]; then fail "$1 needs a value"; exit 2; fi
      verify "$version" "$want" "$mode"
      ;;
  esac
}

main "$@"
