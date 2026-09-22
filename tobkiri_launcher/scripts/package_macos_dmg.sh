#!/usr/bin/env bash
# Package a signed Tauri macOS application without Finder automation.

set -Eeuo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: package_macos_dmg.sh --app-bundle PATH --target TARGET --output-dir PATH \
  [--signing-identity "Developer ID Application: ..." | --allow-ad-hoc-local | \
   --ci-e2e-cert-sha256 SHA256]
USAGE
}

app_bundle=''
target=''
output_dir=''
signing_identity=''
allow_ad_hoc_local=0
ci_e2e_cert_sha256=''

while (($# > 0)); do
  case "$1" in
    --app-bundle)
      (($# >= 2)) || { usage; exit 2; }
      app_bundle=$2
      shift 2
      ;;
    --target)
      (($# >= 2)) || { usage; exit 2; }
      target=$2
      shift 2
      ;;
    --output-dir)
      (($# >= 2)) || { usage; exit 2; }
      output_dir=$2
      shift 2
      ;;
    --signing-identity)
      (($# >= 2)) || { usage; exit 2; }
      ((allow_ad_hoc_local == 0)) || { usage; exit 2; }
      signing_identity=$2
      shift 2
      ;;
    --allow-ad-hoc-local)
      [[ -z "$signing_identity" && -z "$ci_e2e_cert_sha256" ]] || { usage; exit 2; }
      allow_ad_hoc_local=1
      shift
      ;;
    --ci-e2e-cert-sha256)
      (($# >= 2)) || { usage; exit 2; }
      [[ -z "$signing_identity" && "$allow_ad_hoc_local" -eq 0 ]] || { usage; exit 2; }
      ci_e2e_cert_sha256=$2
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

if [[ -z "$app_bundle" || -z "$target" || -z "$output_dir" ]]; then
  usage
  exit 2
fi

case "$target" in
  x86_64-apple-darwin)
    architecture_suffix='x64'
    ;;
  aarch64-apple-darwin)
    architecture_suffix='aarch64'
    ;;
  *)
    printf 'Unsupported macOS target: %s\n' "$target" >&2
    exit 2
    ;;
esac

[[ -d "$app_bundle" ]] || {
  printf 'Tauri app bundle does not exist: %s\n' "$app_bundle" >&2
  exit 1
}
[[ -f "$app_bundle/Contents/Info.plist" ]] || {
  printf 'Tauri app bundle is missing Contents/Info.plist: %s\n' "$app_bundle" >&2
  exit 1
}

command -v codesign >/dev/null 2>&1 || {
  printf '%s\n' 'codesign is required to verify the signed app bundle' >&2
  exit 1
}
command -v ditto >/dev/null 2>&1 || {
  printf '%s\n' 'ditto is required to stage the app bundle' >&2
  exit 1
}
command -v plutil >/dev/null 2>&1 || {
  printf '%s\n' 'plutil is required to read the app bundle version' >&2
  exit 1
}
command -v hdiutil >/dev/null 2>&1 || {
  printf '%s\n' 'hdiutil is required to create the macOS installer' >&2
  exit 1
}

if [[ -n "$signing_identity" && "$signing_identity" != "Developer ID Application: "* ]]; then
  printf '%s\n' 'release macOS signing identity must be Developer ID Application' >&2
  exit 1
fi
if [[ -z "$signing_identity" && "$allow_ad_hoc_local" -ne 1 && -z "$ci_e2e_cert_sha256" ]]; then
  printf '%s\n' 'a Developer ID identity or explicit non-publishable CI/E2E identity is required' >&2
  exit 1
fi
if [[ -n "$ci_e2e_cert_sha256" && ! "$ci_e2e_cert_sha256" =~ ^[0-9a-f]{64}$ ]]; then
  printf '%s\n' 'CI/E2E signing certificate identity must be a lowercase SHA-256' >&2
  exit 1
fi

tobkiri_packaging_python="${TOBKIRI_PACKAGING_PYTHON-}"
tobkiri_packaging_python_sha256="${TOBKIRI_PACKAGING_PYTHON_SHA256-}"
tobkiri_packaging_python_snapshot="${TOBKIRI_PACKAGING_PYTHON_SNAPSHOT-}"

verify_formal_python() {
  local actual_sha256=''
  if [[ -z "$tobkiri_packaging_python" || -z "$tobkiri_packaging_python_sha256" ]]; then
    printf '%s\n' 'missing formal Python binding: TOBKIRI_PACKAGING_PYTHON path and TOBKIRI_PACKAGING_PYTHON_SHA256 digest are required' >&2
    return 1
  fi
  if [[ "$tobkiri_packaging_python" != /* || "$tobkiri_packaging_python" == python3 || "$tobkiri_packaging_python" == python ]]; then
    printf '%s\n' 'TOBKIRI_PACKAGING_PYTHON must be an absolute path, not ambient python3' >&2
    return 1
  fi
  if [[ ! -f "$tobkiri_packaging_python" || -L "$tobkiri_packaging_python" || ! -x "$tobkiri_packaging_python" ]]; then
    printf '%s\n' 'TOBKIRI_PACKAGING_PYTHON wrapper path is not a regular executable' >&2
    return 1
  fi
  if [[ -z "$tobkiri_packaging_python_snapshot" \
     || "$tobkiri_packaging_python_snapshot" != /* \
     || ! -d "$tobkiri_packaging_python_snapshot" \
     || -L "$tobkiri_packaging_python_snapshot" ]]; then
    printf '%s\n' 'TOBKIRI_PACKAGING_PYTHON_SNAPSHOT must be an absolute, real directory' >&2
    return 1
  fi
  if [[ ! "$tobkiri_packaging_python_sha256" =~ ^[0-9a-fA-F]{64}$ ]]; then
    printf '%s\n' 'TOBKIRI_PACKAGING_PYTHON_SHA256 is not a hexadecimal digest' >&2
    return 1
  fi
  actual_sha256=$(shasum -a 256 "$tobkiri_packaging_python" | awk '{print $1}')
  if [[ "$actual_sha256" != "$tobkiri_packaging_python_sha256" ]]; then
    printf '%s\n' 'TOBKIRI_PACKAGING_PYTHON_SHA256 mismatch for the wrapper path' >&2
    return 1
  fi
}

run_formal_python() {
  verify_formal_python || return 1
  # The sealed venv resolves its relocatable home from the launch root.
  # Keep that root stable even when the caller's working directory is elsewhere.
  (
    cd "$tobkiri_packaging_python_snapshot" || {
      printf 'Could not enter formal Python snapshot: %s\n' \
        "$tobkiri_packaging_python_snapshot" >&2
      exit 1
    }
    "$tobkiri_packaging_python" -I -B "$@"
  )
}

bound_image_metadata() {
  local image_path=$1
  run_formal_python - "$image_path" <<'PY'
import os
import stat
import sys

try:
    named_metadata = os.lstat(sys.argv[1])
except OSError:
    raise SystemExit(1)
if stat.S_ISLNK(named_metadata.st_mode) or not stat.S_ISREG(named_metadata.st_mode):
    raise SystemExit(1)
try:
    nofollow = os.O_NOFOLLOW
except AttributeError:
    raise SystemExit(1)
try:
    descriptor = os.open(
        sys.argv[1],
        os.O_RDONLY
        | nofollow
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0),
    )
except OSError:
    raise SystemExit(1)
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or named_metadata.st_nlink != 1
        or metadata.st_dev != named_metadata.st_dev
        or metadata.st_ino != named_metadata.st_ino
    ):
        raise SystemExit(1)
finally:
    os.close(descriptor)
print("%d:%d:%d:%d:%d:%d" % (
    metadata.st_dev,
    metadata.st_ino,
    metadata.st_uid,
    metadata.st_size,
    metadata.st_mode,
    metadata.st_nlink,
))
PY
}

image_identity() {
  bound_image_metadata "$1"
}

verify_bound_image() {
  local image_path=$1
  local expected_identity=$2
  run_formal_python - "$image_path" "$expected_identity" <<'PY'
import os
import stat
import subprocess
import sys

image_path = sys.argv[1]
expected_identity = sys.argv[2]


def identity(metadata: os.stat_result) -> str:
    return "%d:%d:%d:%d:%d:%d" % (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
    )


try:
    named_metadata = os.lstat(image_path)
except OSError:
    raise SystemExit(1)
if stat.S_ISLNK(named_metadata.st_mode) or not stat.S_ISREG(named_metadata.st_mode):
    raise SystemExit(1)
try:
    nofollow = os.O_NOFOLLOW
except AttributeError:
    raise SystemExit(1)
try:
    descriptor = os.open(
        image_path,
        os.O_RDONLY
        | nofollow
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0),
    )
except OSError:
    raise SystemExit(1)
try:
    opened_metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened_metadata.st_mode)
        or opened_metadata.st_uid != os.geteuid()
        or identity(opened_metadata) != expected_identity
        or identity(named_metadata) != expected_identity
    ):
        raise SystemExit(1)
    os.set_inheritable(descriptor, True)
    verification_status = subprocess.run(
        ["hdiutil", "verify", f"/dev/fd/{descriptor}"],
        check=False,
        pass_fds=(descriptor,),
    ).returncode
    retained_metadata = os.fstat(descriptor)
    try:
        retained_path_metadata = os.lstat(image_path)
    except OSError:
        retained_path_metadata = None
    if (
        identity(retained_metadata) != expected_identity
        or retained_path_metadata is None
        or stat.S_ISLNK(retained_path_metadata.st_mode)
        or not stat.S_ISREG(retained_path_metadata.st_mode)
        or identity(retained_path_metadata) != expected_identity
    ):
        print(
            "detached image identity changed during retained verification: "
            + image_path,
            file=sys.stderr,
        )
        raise SystemExit(1)
finally:
    os.close(descriptor)
raise SystemExit(verification_status)
PY
}

find_exact_device() {
  local target_path=$1
  local info=''
  if ! info=$(hdiutil info); then
    return 1
  fi
  awk -v target="$target_path" '
    function finish_record() {
      if (has_record) {
        if (record_path == target) {
          target_records++
          if (record_devices != 1) {
            invalid = 1
          } else {
            selected_device = record_device
          }
        }
      }
      has_record = 0
      record_path = ""
      record_device = ""
      record_devices = 0
    }
    BEGIN {
      has_record = 0
      target_records = 0
      selected_device = ""
      invalid = 0
    }
    /^[[:space:]]*image-path[[:space:]]*:/ {
      finish_record()
      record_path = $0
      sub(/^[^:]*:[[:space:]]*/, "", record_path)
      sub(/^[[:space:]]+/, "", record_path)
      sub(/[[:space:]]+$/, "", record_path)
      if (record_path == "") {
        invalid = 1
      }
      has_record = 1
      next
    }
    /^[[:space:]]*=+[[:space:]]*$/ {
      finish_record()
      next
    }
    has_record && $0 ~ /^[[:space:]]*\/dev\/disk[0-9]+[[:space:]]/ {
      record_devices++
      if (record_devices == 1) {
        record_device = $1
      }
      next
    }
    has_record && $0 ~ /^[[:space:]]*\/dev\/disk[0-9]+s[0-9]+[[:space:]]/ {
      next
    }
    has_record && $0 ~ /^[[:space:]]*\/dev\/disk/ {
      invalid = 1
      next
    }
    END {
      finish_record()
      if (invalid || target_records != 1 || selected_device == "") {
        exit 1
      }
      print selected_device
    }
  ' <<<"$info"
}

owned_image_paths=()
owned_image_identities=()
owned_image_devices=()

record_owned_image() {
  local image_path=$1
  local image_id="${2-}"
  local image_device=''
  [[ -f "$image_path" && ! -L "$image_path" ]] || return 1
  if [[ -z "$image_id" ]]; then
    image_id=$(image_identity "$image_path") || return 1
  fi
  image_device=$(find_exact_device "$image_path") || return 1
  [[ "$image_device" == /dev/disk[0-9]* ]] || return 1
  owned_image_paths[${#owned_image_paths[@]}]="$image_path"
  owned_image_identities[${#owned_image_identities[@]}]="$image_id"
  owned_image_devices[${#owned_image_devices[@]}]="$image_device"
}

record_failed_image_if_owned() {
  local image_path=$1
  if [[ -f "$image_path" && ! -L "$image_path" ]]; then
    record_owned_image "$image_path" || true
  fi
}

detach_owned_image() {
  local index_value=$1
  local image_path="${owned_image_paths[$index_value]}"
  local expected_id="${owned_image_identities[$index_value]}"
  local expected_device="${owned_image_devices[$index_value]}"
  local current_id=''
  local current_device=''
  if ! current_id=$(image_identity "$image_path"); then
    printf 'image path identity changed; detach refused: %s\n' "$image_path" >&2
    return 1
  fi
  if [[ "$current_id" != "$expected_id" ]]; then
    printf 'image path identity changed; detach refused: %s\n' "$image_path" >&2
    return 1
  fi
  if ! current_device=$(find_exact_device "$image_path"); then
    printf 'exact image mapping is unavailable; detach refused: %s\n' "$image_path" >&2
    return 1
  fi
  if [[ "$current_device" != "$expected_device" ]]; then
    printf 'image device mapping changed; foreign detach refused: %s (%s)\n' "$image_path" "$current_device" >&2
    return 1
  fi
  if ! hdiutil detach "$expected_device"; then
    printf 'owned image detach failed: %s (%s)\n' "$image_path" "$expected_device" >&2
    return 1
  fi
}

detach_owned_images() {
  local index_value=0
  local failed=0
  for index_value in "${!owned_image_paths[@]}"; do
    if ! detach_owned_image "$index_value"; then
      failed=1
    fi
  done
  return "$failed"
}

replay_stderr() {
  local stderr_path=$1
  if [[ -s "$stderr_path" ]]; then
    cat "$stderr_path" >&2
  fi
}

publish_verified_dmg() {
  local source_path=$1
  local expected_id=$2
  run_formal_python "$script_dir/publish_macos_dmg.py" \
    --source "$source_path" \
    --destination "$dmg_path" \
    --expected-identity "$expected_id"
}

packvm_helper_relative='Contents/MacOS/tobkiri-packvm-vz-helper'
packvm_helper_identifier='dev.tobkiri.launcher.packvm-vz-helper'

verify_packvm_helper_signature() {
  local bundle_path=$1
  local helper_path="$bundle_path/$packvm_helper_relative"
  local helper_details=''

  [[ -f "$helper_path" && ! -L "$helper_path" ]] || {
    printf '%s\n' 'PackVM VZ helper is missing or unsafe' >&2
    return 1
  }
  codesign --verify --strict --all-architectures --verbose=2 "$helper_path"
  helper_details=$(codesign -d -r- --verbose=4 "$helper_path" 2>&1)
  grep -Fqx "Identifier=$packvm_helper_identifier" <<<"$helper_details" || {
    printf '%s\n' 'PackVM VZ helper has an unexpected identifier' >&2
    return 1
  }
  if grep -Fqx 'Signature=adhoc' <<<"$helper_details"; then
    grep -Eq '^# designated => cdhash H"[0-9a-fA-F]{40}"$' \
      <<<"$helper_details" || {
      printf '%s\n' 'PackVM VZ helper has an invalid ad-hoc requirement' >&2
      return 1
    }
  else
    grep -Fq "designated => identifier \"$packvm_helper_identifier\"" \
      <<<"$helper_details" || {
      printf '%s\n' 'PackVM VZ helper has an unexpected designated requirement' >&2
      return 1
    }
  fi
  codesign -d --entitlements :- "$helper_path" 2>/dev/null \
    | plutil -extract 'com\.apple\.security\.virtualization' raw -o - - \
    | grep -qx true || {
      printf '%s\n' 'PackVM VZ helper lacks the virtualization entitlement' >&2
      return 1
    }
}

app_bundle=$(cd "$app_bundle" && pwd -P)
mkdir -p "$output_dir"
output_dir=$(cd "$output_dir" && pwd -P)

app_name=$(basename "$app_bundle")
[[ "$app_name" == *.app ]] || {
  printf 'Expected a .app bundle, got: %s\n' "$app_name" >&2
  exit 1
}
app_stem=${app_name%.app}
version=$(plutil -extract CFBundleShortVersionString raw -o - "$app_bundle/Contents/Info.plist")
[[ -n "$version" && "$version" =~ ^[A-Za-z0-9][A-Za-z0-9.+_-]*$ ]] || {
  printf 'Tauri app bundle has an unsafe version for a DMG filename: %s\n' "$version" >&2
  exit 1
}

dmg_path="$output_dir/${app_stem}_${version}_${architecture_suffix}.dmg"
if [[ -e "$dmg_path" || -L "$dmg_path" ]]; then
  printf 'Refusing to overwrite existing macOS installer: %s\n' "$dmg_path" >&2
  exit 1
fi

verify_formal_python
script_dir=$(cd "$(dirname "$0")" && pwd -P)
workspace_identity=$(run_formal_python "$script_dir/cleanup_macos_dmg_workspace.py" create --parent "$output_dir")
IFS=$'\t' read -r work_dir work_device work_inode <<<"$workspace_identity"
[[ -n "$work_dir" && "$work_device" =~ ^[0-9]+$ && "$work_inode" =~ ^[0-9]+$ ]] || {
  printf '%s\n' 'DMG workspace helper returned an invalid ownership identity' >&2
  exit 1
}
staging_dir="$work_dir/staging"
image_dir="$work_dir/images"
mkdir "$staging_dir" "$image_dir"

cleanup() {
  local exit_status=$1
  local workspace_verified=0
  trap - EXIT HUP INT QUIT TERM
  if [[ -n "${work_dir:-}" && ( -e "$work_dir" || -L "$work_dir" ) ]]; then
    if run_formal_python "$script_dir/cleanup_macos_dmg_workspace.py" verify \
      --parent "$output_dir" --workspace "$work_dir" \
      --device "$work_device" --inode "$work_inode"; then
      workspace_verified=1
    else
      printf 'Temporary DMG workspace identity changed; cleanup refused: %s\n' "$work_dir" >&2
      if ((exit_status == 0)); then
        exit_status=1
      fi
    fi
    if ((workspace_verified == 1)); then
      if detach_owned_images; then
        if ! run_formal_python "$script_dir/cleanup_macos_dmg_workspace.py" cleanup \
          --parent "$output_dir" --workspace "$work_dir" \
          --device "$work_device" --inode "$work_inode"; then
          printf 'Could not remove temporary DMG workspace: %s\n' "$work_dir" >&2
          if ((exit_status == 0)); then
            exit_status=1
          fi
        fi
      else
        printf 'Temporary DMG image ownership could not be revalidated; workspace preserved: %s\n' "$work_dir" >&2
        if ((exit_status == 0)); then
          exit_status=1
        fi
      fi
    fi
  fi
  return "$exit_status"
}
trap 'cleanup "$?"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 131' QUIT
trap 'exit 143' TERM

printf 'Verifying signed app bundle: %s\n' "$app_bundle"
verify_packvm_helper_signature "$app_bundle"
run_formal_python "$script_dir/../../.github/scripts/macos_ci_artifact.py" \
  verify-packvm-bundle --app-bundle "$app_bundle"
codesign --verify --strict --all-architectures --verbose=2 "$app_bundle"
if [[ -n "$signing_identity" ]]; then
  signing_details=$(codesign --display --verbose=4 "$app_bundle" 2>&1)
  grep -Fqx "Authority=$signing_identity" <<<"$signing_details" || {
    printf '%s\n' 'macOS app is not signed by the requested Developer ID identity' >&2
    exit 1
  }
elif [[ -n "$ci_e2e_cert_sha256" ]]; then
  bundle_identifier=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app_bundle/Contents/Info.plist")
  [[ "$bundle_identifier" == 'dev.tobkiri.launcher.ci-e2e' ]] || {
    printf 'CI/E2E artifact has the wrong bundle identifier: %s\n' "$bundle_identifier" >&2
    exit 1
  }
  marker="$app_bundle/Contents/Resources/NON_PUBLISHABLE_CI_E2E_ARTIFACT.txt"
  [[ -f "$marker" && ! -L "$marker" ]] || {
    printf '%s\n' 'CI/E2E artifact is missing its signed non-publishable marker' >&2
    exit 1
  }
  artifact_policy="$app_bundle/Contents/Resources/ci-e2e-artifact-policy.v1.json"
  expected_policy="$script_dir/../src-tauri/ci-e2e/ci-e2e-artifact-policy.v1.json"
  [[ -f "$artifact_policy" && ! -L "$artifact_policy" ]] && cmp -s "$expected_policy" "$artifact_policy" || {
    printf '%s\n' 'CI/E2E artifact policy is missing or differs from its build domain' >&2
    exit 1
  }
  run_formal_python "$script_dir/../../.github/scripts/macos_ci_artifact.py" verify \
    --app-bundle "$app_bundle" --expected-certificate-sha256 "$ci_e2e_cert_sha256"
fi

printf 'Staging signed app bundle for DMG: %s\n' "$app_name"
ditto "$app_bundle" "$staging_dir/$app_name"
verify_packvm_helper_signature "$staging_dir/$app_name"
run_formal_python "$script_dir/../../.github/scripts/macos_ci_artifact.py" \
  verify-packvm-bundle --app-bundle "$staging_dir/$app_name"
codesign --verify --strict --all-architectures --verbose=2 "$staging_dir/$app_name"
ln -s /Applications "$staging_dir/Applications"

temporary_dmg_path="$image_dir/${app_stem}_${version}_${architecture_suffix}.dmg"
[[ ! -e "$temporary_dmg_path" && ! -L "$temporary_dmg_path" ]] || {
  printf '%s\n' 'temporary image path was not fresh' >&2
  exit 1
}
create_stderr="$work_dir/hdiutil-create.stderr"
create_status=0
printf 'Creating read-only UDZO installer: %s\n' "$temporary_dmg_path"
if hdiutil create \
  -srcfolder "$staging_dir" \
  -volname "$app_stem" \
  -fs APFS \
  -format UDZO \
  "$temporary_dmg_path" 2>"$create_stderr"; then
  replay_stderr "$create_stderr"
else
  create_status=$?
  replay_stderr "$create_stderr"
  record_failed_image_if_owned "$temporary_dmg_path"
  exit "$create_status"
fi

detached_image_identity=$(bound_image_metadata "$temporary_dmg_path") || {
  printf '%s\n' 'hdiutil result is not a detached regular file with a bound identity' >&2
  exit 1
}
current_id=''
if ! current_id=$(image_identity "$temporary_dmg_path") || \
  [[ "$current_id" != "$detached_image_identity" ]]; then
  printf 'detached image identity changed before size verification: %s\n' \
    "$temporary_dmg_path" >&2
  exit 1
fi
printf 'Verifying disk image integrity: %s\n' "$temporary_dmg_path"
if ! verify_bound_image "$temporary_dmg_path" "$detached_image_identity"; then
  printf 'detached image retained verification failed: %s\n' \
    "$temporary_dmg_path" >&2
  exit 1
fi
[[ -s "$temporary_dmg_path" ]] || {
  printf 'created an empty disk image: %s\n' "$temporary_dmg_path" >&2
  exit 1
}
publish_verified_dmg "$temporary_dmg_path" "$detached_image_identity"
printf 'Created verified macOS installer: %s\n' "$dmg_path"
