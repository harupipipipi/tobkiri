"""Read the actual CI-only Linker observation, without claiming provider proof."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
import stat

FILE_LIMIT = 32 * 1024 * 1024
TOTAL_LIMIT = 64 * 1024 * 1024
MAXIMUM_MEMBERS = 128
CORE_HEADER = b"\0asm\x01\0\0\0"
COMPONENT_HEADER = b"\0asm\x0d\0\x01\0"
HELPER_NAME = "_tobkiri_poll_oracle.abi3.so"


def fingerprint(path: Path, maximum: int, *, private: bool = True) -> dict:
    """Hash one bounded regular, unlinked file and reject concurrent changes."""
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= maximum
        or (private and stat.S_IMODE(before.st_mode) != 0o600)
    ):
        raise ValueError("bounded private regular trace file required")
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            count += len(chunk)
            if count > maximum:
                raise ValueError("trace file exceeded bound while reading")
            digest.update(chunk)
    after = path.lstat()
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
    if count != before.st_size or any(
        getattr(before, field) != getattr(after, field) for field in fields
    ):
        raise ValueError("trace file changed during observation")
    return {"bytes": count, "sha256": digest.hexdigest()}


def _read(path: Path, maximum: int) -> tuple[bytes, dict]:
    identity = fingerprint(path, maximum)
    raw = path.read_bytes()
    if (
        len(raw) != identity["bytes"]
        or hashlib.sha256(raw).hexdigest() != identity["sha256"]
    ):
        raise ValueError("trace control changed during decoding")
    return raw, identity


def read_trace(root: Path, helper: dict, component: Path) -> dict:
    """Bind a complete exact recorder directory to this actual final component."""
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not stat.S_ISDIR(root.lstat().st_mode)
        or stat.S_IMODE(root.lstat().st_mode) != 0o700
        or set(helper) != {"bytes", "sha256"}
        or type(helper["bytes"]) is not int
        or not 0 < helper["bytes"] <= FILE_LIMIT
        or type(helper["sha256"]) is not str
        or re.fullmatch("[0-9a-f]{64}", helper["sha256"]) is None
    ):
        raise ValueError("physical private trace and exact helper identity required")
    raw, manifest = _read(root / "libraries.tsv", 2 * 1024 * 1024)
    if not raw.endswith(b"\n"):
        raise ValueError("trace manifest is incomplete")
    lines = raw.split(b"\n")[:-1]
    if (
        not lines
        or lines[0] != b"tobkiri-ci-link-inputs/1"
        or not 1 <= len(lines) - 1 <= MAXIMUM_MEMBERS
    ):
        raise ValueError("trace header or member bound differs")
    files = {"libraries.tsv": manifest}
    libraries = []
    total = manifest["bytes"]
    for ordinal, line in enumerate(lines[1:]):
        fields = line.split(b"\t")
        if len(fields) != 4 or fields[0] != str(ordinal).encode():
            raise ValueError("trace ordinal or field count differs")
        encoded_name, opened, encoded_size = fields[1:]
        if (
            not 2 <= len(encoded_name) <= 8192
            or re.fullmatch(b"(?:[0-9a-f]{2})+", encoded_name) is None
            or opened not in (b"true", b"false")
            or re.fullmatch(b"[1-9][0-9]{0,7}", encoded_size) is None
        ):
            raise ValueError("trace library metadata is not canonical")
        name = bytes.fromhex(encoded_name.decode("ascii")).decode("utf-8")
        size = int(encoded_size)
        if not name or size > FILE_LIMIT:
            raise ValueError("trace name or library size exceeds bound")
        filename = f"{ordinal:04}.wasm"
        path = root / filename
        identity = fingerprint(path, FILE_LIMIT)
        if identity["bytes"] != size:
            raise ValueError("trace library differs from recorded byte count")
        with path.open("rb") as stream:
            if stream.read(8) != CORE_HEADER:
                raise ValueError("trace library is not a core Wasm module")
        files[filename] = identity
        libraries.append(
            {
                "ordinal": ordinal,
                "name": name,
                "dl_openable": opened == b"true",
                **identity,
            }
        )
        total += identity["bytes"]
        if total > TOTAL_LIMIT:
            raise ValueError("trace libraries exceed aggregate bound")
    linked = root / "preinit-linked-component.wasm"
    identity = fingerprint(linked, FILE_LIMIT)
    with linked.open("rb") as stream:
        if stream.read(8) != COMPONENT_HEADER:
            raise ValueError("trace pre-snapshot output is not a component")
    files[linked.name] = identity
    raw, complete = _read(root / "complete.tsv", 128)
    if raw != (
        f"linked-before-snapshot\t{len(libraries)}\t{identity['bytes']}\n".encode()
    ):
        raise ValueError("trace has no exact successful completion marker")
    files["complete.tsv"] = complete
    total += identity["bytes"] + complete["bytes"]
    if total > TOTAL_LIMIT or {p.name for p in root.iterdir()} != set(files):
        raise ValueError("trace aggregate or exact file membership differs")
    helpers = [
        row for row in libraries if PurePosixPath(row["name"]).name == HELPER_NAME
    ]
    if (
        len(helpers) != 1
        or not helpers[0]["dl_openable"]
        or {key: helpers[0][key] for key in helper} != helper
        or sum(row["sha256"] == helper["sha256"] for row in libraries) != 1
    ):
        raise ValueError("actual Linker inputs do not contain the one exact helper")
    for name in ("libc.so", "libcomponentize_py_bindings.so"):
        rows = [row for row in libraries if row["name"] == name]
        if len(rows) != 1 or rows[0]["dl_openable"]:
            raise ValueError("actual libc or generated binding input differs")
    final = fingerprint(component, FILE_LIMIT)
    with component.open("rb") as stream:
        if stream.read(8) != COMPONENT_HEADER:
            raise ValueError("final probe output is not a component")
    return {
        "format": "tobkiri.ci-link-input-observation/1",
        "files": files,
        "libraries": libraries,
        "helper": helper,
        "total_bytes": total,
        "final_component": final,
        "actual_input_observation": True,
        "stage": "before snapshot; final component identity recorded separately",
        "builtin_libdl_and_synthesized_modules_included": False,
        "final_provider_resolution_verified": False,
        "tls_storage_provider_verified": False,
        "production_enabled": False,
    }


def compare_traces(original: dict, candidate: dict) -> dict:
    """Require the same observed helper/bindings/order and only a libc delta."""
    a, b = original["libraries"], candidate["libraries"]
    if len(a) != len(b) or original["helper"] != candidate["helper"]:
        raise ValueError("paired actual library count or helper differs")
    changed = []
    for left, right in zip(a, b):
        for field in ("ordinal", "name", "dl_openable"):
            if left[field] != right[field]:
                raise ValueError("paired library selection or order differs")
        if (left["bytes"], left["sha256"]) != (right["bytes"], right["sha256"]):
            changed.append(left["name"])
    if changed != ["libc.so"]:
        raise ValueError("paired observed modules must differ only in libc")
    if original["final_component"] == candidate["final_component"]:
        raise ValueError("paired final components are unexpectedly identical")
    return {
        "actual_input_pair_matches": True,
        "changed_libraries": changed,
        "same_helper_and_generated_bindings": True,
        "final_provider_resolution_verified": False,
        "tls_storage_provider_verified": False,
    }
