#!/usr/bin/env bash
# Build and stage the signed-at-package-time macOS PackVM VZ sidecar.
#
# The caller signs this executable after staging.  This script never stages a
# raw image, a Host channel key, or a guest signing key.  It installs only the
# immutable provisioning inputs that the Host verifier hashes again before use.
set -euo pipefail

readonly script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly launcher_dir="$(cd "${script_dir}/.." && pwd)"
readonly helper_dir="${launcher_dir}/packvm-vz-helper"
readonly provisioning_dir="${helper_dir}/Provisioning"
readonly guest_runner="${launcher_dir}/../tobkiri_runtime/ecosystem/defaultspack/backend/sandbox/isolation/resources/packvm_guest_runner.py"

target=""
app_bundle=""

while (($#)); do
  case "$1" in
    --target)
      target="${2:?missing value for --target}"
      shift 2
      ;;
    --app-bundle)
      app_bundle="${2:?missing value for --app-bundle}"
      shift 2
      ;;
    *)
      echo "usage: $0 --target aarch64-apple-darwin --app-bundle /path/Tobkiri\\ Launcher.app" >&2
      exit 64
      ;;
  esac
done

if [[ "${target}" != "aarch64-apple-darwin" ]]; then
  echo "packvm VZ sidecar only supports --target aarch64-apple-darwin" >&2
  exit 65
fi
if [[ -z "${app_bundle}" || ! -d "${app_bundle}/Contents/MacOS" ]]; then
  echo "--app-bundle must be an existing macOS .app bundle" >&2
  exit 66
fi

safe_directory() {
  local path="$1"
  if [[ -L "${path}" || ( -e "${path}" && ! -d "${path}" ) ]]; then
    echo "unsafe PackVM staging directory: ${path}" >&2
    exit 69
  fi
  if [[ ! -e "${path}" ]]; then
    mkdir "${path}"
  fi
}

safe_source_file() {
  local path="$1"
  if [[ ! -f "${path}" || -L "${path}" || "$(stat -f '%l' "${path}")" != "1" ]]; then
    echo "unsafe PackVM staging source: ${path}" >&2
    exit 70
  fi
}

safe_destination_file() {
  local path="$1"
  if [[ -L "${path}" || ( -e "${path}" && ( ! -f "${path}" || "$(stat -f '%l' "${path}")" != "1" ) ) ]]; then
    echo "unsafe PackVM staging destination: ${path}" >&2
    exit 71
  fi
}

# Refuse pre-existing link traversal before we create the one Resources child.
safe_directory "${app_bundle}"
safe_directory "${app_bundle}/Contents"
safe_directory "${app_bundle}/Contents/MacOS"
safe_directory "${app_bundle}/Contents/Resources"
safe_directory "${app_bundle}/Contents/Resources/packvm-vz-provisioning"
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "packvm VZ sidecar must be built on an arm64 macOS host" >&2
  exit 67
fi

(
  cd "${helper_dir}"
  swift build -c release --arch arm64
)

readonly source_binary="${helper_dir}/.build/arm64-apple-macosx/release/tobkiri-packvm-vz-helper"
readonly destination_binary="${app_bundle}/Contents/MacOS/tobkiri-packvm-vz-helper"
if [[ ! -f "${source_binary}" ]]; then
  echo "SwiftPM did not produce the PackVM VZ helper" >&2
  exit 68
fi
safe_source_file "${source_binary}"
safe_destination_file "${destination_binary}"

install -m 0755 "${source_binary}" "${destination_binary}"

readonly staged_inputs=(
  "image_descriptor:${provisioning_dir}/image_descriptor.v1.json:image_descriptor.v1.json"
  "bubblewrap_descriptor:${provisioning_dir}/bubblewrap_descriptor.v1.json:bubblewrap_descriptor.v1.json"
  "bubblewrap_package:${app_bundle}/Contents/Resources/packvm-vz-provisioning/bubblewrap_arm64.deb:bubblewrap_arm64.deb"
  "guest_runner:${guest_runner}:packvm_guest_runner.py"
  "guest_service_template:${provisioning_dir}/guest_service_template.v1.json:guest_service_template.v1.json"
  "cloud_init_template:${provisioning_dir}/cloud_init_template.yaml:cloud_init_template.yaml"
  "licenses:${provisioning_dir}/licenses.txt:licenses.txt"
)

readonly bubblewrap_destination="${app_bundle}/Contents/Resources/packvm-vz-provisioning/bubblewrap_arm64.deb"
safe_destination_file "${bubblewrap_destination}"
# The descriptor drives the download policy, so reject links and multiply
# linked source files before parsing it rather than only at the later copy.
safe_source_file "${provisioning_dir}/bubblewrap_descriptor.v1.json"
readonly bubblewrap_temporary="${bubblewrap_destination}.tmp-$$"
trap 'rm -f "${bubblewrap_temporary}"' EXIT
bubblewrap_metadata="$(python3 - "${provisioning_dir}/bubblewrap_descriptor.v1.json" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

descriptor = json.loads(Path(sys.argv[1]).read_bytes())
if (
    not isinstance(descriptor, dict)
    or set(descriptor) != {"schema", "package", "version", "architecture", "source"}
    or descriptor["schema"] != "io.tobkiri.packvm-vz-bubblewrap-descriptor.v1"
    or descriptor["package"] != "bubblewrap"
    or descriptor["version"] != "0.11.0-2+deb13u1"
    or descriptor["architecture"] != "arm64"
    or not isinstance(descriptor["source"], dict)
    or set(descriptor["source"]) != {"url", "size_bytes", "sha256"}
):
    raise SystemExit("invalid PackVM bubblewrap descriptor")
source = descriptor["source"]
if (
    not isinstance(source["url"], str)
    or not source["url"].startswith("https://")
    or not isinstance(source["size_bytes"], int)
    or isinstance(source["size_bytes"], bool)
    or source["size_bytes"] <= 0
    or not isinstance(source["sha256"], str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", source["sha256"]) is None
):
    raise SystemExit("invalid PackVM bubblewrap source")
print(source["url"], source["size_bytes"], source["sha256"], sep="\t")
PY
 )"
IFS=$'\t' read -r bubblewrap_url bubblewrap_bytes bubblewrap_sha256 <<<"${bubblewrap_metadata}"
if [[ -z "${bubblewrap_url}" || -z "${bubblewrap_bytes}" || -z "${bubblewrap_sha256}" ]]; then
  echo "PackVM bubblewrap descriptor did not produce an immutable source" >&2
  exit 72
fi
curl --fail --silent --show-error --proto '=https' --max-redirs 0 --tlsv1.2 \
  "${bubblewrap_url}" \
  --output "${bubblewrap_temporary}"
if [[ "$(stat -f '%z' "${bubblewrap_temporary}")" != "${bubblewrap_bytes}" \
  || "sha256:$(shasum -a 256 "${bubblewrap_temporary}" | awk '{print $1}')" != "${bubblewrap_sha256}" ]]; then
  echo "PackVM bubblewrap package did not match its pinned identity" >&2
  exit 72
fi
chmod 0444 "${bubblewrap_temporary}"
mv -f "${bubblewrap_temporary}" "${bubblewrap_destination}"
safe_source_file "${bubblewrap_destination}"
trap - EXIT

for entry in "${staged_inputs[@]}"; do
  IFS=: read -r _name source destination <<<"${entry}"
  # The pinned package was atomically downloaded above; every other input is
  # copied from its source after no-follow/single-link validation.
  if [[ "${source}" == "${bubblewrap_destination}" ]]; then
    continue
  fi
  safe_source_file "${source}"
  safe_destination_file "${app_bundle}/Contents/Resources/packvm-vz-provisioning/${destination}"
  install -m 0444 "${source}" "${app_bundle}/Contents/Resources/packvm-vz-provisioning/${destination}"
done

readonly manifest_path="${app_bundle}/Contents/Resources/packvm-vz-provisioning.v1.json"
safe_destination_file "${manifest_path}"
python3 - "${app_bundle}" "${manifest_path}" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

bundle = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
resources = bundle / "Contents" / "Resources"
inputs = (
    ("image_descriptor", "image_descriptor.v1.json"),
    ("bubblewrap_descriptor", "bubblewrap_descriptor.v1.json"),
    ("bubblewrap_package", "bubblewrap_arm64.deb"),
    ("guest_runner", "packvm_guest_runner.py"),
    ("guest_service_template", "guest_service_template.v1.json"),
    ("cloud_init_template", "cloud_init_template.yaml"),
    ("licenses", "licenses.txt"),
)
entries: list[dict[str, str]] = []
for name, filename in inputs:
    relative = Path("packvm-vz-provisioning") / filename
    path = resources / relative
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SystemExit(f"unsafe staged PackVM provisioning input: {relative}")
    entries.append(
        {
            "name": name,
            "path": relative.as_posix(),
            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )
runner = resources / "packvm-vz-provisioning" / "packvm_guest_runner.py"
service_template = resources / "packvm-vz-provisioning" / "guest_service_template.v1.json"
service = json.loads(service_template.read_bytes())
if not isinstance(service, dict) or set(service) != {
    "schema", "protocol", "guest_runner_sha256", "service_unit"
}:
    raise SystemExit("guest service template has an invalid schema")
service["guest_runner_sha256"] = "sha256:" + hashlib.sha256(runner.read_bytes()).hexdigest()
service_template.chmod(0o600)
service_template.write_text(
    json.dumps(service, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
service_template.chmod(0o444)
# Hash only after the staged template has been rebound to the exact canonical
# runner bytes copied into this bundle.
entries = []
for name, filename in inputs:
    relative = Path("packvm-vz-provisioning") / filename
    path = resources / relative
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SystemExit(f"unsafe staged PackVM provisioning input: {relative}")
    entries.append(
        {
            "name": name,
            "path": relative.as_posix(),
            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )
document = {
    "schema": "io.tobkiri.packvm-vz-provisioning.v1",
    "target": "aarch64-apple-darwin",
    "boot_mode": "efi",
    "inputs": entries,
}
temporary = manifest_path.with_name(f".{manifest_path.name}.tmp-{os.getpid()}")
temporary.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
temporary.chmod(0o444)
os.replace(temporary, manifest_path)
PY
