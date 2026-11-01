#!/usr/bin/env python3
"""Produce and inspect the pinned private SQLite core Wasm; never download tools."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import signal
import time

SOURCE_SHA = "0a409f1633283fa31a9126b11fbfd64a1991c5d30defad07e5745d4667f5e23d"
SDK_SHA = "9c59398106b417f8f14913380fdf0097a8cc0ff4af9eb3ce0065a859e88d49e9"
CLANG_SHA = "19b9ef777eec50d9a353060db546963f588fc10b13baff7839a54d0d97bf871c"
LINKER_SHA = "f3f36480cec4c76cd996e7e7624c811d18f92d63dc81fd224c0f757a7f4c3377"
LIBC_SHA = "a9aff53312240258085fd94da05e18545f4c98b2f6a91ce4985b519eb8159fb9"
FLOOR = 5 * 1024**3
EXPORTS = {
    "tobkiri_abi_version": ([], ["i32"]),
    "tobkiri_init": (["i32", "i64", "i64"], ["i32"]),
    "tobkiri_context": (["i64", "i64"], []),
    "tobkiri_alloc": (["i32"], ["i32"]),
    "tobkiri_free": (["i32"], []),
    "tobkiri_open": (["i32"], ["i32"]),
    "tobkiri_close": ([], ["i32"]),
    "tobkiri_prepare": (["i32"] * 4, ["i32"]),
    "tobkiri_stmt_readonly": ([], ["i32"]),
    "tobkiri_stmt_generation": ([], ["i64"]),
    "tobkiri_shm_calls": ([], ["i32"]),
    "tobkiri_bind_null": (["i32"], ["i32"]),
    "tobkiri_bind_integer": (["i32", "i64"], ["i32"]),
    "tobkiri_bind_real": (["i32", "f64"], ["i32"]),
    "tobkiri_bind_text": (["i32"] * 3, ["i32"]),
    "tobkiri_bind_blob": (["i32"] * 3, ["i32"]),
    "tobkiri_step": ([], ["i32"]),
    "tobkiri_column_count": ([], ["i32"]),
    "tobkiri_column_name": (["i32"], ["i32"]),
    "tobkiri_column_type": (["i32"], ["i32"]),
    "tobkiri_column_bytes": (["i32"], ["i32"]),
    "tobkiri_column_text": (["i32"], ["i32"]),
    "tobkiri_column_blob": (["i32"], ["i32"]),
    "tobkiri_column_integer": (["i32"], ["i64"]),
    "tobkiri_column_real": (["i32"], ["f64"]),
    "tobkiri_changes": ([], ["i64"]),
    "tobkiri_last_rowid": ([], ["i64"]),
    "tobkiri_finish": ([], ["i32"]),
    "tobkiri_autocommit": ([], ["i32"]),
    "tobkiri_control": (["i32"], ["i32"]),
    "tobkiri_error_code": ([], ["i32"]),
}
IMPORTS = {
    ("tobkiri-sql", "broker"): (["i32"] * 4 + ["i64"], ["i64"]),
    ("tobkiri-sql", "check"): ([], ["i32"]),
}
FLAGS = [
    "--target=wasm32-wasip1", "-O2", "-DNDEBUG", "-nostartfiles",
    "-DSQLITE_OS_OTHER=1", "-DSQLITE_THREADSAFE=0", "-DSQLITE_ENABLE_MEMSYS5=1",
    "-DSQLITE_OMIT_LOAD_EXTENSION=1", "-DSQLITE_OMIT_SHARED_CACHE=1",
    "-DSQLITE_OMIT_AUTOINIT=1", "-DSQLITE_OMIT_LOCALTIME=1", "-DSQLITE_USE_URI=0",
    "-DSQLITE_OMIT_AUTORESET=1",
    "-DSQLITE_TEMP_STORE=3", "-DSQLITE_DEFAULT_CACHE_SIZE=-512",
    "-DSQLITE_DEFAULT_PAGE_SIZE=4096", "-DSQLITE_DEFAULT_MEMSTATUS=0",
    "-DLONGDOUBLE_TYPE=double", "-Wl,--no-entry", "-Wl,--strip-all",
    "-Wl,--stack-first", "-Wl,-z,stack-size=262144",
    "-Wl,--initial-memory=16777216", "-Wl,--max-memory=16777216",
    "-Wl,--export-memory",
]


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                return value.hexdigest()
            value.update(block)


class Reader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def byte(self):
        if self.offset >= len(self.data):
            raise ValueError("truncated Wasm")
        value = self.data[self.offset]
        self.offset += 1
        return value

    def leb(self):
        value, shift = 0, 0
        while shift < 35:
            byte = self.byte()
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
            shift += 7
        raise ValueError("oversized Wasm integer")

    def block(self):
        length = self.leb()
        end = self.offset + length
        if end > len(self.data):
            raise ValueError("truncated Wasm section")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def name(self):
        return self.block().decode("utf-8")

    def limits(self):
        flags = self.leb()
        if flags not in (0, 1):
            raise ValueError("shared/memory64 ABI denied")
        initial = self.leb()
        maximum = self.leb() if flags else None
        return {"initial": initial, "maximum": maximum}


def inspect(data):
    if data[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("expected core Wasm1 module")
    reader = Reader(data[8:])
    types, imports, functions, exports, memories, tables = [], [], [], {}, [], []
    names = {0x7f: "i32", 0x7e: "i64", 0x7d: "f32", 0x7c: "f64"}
    while reader.offset < len(reader.data):
        section = reader.byte()
        content = Reader(reader.block())
        if section == 1:
            for _ in range(content.leb()):
                if content.byte() != 0x60:
                    raise ValueError("non-numeric ABI type")
                params = [names[content.byte()] for _ in range(content.leb())]
                results = [names[content.byte()] for _ in range(content.leb())]
                types.append((params, results))
        elif section == 2:
            for _ in range(content.leb()):
                module, name = content.name(), content.name()
                if content.byte() != 0:
                    raise ValueError("non-function import denied")
                imports.append((module, name, content.leb()))
        elif section == 3:
            functions = [content.leb() for _ in range(content.leb())]
        elif section == 4:
            for _ in range(content.leb()):
                if content.byte() != 0x70:
                    raise ValueError("non-funcref table denied")
                tables.append(content.limits())
        elif section == 5:
            memories = [content.limits() for _ in range(content.leb())]
        elif section == 7:
            for _ in range(content.leb()):
                name = content.name()
                if name in exports:
                    raise ValueError("duplicate export")
                exports[name] = (content.byte(), content.leb())
        elif section == 8:
            raise ValueError("implicit start function denied")
    actual_imports = {(module, name): types[index] for module, name, index in imports}
    if actual_imports != IMPORTS or len(imports) != len(IMPORTS):
        raise ValueError("private imports mismatch: " + repr(actual_imports))
    if set(exports) != set(EXPORTS) | {"memory"}:
        raise ValueError("private exports mismatch: " + repr(sorted(exports)))
    function_types = [item[2] for item in imports] + functions
    for name, expected in EXPORTS.items():
        kind, index = exports[name]
        if kind != 0 or types[function_types[index]] != expected:
            raise ValueError("private signature mismatch: " + name)
    if exports["memory"] != (2, 0) or memories != [{"initial": 256, "maximum": 256}]:
        raise ValueError("linear memory must be fixed16MiB")
    if len(tables) > 1 or any(t["maximum"] is None or t["maximum"] > 1024 for t in tables):
        raise ValueError("unbounded/excessive private table")
    return {"abi_version": 1, "imports": sorted("/".join(k) for k in actual_imports),
            "exports": sorted(exports), "linear_memories": memories, "tables": tables,
            "stack_bytes": 262144, "sqlite_heap_capacity_bytes": 4194304}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    parser.add_argument("--sdk", type=Path, default=workspace / ".work/toolchains/wasi-sdk-34.0-arm64-macos")
    parser.add_argument("--out", type=Path, default=workspace / ".work/sqlite-core")
    parser.add_argument("--disk-floor-bytes", type=int, default=FLOOR)
    parser.add_argument("--max-additional-disk-bytes", type=int, default=64 * 1024**2)
    args = parser.parse_args()
    for path, expected in [(root / "vendor/sqlite3.c", SOURCE_SHA),
                           (args.sdk / "bin/clang", CLANG_SHA),
                           (args.sdk / "bin/wasm-ld", LINKER_SHA),
                           (args.sdk / "share/wasi-sysroot/lib/wasm32-wasip1/libc.a", LIBC_SHA)]:
        if digest(path) != expected:
            raise ValueError("pinned source/tool hash mismatch: " + str(path))
    args.out.mkdir(parents=True, exist_ok=True)
    if args.disk_floor_bytes < FLOOR or args.max_additional_disk_bytes > 64 * 1024**2 or args.max_additional_disk_bytes <= 0:
        raise RuntimeError("producer disk bound cannot be widened")
    initial_free = shutil.disk_usage(args.out).free
    if initial_free < args.disk_floor_bytes + args.max_additional_disk_bytes:
        raise RuntimeError("producer would violate its disk floor")
    source_paths = [root / "private_sql.c", root / "build.py", root / "vendor/sqlite3.c", root / "vendor/sqlite3.h"]
    source_before = {str(path.relative_to(root)): digest(path) for path in source_paths}
    module = args.out / "sqlite-core.wasm"
    flags = FLAGS + ["-ffile-prefix-map=" + str(root) + "=/tobkiri/private-sql"]
    command = [str(args.sdk / "bin/clang"), *flags, "-I", str(root / "vendor"),
               str(root / "vendor/sqlite3.c"), str(root / "private_sql.c"),
               *("-Wl,--export=" + name for name in EXPORTS), "-o", str(module)]
    process = subprocess.Popen(command, start_new_session=True)
    minimum_free = initial_free
    while process.poll() is None:
        free = shutil.disk_usage(args.out).free
        minimum_free = min(minimum_free, free)
        if free < args.disk_floor_bytes or initial_free - free > args.max_additional_disk_bytes:
            # This is the isolated process group created by this producer only.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise RuntimeError("owned C producer stopped at its disk bound")
        time.sleep(0.05)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)
    minimum_free = min(minimum_free, shutil.disk_usage(args.out).free)
    source_after = {str(path.relative_to(root)): digest(path) for path in source_paths}
    if source_before != source_after:
        raise RuntimeError("producer source changed during compilation")
    proof = inspect(module.read_bytes())
    proof.update({"sqlite_version": "3.53.2", "sqlite_source_sha256": SOURCE_SHA,
                  "sdk_version": "34.0", "sdk_archive_sha256": SDK_SHA,
                  "compiler_sha256": CLANG_SHA, "linker_sha256": LINKER_SHA,
                  "libc_sha256": LIBC_SHA, "wrapper_sha256": digest(root / "private_sql.c"),
                  "producer_sha256": digest(Path(__file__).resolve()),
                  "compiler_version": subprocess.check_output([str(args.sdk / "bin/clang"), "--version"], text=True).splitlines()[0],
                  "flags": FLAGS + ["-ffile-prefix-map=<producer-root>=/tobkiri/private-sql"],
                  "source_before": source_before, "source_after": source_after,
                  "disk_floor_bytes": args.disk_floor_bytes, "free_before_bytes": initial_free,
                  "free_min_bytes": minimum_free, "peak_additional_disk_bytes": max(0, initial_free-minimum_free),
                  "module_sha256": digest(module), "module_bytes": module.stat().st_size})
    (args.out / "provenance.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps({"module": str(module), "module_sha256": proof["module_sha256"],
                      "module_bytes": proof["module_bytes"], "imports": proof["imports"],
                      "free_bytes": shutil.disk_usage(args.out).free,
                      "peak_additional_disk_bytes": proof["peak_additional_disk_bytes"],
                      "source_unchanged": source_before == source_after}))


if __name__ == "__main__":
    main()
