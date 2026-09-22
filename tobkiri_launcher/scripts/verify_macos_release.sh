#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: verify_macos_release.sh --app-bundle PATH --signing-identity "Developer ID Application: ..."
EOF
}

app_bundle=''
signing_identity=''
while (($# > 0)); do
  case "$1" in
    --app-bundle)
      (($# >= 2)) || { usage; exit 2; }
      app_bundle=$2
      shift 2
      ;;
    --signing-identity)
      (($# >= 2)) || { usage; exit 2; }
      signing_identity=$2
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

if [[ -z "$app_bundle" || -z "$signing_identity" ]]; then
  usage
  exit 2
fi
if [[ "$signing_identity" != "Developer ID Application: "* ]]; then
  printf 'release macOS signing identity must be Developer ID Application, not ad-hoc\n' >&2
  exit 1
fi
[[ -d "$app_bundle" ]] || {
  printf 'macOS release app bundle is missing: %s\n' "$app_bundle" >&2
  exit 1
}

bundle_identifier="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' \
  "$app_bundle/Contents/Info.plist")"
if [[ "$bundle_identifier" != 'dev.tobkiri.launcher' ]]; then
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
codesign --verify --deep --strict --verbose=2 "$app_bundle"
details="$(codesign --display --verbose=4 "$app_bundle" 2>&1)"
if ! grep -Fqx "Authority=$signing_identity" <<<"$details"; then
  printf 'macOS app is not signed by the required Developer ID identity\n' >&2
  exit 1
fi
if grep -Fqx 'Authority=-' <<<"$details"; then
  printf 'ad-hoc macOS signatures are forbidden for release artifacts\n' >&2
  exit 1
fi
