"""Stage a development-only, digest-pinned amd64 QEMU/KVM PackVM bundle.

This builder never downloads or executes supplied binaries. Input hashes must
come from separately reviewed provenance. The resulting manifest is an
inventory, not a signature or production installation authority. Version one
requires static QEMU; arbitrary dynamic-library relocation is unsupported.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

try:
    from scripts.build_packvm_guest_bundle import build_guest_bundle
except ModuleNotFoundError:
    from build_packvm_guest_bundle import build_guest_bundle


SCHEMA = "io.tobkiri.packvm-qemu-provisioning.v1"
MANIFEST_NAME = "packvm-qemu-provisioning.v1.json"
SERVICE_SCHEMA = "io.tobkiri.packvm-qemu-guest-service-template.v1"
BWRAP_SCHEMA = "io.tobkiri.packvm-qemu-bubblewrap-descriptor.v1"
BWRAP_VERSION = "0.12.0-1~deb13u1"
GUEST_PROTOCOL = "io.tobkiri.macos-vz-supervisor.v1"
FILE_PATHS = {
    "qemu": "bin/qemu-system-x86_64",
    "firmware_code": "firmware/OVMF_CODE.fd",
    "firmware_vars": "firmware/OVMF_VARS.fd",
    "image": "images/packvm-amd64.raw",
    "agent": "provisioning/runner.py",
    "config": "provisioning/cloud_init_template.yaml",
    "service": "provisioning/guest_service_template.v1.json",
    "bubblewrap": "provisioning/bubblewrap_amd64.deb",
    "bubblewrap_descriptor": "provisioning/bubblewrap_descriptor.v1.json",
}
INPUT_SLOTS = frozenset(FILE_PATHS) - {"agent", "service"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_BYTES = {
    "qemu": 256 * 1024 * 1024,
    "firmware_code": 64 * 1024 * 1024,
    "firmware_vars": 64 * 1024 * 1024,
    "image": 64 * 1024 * 1024 * 1024,
    "config": 64 * 1024,
    "bubblewrap": 16 * 1024 * 1024,
    "bubblewrap_descriptor": 64 * 1024,
}
SERVICE_UNIT = (
    "[Unit]\n"
    "Description=Tobkiri PackVM authenticated guest supervisor\n"
    "After=local-fs.target\n\n"
    "[Service]\nType=simple\nUser=root\nGroup=root\n"
    "ExecStart=/usr/bin/python3 /usr/local/lib/tobkiri-packvm/"
    "packvm_guest_runner.py --serve-virtio-serial\n"
    "Restart=on-failure\nRestartSec=1\nNoNewPrivileges=true\n"
    "PrivateTmp=true\nProtectHome=true\n\n"
    "[Install]\nWantedBy=multi-user.target\n"
)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _require_digest(value: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("expected input digest must be sha256 plus 64 lowercase hex digits")


def _require_https(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or any(character.isspace() for character in value)
    ):
        raise ValueError("source must be an uncredentialed HTTPS URL without query or fragment")


@contextmanager
def _pinned_input(path: Path) -> Iterator[BinaryIO]:
    """Open each path component without following links, then pin the file."""
    if os.name == "nt":
        source_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, source_root)
        try:
            from tobkiri_host.windows_whpx_security import stable_file

            with stable_file(path) as stream:
                yield stream
        finally:
            sys.path.remove(source_root)
        return
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        source_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with os.fdopen(source_fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("input must be a single-link regular file")
        yield stream


def _copy_verified(
    source: Path, destination: Path, expected_digest: str, slot: str
) -> dict[str, object]:
    _require_digest(expected_digest)
    digest = hashlib.sha256()
    total = 0
    with _pinned_input(source) as stream, destination.open("xb") as target:
        before = os.fstat(stream.fileno())
        if not 0 < before.st_size <= _MAX_BYTES[slot]:
            raise ValueError(f"{slot} input size is outside the supported bounds")
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > before.st_size:
                raise ValueError(f"{slot} input changed during copy")
            digest.update(block)
            target.write(block)
        after = os.fstat(stream.fileno())
        if (
            total != before.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or after.st_nlink != 1
            or "sha256:" + digest.hexdigest() != expected_digest
        ):
            raise ValueError(f"{slot} input digest mismatch or input changed during copy")
        target.flush()
        os.fsync(target.fileno())
    destination.chmod(0o555 if slot == "qemu" else 0o444)
    return {"path": FILE_PATHS[slot], "sha256": expected_digest, "size_bytes": total}


def validate_static_qemu(path: Path) -> None:
    """Use the same ELF rules as runtime admission, without executing QEMU."""
    source = Path(__file__).resolve().parents[1] / "tobkiri_host/qemu_binary_validation.py"
    spec = importlib.util.spec_from_file_location("_packvm_elf_validation", source)
    if spec is None or spec.loader is None:
        raise ValueError("PackVM ELF validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_static_amd64_elf(path)


def _validate_bubblewrap(root: Path, records: Mapping[str, dict[str, object]]) -> None:
    descriptor = json.loads((root / FILE_PATHS["bubblewrap_descriptor"]).read_bytes())
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != {"schema", "package", "version", "architecture", "source"}
        or descriptor["schema"] != BWRAP_SCHEMA
        or descriptor["package"] != "bubblewrap"
        or descriptor["version"] != BWRAP_VERSION
        or descriptor["architecture"] != "amd64"
    ):
        raise ValueError("unsupported amd64 bubblewrap descriptor")
    source = descriptor["source"]
    if not isinstance(source, dict) or set(source) != {"url", "size_bytes", "sha256"}:
        raise ValueError("invalid bubblewrap source descriptor")
    _require_https(source["url"])
    package = records["bubblewrap"]
    if (
        isinstance(source["size_bytes"], bool)
        or source["size_bytes"] != package["size_bytes"]
        or source["sha256"] != package["sha256"]
    ):
        raise ValueError("bubblewrap descriptor does not bind the supplied package")


def _write_generated(root: Path, slot: str, content: bytes) -> dict[str, object]:
    destination = root / FILE_PATHS[slot]
    with destination.open("xb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    destination.chmod(0o444)
    return {"path": FILE_PATHS[slot], "sha256": _digest(content), "size_bytes": len(content)}


def build_portable_bundle(
    *,
    inputs: Mapping[str, tuple[Path, str]],
    runtime_root: Path,
    output: Path,
    image_source: str,
    expected_agent_sha256: str,
    accelerator: str = "kvm",
    dependencies: tuple[tuple[str, Path, str], ...] = (),
) -> Path:
    """Publish a fresh bundle only after all explicit input identities verify.

    ``inputs`` has exactly INPUT_SLOTS, each paired with an independently
    expected digest. Generated guest code is checked against its separate
    expected zipapp digest. Neither that check nor this manifest signs code.
    """
    if accelerator not in {"kvm", "whpx"} or (accelerator == "kvm" and dependencies):
        raise ValueError("unsupported portable VM accelerator or dependency closure")
    if len(dependencies) > 128:
        raise ValueError("too many QEMU dependencies")
    paths = dict(FILE_PATHS)
    if accelerator == "whpx":
        paths["qemu"] = "bin/qemu-system-x86_64.exe"
    dependency_names: set[str] = set()
    for name, _, expected in dependencies:
        if (
            not re.fullmatch(r"[A-Za-z0-9_+.-]+\.dll", name, re.IGNORECASE)
            or name.lower() in dependency_names
            or name.startswith(".")
        ):
            raise ValueError("QEMU dependency must have a unique portable DLL basename")
        dependency_names.add(name.lower())
        _require_digest(expected)
    if set(inputs) != INPUT_SLOTS:
        raise ValueError("all and only the explicit Linux bundle input slots are required")
    _require_https(image_source)
    _require_digest(expected_agent_sha256)
    for _, digest in inputs.values():
        _require_digest(digest)
    output = Path(os.path.abspath(output))
    if output.exists() or output.is_symlink():
        raise ValueError("bundle output must not already exist")
    if (
        not output.parent.is_dir()
        or output.parent.resolve(strict=True) != output.parent
        or any(parent.is_symlink() for parent in (output.parent, *output.parent.parents))
    ):
        raise ValueError("bundle parent must exist without symlink traversal")
    temporary = Path(tempfile.mkdtemp(prefix=".packvm-qemu-", dir=output.parent))
    try:
        for relative in paths.values():
            (temporary / relative).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        records = {
            slot: _copy_verified(path, temporary / paths[slot], digest, slot)
            for slot, (path, digest) in sorted(inputs.items())
        }
        for slot, record in records.items():
            record["path"] = paths[slot]
        dependency_records = []
        for name, source, expected in dependencies:
            relative = "bin/" + name
            record = _copy_verified(source, temporary / relative, expected, "qemu")
            record["path"] = relative
            dependency_records.append(record)
        if accelerator == "kvm":
            validate_static_qemu(temporary / paths["qemu"])
        else:
            source_root = str(Path(__file__).resolve().parents[1])
            sys.path.insert(0, source_root)
            try:
                from ecosystem.defaultspack.backend.sandbox.isolation.windows_whpx_assets import (
                    verify_windows_qemu_dependency_closure,
                )

                verify_windows_qemu_dependency_closure(
                    temporary / paths["qemu"],
                    tuple(temporary / str(record["path"]) for record in dependency_records),
                )
            finally:
                sys.path.remove(source_root)
        _validate_bubblewrap(temporary, records)
        agent = build_guest_bundle(runtime_root)
        if _digest(agent) != expected_agent_sha256:
            raise ValueError("generated guest agent digest mismatch")
        records["agent"] = _write_generated(temporary, "agent", agent)
        service = {
            "schema": SERVICE_SCHEMA,
            "protocol": GUEST_PROTOCOL,
            "guest_runner_sha256": expected_agent_sha256,
            "service_unit": SERVICE_UNIT,
        }
        records["service"] = _write_generated(temporary, "service", _json_bytes(service))
        manifest = {
            "schema": SCHEMA,
            "architecture": "amd64",
            "accelerator": accelerator,
            "files": records,
            "image_source": image_source,
            "qemu_dependencies": dependency_records,
        }
        manifest_path = temporary / MANIFEST_NAME
        with manifest_path.open("xb") as target:
            target.write(_json_bytes(manifest))
            target.flush()
            os.fsync(target.fileno())
        manifest_path.chmod(0o444)
        if output.exists() or output.is_symlink():
            raise ValueError("bundle output appeared during staging")
        os.rename(temporary, output)
        return output / MANIFEST_NAME
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, onerror=_remove_staged_readonly)


def _remove_staged_readonly(function, path, error) -> None:
    """Allow cleanup of this builder's unpublished read-only Windows stage."""
    if os.name != "nt":
        raise error[1]
    Path(path).chmod(0o700 if Path(path).is_dir() else 0o600)
    function(path)


def build_linux_bundle(
    *,
    inputs: Mapping[str, tuple[Path, str]],
    runtime_root: Path,
    output: Path,
    image_source: str,
    expected_agent_sha256: str,
) -> Path:
    """Stage only the static Linux/KVM profile of the portable bundle."""
    return build_portable_bundle(
        inputs=inputs,
        runtime_root=runtime_root,
        output=output,
        image_source=image_source,
        expected_agent_sha256=expected_agent_sha256,
    )


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main(*, accelerator: str = "kvm") -> int:
    """Stage reviewed local artifacts without downloads or host installation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-source", required=True)
    parser.add_argument("--expected-agent-sha256", required=True)
    for slot in sorted(INPUT_SLOTS):
        argument = slot.replace("_", "-")
        parser.add_argument(f"--{argument}", type=Path, required=True)
        parser.add_argument(f"--{argument}-sha256", required=True)
    if accelerator == "whpx":
        parser.add_argument(
            "--qemu-dll", nargs=3, action="append", default=[], metavar=("NAME", "PATH", "SHA256")
        )
    arguments = parser.parse_args()
    inputs = {
        slot: (getattr(arguments, slot), getattr(arguments, slot + "_sha256"))
        for slot in INPUT_SLOTS
    }
    path = build_portable_bundle(
        inputs=inputs,
        runtime_root=arguments.runtime_root,
        output=arguments.output,
        image_source=arguments.image_source,
        expected_agent_sha256=arguments.expected_agent_sha256,
        accelerator=accelerator,
        dependencies=tuple(
            (name, Path(path), digest) for name, path, digest in getattr(arguments, "qemu_dll", [])
        ),
    )
    print(f"Development bundle manifest: {path}")
    print(f"Manifest inventory digest (not a signature): {_digest(path.read_bytes())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
