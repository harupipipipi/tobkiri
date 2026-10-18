"""Expand reviewed local vendor pins into the strict development WHPX builder.

Never downloads, extracts, executes or signs vendor binaries. The input lock is
not a production trust authority. Guest code/config pins are separate inputs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from scripts.build_linux_packvm_bundle import build_portable_bundle
except ModuleNotFoundError:
    from build_linux_packvm_bundle import build_portable_bundle

SCHEMA = "io.tobkiri.windows-packvm-input-lock.v1"
VENDOR_SLOTS = frozenset(
    {
        "qemu",
        "firmware_code",
        "firmware_vars",
        "image",
        "bubblewrap",
        "bubblewrap_descriptor",
    }
)


def _input(root: Path, record: Any) -> tuple[Path, str]:
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "size_bytes"}:
        raise ValueError("invalid Windows vendor input record")
    raw = record["path"]
    if not isinstance(raw, str):
        raise TypeError("vendor input path must be text")
    path = PurePosixPath(raw)
    if (
        not path.parts
        or path.is_absolute()
        or str(path) != raw
        or any(p in {".", ".."} or p.endswith((".", " ")) for p in path.parts)
        or any(c in raw for c in ("\\", ":", "\0", "\r", "\n"))
    ):
        raise ValueError("vendor input path is not a safe relative path")
    source = root.joinpath(*path.parts)
    if type(record["size_bytes"]) is not int or record["size_bytes"] <= 0:
        raise ValueError("vendor input size must be positive")
    if source.stat().st_size != record["size_bytes"]:
        raise ValueError("vendor input size mismatch")
    return source, record["sha256"]


def build_pinned_bundle(
    *,
    lock: Path,
    inputs_root: Path,
    runtime_root: Path,
    config: Path,
    expected_config_sha256: str,
    expected_agent_sha256: str,
    output: Path,
) -> Path:
    """Expand reviewed pins; delegate all stable-file, digest and PE checks."""
    value = json.loads(lock.read_bytes())
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA
        or value.get("architecture") != "amd64"
        or value.get("accelerator") != "whpx"
        or not isinstance(value.get("files"), dict)
        or set(value["files"]) != VENDOR_SLOTS
        or not isinstance(value.get("qemu_dependencies"), list)
        or len(value["qemu_dependencies"]) > 128
    ):
        raise ValueError("unsupported Windows vendor input lock")
    root = inputs_root.absolute()
    inputs = {slot: _input(root, record) for slot, record in value["files"].items()}
    inputs["config"] = (config.absolute(), expected_config_sha256)
    dependencies = []
    for record in value["qemu_dependencies"]:
        path, digest = _input(root, record)
        dependencies.append((path.name, path, digest))
    return build_portable_bundle(
        inputs=inputs,
        runtime_root=runtime_root,
        output=output,
        image_source=value["image_source"],
        expected_agent_sha256=expected_agent_sha256,
        accelerator="whpx",
        dependencies=tuple(dependencies),
    )


def main() -> int:
    """Stage a fresh development bundle without executing downloaded code."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("lock", "inputs-root", "runtime-root", "config", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--expected-agent-sha256", required=True)
    print(build_pinned_bundle(**vars(parser.parse_args())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
