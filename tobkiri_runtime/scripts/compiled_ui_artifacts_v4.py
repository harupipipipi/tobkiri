"""Declare compiled Defaultspack UI bytes in a canonical Pack source record."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

_PUBLIC_SUFFIXES = frozenset(
    {
        ".html",
        ".js",
        ".css",
        ".png",
        ".svg",
        ".wasm",
        ".task",
        ".txt",
        ".md",
        ".woff",
        ".woff2",
        ".ttf",
        ".ico",
        ".webp",
        ".jpg",
        ".jpeg",
        ".gif",
    }
)
_EXCLUDED = frozenset({".shell-bundle-manifest.json", "actions.py"})
_MAX_ASSET_BYTES = 32 * 1024 * 1024


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_root(path: Path) -> int:
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise ValueError("compiled UI root cannot contain parent traversal")
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _digest_file(parent_fd: int, name: str) -> str:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_ASSET_BYTES
        ):
            raise ValueError("compiled UI asset is not a bounded single-link file")
        digest = hashlib.sha256()
        total = 0
        while block := os.read(descriptor, 1024 * 1024):
            total += len(block)
            if total > _MAX_ASSET_BYTES:
                raise ValueError("compiled UI asset grew beyond its size limit")
            digest.update(block)
        if total != before.st_size or _identity(before) != _identity(os.fstat(descriptor)):
            raise ValueError("compiled UI asset changed while it was read")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def compiled_ui_artifacts(ui_root: Path) -> list[dict[str, str]]:
    """Hash only public compiled UI assets, without following any symlinks."""
    result: list[dict[str, str]] = []

    def walk(descriptor: int, prefix: tuple[str, ...]) -> None:
        before = os.fstat(descriptor)
        with os.scandir(descriptor) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if "\\" in name or name in {".", ".."}:
                raise ValueError("compiled UI path is invalid")
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("compiled UI contains a symlink")
            if not prefix and name in _EXCLUDED:
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("excluded UI metadata is not a regular file")
                continue
            if name.startswith("."):
                raise ValueError("compiled UI contains an undeclared hidden path")
            parts = (*prefix, name)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    walk(child, parts)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                if Path(name).suffix.lower() not in _PUBLIC_SUFFIXES:
                    raise ValueError("compiled UI contains a non-public file type")
                result.append(
                    {
                        "path": "/".join(("ui", *parts)),
                        "digest": _digest_file(descriptor, name),
                        "kind": "asset",
                        "index_role": "asset",
                    }
                )
            else:
                raise ValueError("compiled UI contains a non-regular path")
        if _identity(before) != _identity(os.fstat(descriptor)):
            raise ValueError("compiled UI directory changed while it was read")

    descriptor = _open_root(ui_root)
    try:
        walk(descriptor, ())
    finally:
        os.close(descriptor)
    if not any(item["path"] == "ui/shell.html" for item in result):
        raise ValueError("compiled UI requires shell.html")
    return sorted(result, key=lambda item: item["path"])


def catalog_with_compiled_ui(
    catalog: Mapping[str, Any],
    *,
    ui_root: Path,
    version: str,
) -> dict[str, Any]:
    """Return a copy with only the Defaultspack version and UI assets updated."""
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("compiled UI release requires an explicit stable version")
    updated = copy.deepcopy(dict(catalog))
    records = [r for r in updated["packs"] if r["pack_id"] == "defaultspack"]
    if len(records) != 1 or records[0].get("kind") != "normal_sandbox":
        raise ValueError("compiled UI owner must be the single Normal Defaultspack")
    record = records[0]
    assets = compiled_ui_artifacts(ui_root)
    retained = [
        item
        for item in record["runtime_artifacts"]
        if not (item["path"].startswith("ui/") and item.get("kind") == "asset")
    ]
    if {item["path"] for item in retained} & {item["path"] for item in assets}:
        raise ValueError("compiled UI asset collides with a runtime artifact")
    record["runtime_artifacts"] = sorted(
        [*retained, *assets],
        key=lambda item: item["path"],
    )
    record["version"] = version
    return updated


def main() -> None:
    """Update explicit canonical source; the normal artifact generator follows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--ui-root", required=True, type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    updated = catalog_with_compiled_ui(
        catalog,
        ui_root=args.ui_root,
        version=args.version,
    )
    args.catalog.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
