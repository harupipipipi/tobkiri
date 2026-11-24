"""Select the thirteen original producer assets without copying build trees."""
import json
import os
from pathlib import Path
import stat

from build_native import fingerprint
from packager import package

ADAPTER_SHA256 = "a97cd69d0efda7e6edc69bccf78382958c9b3480cfc08e041a5fd84ff54ba0d6"
RUNTIME_PATH = "wasm32-wasip2/release/componentize_py_runtime.wasm"


def selected(source: Path, wasi: Path, runtimes: Path) -> dict:
    """Map fixed original input locations to their private packaging namespace."""
    sdk = wasi / "sdk-input/sdk/share/wasi-sysroot/lib/wasm32-wasip2"
    result = {
        f"libcomponentize_py_runtime_{variant}.so.zst": (
            runtimes / f"work/runtime-{variant}" / RUNTIME_PATH,
            f"runtime-{variant}.wasm", None)
        for variant in ("sync", "async")
    }
    for name in ("libc.so", "libwasi-emulated-mman.so",
                 "libwasi-emulated-process-clocks.so", "libwasi-emulated-getpid.so",
                 "libwasi-emulated-signal.so", "libc++.so", "libc++abi.so"):
        path = sdk / "noeh" / name if name in ("libc++.so", "libc++abi.so") else sdk / name
        result[name + ".zst"] = (path, name, None)
    result.update({
        "libpython3.13.so.zst": (wasi / "work/wasi/libpython3.13.so",
                                  "libpython3.13.so", None),
        "wasi_snapshot_preview1.reactor.wasm.zst": (
            source / "componentize-py/adapters/ab5a4484/wasi_snapshot_preview1.reactor.wasm",
            "reactor.wasm", ADAPTER_SHA256),
        "python-lib.tar.zst": (wasi / "work/wasi/install/lib/python3.13", "stdlib", None),
        "bundled.tar.zst": (source / "componentize-py/bundled", "bundled", None),
    })
    return result


def manifest(inputs: dict) -> tuple:
    """Hash every included regular file and retain original inclusion semantics."""
    if set(inputs) != package.ASSETS:
        raise ValueError("all thirteen original assets must be selected")
    records, mounts, identities = [], [], {}
    total = 0

    def append(asset, path, logical, member):
        nonlocal total
        package.safe_name(logical)
        if member is not None:
            package.safe_name(member)
        if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("only physical regular asset files are accepted")
        identity = fingerprint(path, package.MAX_FILE)
        total += identity["bytes"]
        if total > package.MAX_TOTAL or len(records) >= package.MAX_RECORDS:
            raise ValueError("asset input aggregate bound exceeded")
        records.append({"asset": asset, "path": logical, "member": member,
                        "sha256": identity["sha256"],
                        "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644})
        identities[str(path)] = identity
        return identity

    for asset, (path, logical, pin) in sorted(inputs.items()):
        package.safe_name(logical)
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("physical absolute asset root required")
        mounts.append((path, "/inputs/" + logical))
        if asset in package.BINARY_ASSETS:
            identity = append(asset, path, logical, None)
            if pin is not None and identity["sha256"] != pin:
                raise ValueError("original adapter identity changed")
            continue
        if not path.is_dir():
            raise ValueError("original tar input directory missing")
        before = len(records)
        scanned = 0
        for parent, directories, files in os.walk(path, followlinks=False):
            scanned += len(directories) + len(files)
            if scanned > 50000:
                raise ValueError("tar traversal bound exceeded")
            for name in directories + files:
                candidate = Path(parent) / name
                mode = candidate.lstat().st_mode
                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise ValueError("linked or special tar input forbidden")
            for name in sorted(files):
                candidate = Path(parent) / name
                if candidate.suffix in (".a", ".pyc", ".whl") or name in (
                        "Makefile", "Changelog", "NEWS.txt"):
                    continue
                member = candidate.relative_to(path).as_posix()
                append(asset, candidate, logical + "/" + member, member)
        if len(records) == before:
            raise ValueError("original tar asset cannot be empty")
    records.sort(key=lambda record: (record["asset"], record["path"]))
    encoded = (json.dumps({"schema_version": 1, "files": records},
                          indent=2, sort_keys=True) + "\n").encode()
    if len(encoded) > package.MAX_MANIFEST:
        raise ValueError("asset manifest bound exceeded")
    return encoded, tuple(mounts), identities
