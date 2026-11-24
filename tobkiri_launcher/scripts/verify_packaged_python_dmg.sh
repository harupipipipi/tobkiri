#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  printf '%s\n' 'Usage: verify_packaged_python_dmg.sh --dmg PATH --target TARGET --expected-manifest-sha256 SHA256' >&2
}

dmg=''
target=''
expected_manifest=''
while (($# > 0)); do
  case "$1" in
    --dmg) (($# >= 2)) || { usage; exit 2; }; dmg=$2; shift 2 ;;
    --target) (($# >= 2)) || { usage; exit 2; }; target=$2; shift 2 ;;
    --expected-manifest-sha256)
      (($# >= 2)) || { usage; exit 2; }
      expected_manifest=$2
      shift 2
      ;;
    *) usage; exit 2 ;;
  esac
done

[[ -f "$dmg" && -n "$target" && "$expected_manifest" =~ ^[0-9a-f]{64}$ ]] || {
  usage
  exit 2
}
script_dir=$(cd "$(dirname "$0")" && pwd -P)
repo_root=$(cd "$script_dir/../.." && pwd -P)
/usr/bin/python3 -B "$script_dir/verify_packaged_python_dmg.py" \
  --dmg "$dmg" \
  --repo-root "$repo_root" \
  --target "$target" \
  --expected-manifest-sha256 "$expected_manifest"
