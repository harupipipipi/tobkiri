"""Build an inactive exact-source libc candidate in a supervised Linux CI jail.

The SDK/source inputs remain immutable. This does not select producer assets,
build the C ABI extension, run a guest, or establish product/UI parity.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from build_native import GIB, fingerprint, sandbox_command
from build_wasi import run_stage
import fetch_sdk
import fetch_ui_poll_libc
import fetch_ui_poll_tools
import ui_empty_poll_source

RECIPE = r"""set -eu
umask 077
trap 'poll_build_status=$?; if [ "$poll_build_status" -ne 0 ]; then /usr/bin/tail -c 8192 /work/configure.log /work/compile.log 2>/dev/null || :; fi; exit "$poll_build_status"' 0
export PATH=/sdk/bin:/usr/bin:/bin
/sdk/bin/clang --version > /work/clang-version.txt
/build-tools/cmake/cmake/data/bin/cmake --version > /work/cmake-version.txt
/build-tools/wasm-tools --version > /work/wasm-tools-version.txt
/usr/bin/make --version > /work/make-version.txt
poll_builtins_p1=$(/sdk/bin/clang --target=wasm32-wasip1 --print-libgcc-file-name)
poll_builtins_p2=$(/sdk/bin/clang --target=wasm32-wasip2 --print-libgcc-file-name)
/usr/bin/python3 - "$poll_builtins_p1" "$poll_builtins_p2" <<'PY'
from pathlib import Path
import hashlib,json,sys
records=[]
for value in sys.argv[1:]:
    path=Path(value).resolve(strict=True)
    if not path.is_relative_to('/sdk') or not path.is_file():
        raise ValueError('compiler-rt is outside the verified SDK')
    records.append({'path':str(path),'bytes':path.stat().st_size,
                    'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
if records[0]['sha256']!=records[1]['sha256']:
    raise ValueError('SDK33 compiler-rt aliases differ from the SDK recipe')
Path('/work/builtins.json').write_text(json.dumps(records,indent=2)+'\n')
PY
# Pass the SDK's own wasip1 compiler-rt exactly as the SDK33 libc recipe does.
# Never permit the upstream CMake fallback to acquire SDK25 builtins.
/build-tools/cmake/cmake/data/bin/cmake -S /libc-source -B /work/cmake \
  -G 'Unix Makefiles' \
  -DCMAKE_MAKE_PROGRAM=/usr/bin/make \
  -DCMAKE_C_COMPILER=/sdk/bin/clang \
  -DCMAKE_ASM_COMPILER=/sdk/bin/clang \
  -DCMAKE_C_COMPILER_WORKS=ON \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  '-DCMAKE_C_FLAGS=-mcpu=lime1 -fPIC' \
  '-DCMAKE_ASM_FLAGS=-mcpu=lime1 -fPIC' \
  -DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF \
  -DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF \
  -DTARGET_TRIPLE=wasm32-wasip2 \
  -DMALLOC=dlmalloc -DLTO= -DBUILD_SHARED=ON -DBUILD_TESTS=OFF \
  -DBINDINGS_TARGET=OFF -DUSE_WASM_COMPONENT_LD=OFF \
  -DWASM_TOOLS_EXECUTABLE=/build-tools/wasm-tools \
  -DWASI_SDK_VERSION=33.0 \
  "-DBUILTINS_LIB=$poll_builtins_p1" > /work/configure.log 2>&1
/build-tools/cmake/cmake/data/bin/cmake --build /work/cmake --target c \
  --parallel 1 --verbose > /work/compile.log 2>&1
/usr/bin/python3 - <<'PY'
from pathlib import Path
for path in (Path('/work/cmake/libc.so'),
             Path('/sdk/share/wasi-sysroot/lib/wasm32-wasip2/libc.so')):
    with path.open('rb') as stream:
        if stream.read(8)!=b'\0asm\1\0\0\0':
            raise ValueError('libc result is not a core Wasm module')
PY
"""


def physical(path: Path) -> None:
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError("physical absolute source/tool directory required")


def json_file(path: Path) -> dict:
    fingerprint(path, 4 * 1024 * 1024)
    result = json.loads(path.read_bytes())
    if not isinstance(result, dict):
        raise ValueError("structured libc input mapping required")
    return result


def run(source: Path, tools: Path, destination: Path) -> dict:
    """Build only from independently verified, separate immutable inputs."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("libc building is restricted to Linux CI")
    physical(source)
    physical(tools)
    if (
        not destination.is_absolute()
        or not destination.parent.is_dir()
        or destination.parent.resolve() != destination.parent
        or os.path.lexists(destination)
        or shutil.disk_usage(destination.parent).free < 8 * GIB
    ):
        raise ValueError(
            "new private libc build directory and original disk floor required"
        )
    fetch_ui_poll_libc.verify(source)
    source_report = json_file(source / "source-inputs.json")
    tool_report = fetch_ui_poll_tools.verify(tools)
    predecessors = {
        "source": fingerprint(source / "source-inputs.json"),
        "tools": fingerprint(tools / "tool-inputs.json"),
    }
    original_source = source / "source"
    original_inventory = fetch_sdk.inventory(original_source)
    original_tools = fetch_sdk.inventory(tools)
    destination.mkdir(mode=0o700)
    sdk, sdk_inventory = fetch_sdk.acquire(destination / "sdk-input")
    candidate_source = destination / "candidate-source"
    shutil.copytree(original_source, candidate_source, symlinks=False)
    poll = candidate_source / ui_empty_poll_source.POLL_PATH
    candidate, derivation = ui_empty_poll_source.derive(poll.read_bytes())
    poll.write_bytes(candidate)
    candidate_inventory = fetch_sdk.inventory(candidate_source)
    changed = [
        name
        for name in original_inventory
        if original_inventory[name] != candidate_inventory.get(name)
    ]
    if set(candidate_inventory) != set(original_inventory) or changed != [
        ui_empty_poll_source.POLL_PATH
    ]:
        raise ValueError("candidate source changed beyond the exact poll insertion")
    # The actual pinned SDK33 archive records VERSION at its distribution root.
    sdk_version = sdk / "VERSION"
    fingerprint(sdk_version, 64 * 1024)
    version_bytes = sdk_version.read_bytes()
    work = destination / "work"
    work.mkdir(mode=0o700)
    (work / "home").mkdir(mode=0o700)
    recipe = destination / "build-libc.sh"
    recipe.write_text(RECIPE)
    report = {
        "format": "tobkiri.ui-empty-poll-libc-build/1",
        "outcome": "pending",
        "predecessors": predecessors,
        "source_pin": source_report["source_pin"],
        "derivation": derivation,
        "build_recipe": fingerprint(recipe),
        "sdk_archive": {"bytes": fetch_sdk.ARCHIVE_BYTES, "sha256": fetch_sdk.SHA256},
        "sdk_manifest": fingerprint(destination / "sdk-input/sdk-manifest.json"),
        "sdk_version_file": {
            "identity": fingerprint(sdk_version),
            "text": version_bytes.decode("utf-8"),
        },
        "wasm_tools_version": tool_report["version"],
        "cmake_version": tool_report["cmake"]["version"],
        "build_performed": False,
        "c_abi_verified": False,
        "cold_schema_verified": False,
        "production_enabled": False,
    }
    receipt_path = destination / "libc-build.json"
    try:
        command = sandbox_command(
            None,
            work,
            recipe,
            (
                (sdk, "/sdk"),
                (candidate_source, "/libc-source"),
                (tools, "/build-tools"),
            ),
        )
        marker = command.index("--")
        command[marker:marker] = ["--setenv", "HOME", "/work/home"]
        report["supervision"] = run_stage(
            "ui-empty-poll-libc", command, work, destination / "supervision"
        )
        output = work / "cmake/libc.so"
        original_libc = sdk / "share/wasi-sysroot/lib/wasm32-wasip2/libc.so"
        report["original_libc"] = fingerprint(original_libc, 32 * 1024 * 1024)
        report["candidate_libc"] = {
            "path": "work/cmake/libc.so",
            **fingerprint(output, 32 * 1024 * 1024),
        }
        fingerprint(work / "builtins.json", 64 * 1024)
        report["builtins"] = json.loads((work / "builtins.json").read_bytes())
        report["tools_observed"] = {
            name: (work / (name + "-version.txt")).read_text()[:8192]
            for name in ("clang", "cmake", "wasm-tools", "make")
        }
        report["build_performed"] = True
        report["outcome"] = "success"
    except BaseException as error:
        report["outcome"] = "failed"
        report["failure_type"] = type(error).__name__
        raise
    finally:
        report["inputs_unchanged"] = (
            fetch_sdk.inventory(sdk) == sdk_inventory
            and fetch_sdk.inventory(original_source) == original_inventory
            and fetch_sdk.inventory(candidate_source) == candidate_inventory
            and fetch_sdk.inventory(tools) == original_tools
            and fingerprint(source / "source-inputs.json") == predecessors["source"]
            and fingerprint(tools / "tool-inputs.json") == predecessors["tools"]
        )
        if not report["inputs_unchanged"]:
            report["outcome"] = "failed"
        receipt_path.write_text(json.dumps(report, indent=2) + "\n")
    if not report["inputs_unchanged"]:
        raise ValueError("immutable libc source, SDK or build tools changed")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "tools", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = run(args.source, args.tools, args.destination)
    print(
        json.dumps(
            {
                "outcome": result["outcome"],
                "c_abi_verified": False,
                "cold_schema_verified": False,
                "production_enabled": False,
            }
        )
    )
