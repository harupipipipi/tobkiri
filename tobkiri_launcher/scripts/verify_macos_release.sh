#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: verify_macos_release.sh --app-bundle PATH
EOF
}

app_bundle=''
while (($# > 0)); do
  case "$1" in
    --app-bundle)
      (($# >= 2)) || { usage; exit 2; }
      app_bundle=$2
      shift 2
      ;;
    -h|--help)
      usage >&1
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$app_bundle" ]]; then
  usage
  exit 2
fi
[[ -d "$app_bundle" ]] || {
  printf 'macOS release app bundle is missing: %s\n' "$app_bundle" >&2
  exit 1
}

bundle_identifier="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' \
  "$app_bundle/Contents/Info.plist")"
if [[ "$bundle_identifier" != 'dev.rumiai.app' ]]; then
  printf 'release macOS app has a non-production bundle identifier: %s\n' \
    "$bundle_identifier" >&2
  exit 1
fi
if [[ -e "$app_bundle/Contents/Resources/NON_PUBLISHABLE_CI_E2E_ARTIFACT.txt" \
   || -L "$app_bundle/Contents/Resources/NON_PUBLISHABLE_CI_E2E_ARTIFACT.txt" \
   || -e "$app_bundle/Contents/Resources/ci-e2e-artifact-policy.v1.json" \
   || -L "$app_bundle/Contents/Resources/ci-e2e-artifact-policy.v1.json" \
   || -e "$app_bundle/Contents/Resources/ci-e2e-signing-certificate.der" \
   || -L "$app_bundle/Contents/Resources/ci-e2e-signing-certificate.der" \
   || -e "$app_bundle/Contents/Resources/ci-e2e-startup-attestation.v1.json" \
   || -L "$app_bundle/Contents/Resources/ci-e2e-startup-attestation.v1.json" ]]; then
  printf 'non-publishable CI/E2E artifacts are forbidden in production releases\n' >&2
  exit 1
fi

command -v codesign >/dev/null 2>&1 || {
  printf 'codesign is required to verify the macOS release\n' >&2
  exit 1
}
codesign --verify --strict --all-architectures --verbose=2 "$app_bundle"
details="$(codesign -d -r- --verbose=4 "$app_bundle" 2>&1)"
if ! grep -Fqx 'Identifier=dev.rumiai.app' <<<"$details"; then
  printf 'macOS app has an unexpected code-signing identifier\n' >&2
  exit 1
fi
if ! grep -Fqx 'Signature=adhoc' <<<"$details"; then
  printf 'macOS release app must use an ad-hoc signature\n' >&2
  exit 1
fi
