#!/usr/bin/env python3
"""CI-only pinned Linux producer. Never download, extract, or replace module pins."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import sys
import tarfile
from typing import Any

CHUNK = 64 * 1024

def digest(path: Path) -> str:
    """Hash a file with bounded input buffers."""
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(CHUNK):
            result.update(block)
    return result.hexdigest()

def require(condition: bool, message: str) -> None:
    """Reject an unverified producer input."""
    if not condition:
        raise RuntimeError(message)

def verify_tree(archive: Path, sdk: Path, profile: dict[str, Any]) -> None:
    """After whole-archive authentication, compare every SDK object in place."""
    expected, members, prefix = set(), 0, profile["sdk_root"]
    with tarfile.open(archive, "r|gz", bufsize=CHUNK) as stream:
        while member := stream.next():
            members += 1
            require(members <= 100_000, "SDK member bound")
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and ".." not in name.parts and len(str(name).encode()) <= 512,
                    "SDK archive path outside closed root")
            require(name.parts and name.parts[0] == prefix, "SDK root mismatch")
            relative = Path(*name.parts[1:])
            if not name.parts[1:]:
                require(member.isdir(), "SDK root must be a directory")
                stream.members.clear()
                continue
            require(relative.as_posix() not in expected, "duplicate SDK object")
            expected.add(relative.as_posix())
            actual = sdk / relative
            require(actual.resolve().is_relative_to(sdk), "SDK path escaped physical root")
            mode = actual.lstat().st_mode
            if member.isdir():
                require(stat.S_ISDIR(mode), "SDK directory kind mismatch")
            elif member.issym():
                require(stat.S_ISLNK(mode) and os.readlink(actual) == member.linkname,
                        "SDK symlink identity mismatch")
            elif member.isfile() or member.islnk():
                require(stat.S_ISREG(mode) and member.size <= 512 * 1024**2,
                        "SDK regular file kind/size bound")
                # Hardlinks share already authenticated bytes of their target.
                if member.islnk():
                    target = PurePosixPath(member.linkname)
                    require(not target.is_absolute() and bool(target.parts) and target.parts[0] == prefix and ".." not in target.parts,
                            "SDK hardlink target outside root")
                    require(os.path.samefile(actual,sdk.joinpath(*target.parts[1:])), "SDK hardlink identity mismatch")
                else:
                    require(actual.stat().st_size == member.size, "SDK file size mismatch")
                    archived = hashlib.sha256()
                    source = stream.extractfile(member)
                    require(source is not None, "SDK archive file unavailable")
                    while block := source.read(CHUNK):
                        archived.update(block)
                    require(digest(actual) == archived.hexdigest(), "SDK file digest mismatch")
            else:
                raise RuntimeError("unsupported SDK archive object")
            stream.members.clear()
    found = set()
    for directory, directories, files in os.walk(sdk, followlinks=False):
        for name in directories + files:
            found.add((Path(directory) / name).relative_to(sdk).as_posix())
            require(len(found) <= 100_000, "SDK filesystem member bound")
    require(found == expected, "SDK extra/missing object closure mismatch")

def main() -> None:
    """Validate the exact Linux SDK and run only the pinned private producer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive",type=Path,required=True)
    parser.add_argument("--sdk",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--disk-floor-bytes",type=int,default=5905580032)
    args = parser.parse_args()
    require(platform.system() == "Linux" and platform.machine() in ("x86_64","amd64"),
            "Linux x86_64 producer execution is unavailable on this host")
    root = Path(__file__).resolve().parent
    pin_path = root / "host-toolchain-sdk34-x86_64-linux.json"
    profile = json.loads(pin_path.read_text())
    require(digest(Path(__file__).resolve()) == profile["producer_script_sha256"], "Linux entry source pin mismatch")
    require(digest(root / "build.py") == profile["base_producer_sha256"], "base producer source pin mismatch")
    require(args.archive.stat().st_size == profile["archive_bytes"] and digest(args.archive) == profile["archive_sha256"],
            "whole SDK archive pin mismatch")
    sdk = args.sdk.resolve(strict=True)
    verify_tree(args.archive,sdk,profile)
    for tool in profile["tools"].values():
        require((sdk/tool["requested"]).resolve() == sdk/tool["resolved"], "SDK tool closed target mismatch")
        require(digest(sdk/tool["resolved"]) == tool["sha256"], "SDK tool digest mismatch")
    for name,entry in profile["native_libraries"].items():
        actual = sdk/name
        if entry["kind"] == "file":
            require(digest(actual) == entry["sha256"], "SDK native library digest mismatch")
        else:
            require(actual.resolve().is_relative_to(sdk), "SDK library link escaped root")
    # Trusted CI host dependencies remain host dependencies; the full SDK tree
    # and exact output digest are pinned. Do not inherit tool injection settings.
    for name in ("LD_PRELOAD","LD_LIBRARY_PATH","LD_AUDIT","CPATH","C_INCLUDE_PATH","CPLUS_INCLUDE_PATH",
                 "LIBRARY_PATH","COMPILER_PATH","GCC_EXEC_PREFIX","CCC_OVERRIDE_OPTIONS"):
        os.environ.pop(name,None)
    spec = importlib.util.spec_from_file_location("tobkiri_pinned_sql_producer",root/"build.py")
    require(spec is not None and spec.loader is not None, "base producer unavailable")
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    producer.SDK_SHA = profile["archive_sha256"]
    producer.CLANG_SHA = profile["compiler_sha256"]
    producer.LINKER_SHA = profile["linker_sha256"]
    producer.LIBC_SHA = profile["libc_sha256"]
    sys.argv = [str(root/"build.py"),"--sdk",str(sdk),"--out",str(args.out),
                "--disk-floor-bytes",str(args.disk_floor_bytes),"--max-additional-disk-bytes",str(64*1024**2)]
    producer.main()
    module = args.out/"sqlite-core.wasm"
    require(module.stat().st_size == profile["required_module_bytes"] and digest(module) == profile["required_module_sha256"],
            "Linux module differs from approved module; do not refresh pin automatically")
    receipt_path = args.out/"provenance.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update({"host_toolchain_profile":profile["profile"],"host_entry_sha256":profile["producer_script_sha256"],
                    "host_pin_sha256":digest(pin_path),"host_sdk_tree_verified":True})
    receipt_path.write_text(json.dumps(receipt,indent=2)+"\n")

if __name__ == "__main__":
    main()
