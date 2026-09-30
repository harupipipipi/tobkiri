"""Linux bundle staging checks are structural, not actual VM boot evidence."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

import yaml
from scripts import build_linux_packvm_bundle as builder
from scripts.build_packvm_guest_bundle import build_guest_bundle

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT.parent / "tobkiri_launcher/packvm-qemu/Provisioning/cloud_init_template.yaml"


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def elf(*, interpreter: bool = False, needed: bool = False, machine: int = 62) -> bytes:
    """Return non-executable test data resembling the ELF metadata we inspect."""
    count = 1 + bool(interpreter or needed)
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        b"\x7fELF\x02\x01\x01" + bytes(9),
        2,
        machine,
        1,
        0,
        64,
        0,
        0,
        64,
        56,
        count,
        0,
        0,
        0,
    )
    load = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, 256, 256, 4096)
    extra = b""
    payload = b""
    if interpreter:
        extra = struct.pack("<IIQQQQQQ", 3, 4, 192, 0, 0, 16, 16, 1)
        payload = b"/lib/ld.so\0"
    elif needed:
        extra = struct.pack("<IIQQQQQQ", 2, 4, 192, 0, 0, 32, 32, 8)
        payload = struct.pack("<qQqQ", 1, 1, 0, 0)
    return (header + load + extra).ljust(192, b"\0") + payload.ljust(64, b"\0")


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, tuple[Path, str]]:
    source = tmp_path / "sources"
    source.mkdir()
    package = b"pinned package unit-test bytes, not an installable package"
    descriptor = {
        "schema": builder.BWRAP_SCHEMA,
        "package": "bubblewrap",
        "version": builder.BWRAP_VERSION,
        "architecture": "amd64",
        "source": {
            "url": "https://deb.debian.org/debian/pool/main/b/bubblewrap/"
            "bubblewrap_0.12.0-1~deb13u1_amd64.deb",
            "sha256": digest(package),
            "size_bytes": len(package),
        },
    }
    contents = {
        "qemu": elf(),
        "firmware_code": b"pinned firmware code fixture",
        "firmware_vars": b"pinned empty firmware vars fixture",
        "image": b"pinned raw disk fixture",
        "config": TEMPLATE.read_bytes(),
        "bubblewrap": package,
        "bubblewrap_descriptor": json.dumps(descriptor).encode(),
    }
    result = {}
    for slot, content in contents.items():
        path = source / slot
        path.write_bytes(content)
        result[slot] = (path, digest(content))
    return result


def build(tmp_path: Path, inputs: dict, **overrides: object) -> Path:
    options = {
        "inputs": inputs,
        "runtime_root": ROOT,
        "output": tmp_path / "bundle",
        "image_source": "https://cloud.debian.org/images/cloud/trixie/reviewed.raw",
        "expected_agent_sha256": digest(build_guest_bundle(ROOT)),
    }
    options.update(overrides)
    return builder.build_linux_bundle(**options)


def test_manifest_binds_exact_files_and_generated_service(tmp_path: Path, inputs: dict) -> None:
    path = build(tmp_path, inputs)
    manifest = json.loads(path.read_bytes())
    assert set(manifest) == {
        "schema",
        "architecture",
        "accelerator",
        "files",
        "image_source",
        "qemu_dependencies",
    }
    assert manifest["schema"] == builder.SCHEMA
    assert manifest["architecture"] == "amd64"
    assert manifest["accelerator"] == "kvm"
    assert manifest["qemu_dependencies"] == []
    assert set(manifest["files"]) == set(builder.FILE_PATHS)
    for slot, record in manifest["files"].items():
        assert set(record) == {"path", "sha256", "size_bytes"}
        assert record["path"] == builder.FILE_PATHS[slot]
        staged = path.parent / record["path"]
        assert staged.is_file() and not staged.is_symlink()
        assert record["sha256"] == digest(staged.read_bytes())
        assert record["size_bytes"] == staged.stat().st_size
        assert not staged.stat().st_mode & 0o222
    service = json.loads((path.parent / builder.FILE_PATHS["service"]).read_bytes())
    assert service["guest_runner_sha256"] == manifest["files"]["agent"]["sha256"]
    assert service["protocol"] == "io.tobkiri.macos-vz-supervisor.v1"
    assert "--serve-virtio-serial" in service["service_unit"]
    assert "--serve-vsock" not in service["service_unit"]
    assert not any("key" in name for name in manifest["files"])


@pytest.mark.parametrize("slot", sorted(builder.INPUT_SLOTS))
def test_tampered_input_refuses_publication(tmp_path: Path, inputs: dict, slot: str) -> None:
    source, expected = inputs[slot]
    source.write_bytes(source.read_bytes() + b"tampered")
    assert inputs[slot][1] == expected
    with pytest.raises(ValueError, match="digest mismatch"):
        build(tmp_path, inputs)
    assert not (tmp_path / "bundle").exists()
    assert not list(tmp_path.glob(".packvm-qemu-*"))


@pytest.mark.parametrize("mode", ["symlink", "hardlink", "parent-symlink"])
def test_linked_inputs_are_rejected(tmp_path: Path, inputs: dict, mode: str) -> None:
    source, expected = inputs["image"]
    foreign = tmp_path / "linked-input"
    if mode == "symlink":
        foreign.symlink_to(source)
    elif mode == "hardlink":
        os.link(source, foreign)
    else:
        foreign.symlink_to(source.parent, target_is_directory=True)
        foreign = foreign / source.name
    inputs["image"] = (foreign, expected)
    with pytest.raises((OSError, ValueError)):
        build(tmp_path, inputs)
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    "content, message",
    [
        (elf(interpreter=True), "dynamic QEMU"),
        (elf(needed=True), "dynamic dependencies"),
        (elf(machine=183), "amd64"),
        (b"not ELF", "ELF64"),
    ],
)
def test_static_qemu_rejects_loader_dependencies_and_wrong_arch(
    tmp_path: Path, inputs: dict, content: bytes, message: str
) -> None:
    source, _ = inputs["qemu"]
    source.write_bytes(content)
    inputs["qemu"] = (source, digest(content))
    with pytest.raises(ValueError, match=message):
        build(tmp_path, inputs)


def test_guest_archive_requires_separately_expected_digest(tmp_path: Path, inputs: dict) -> None:
    with pytest.raises(ValueError, match="guest agent digest mismatch"):
        build(tmp_path, inputs, expected_agent_sha256="sha256:" + "0" * 64)


def test_bubblewrap_descriptor_cannot_disagree_with_package(tmp_path: Path, inputs: dict) -> None:
    source, _ = inputs["bubblewrap_descriptor"]
    descriptor = json.loads(source.read_bytes())
    descriptor["source"]["sha256"] = "sha256:" + "1" * 64
    content = json.dumps(descriptor).encode()
    source.write_bytes(content)
    inputs["bubblewrap_descriptor"] = (source, digest(content))
    with pytest.raises(ValueError, match="does not bind"):
        build(tmp_path, inputs)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/image",
        "https://a:b@example.com/image",
        "https://example.com/image?token=secret",
    ],
)
def test_source_does_not_embed_credentials(tmp_path: Path, inputs: dict, url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS URL"):
        build(tmp_path, inputs, image_source=url)


def test_existing_output_is_never_overwritten(tmp_path: Path, inputs: dict) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    sentinel = output / "user-data"
    sentinel.write_text("retain")
    with pytest.raises(ValueError, match="already exist"):
        build(tmp_path, inputs)
    assert sentinel.read_text() == "retain"


def test_missing_or_additional_inputs_fail_closed(tmp_path: Path, inputs: dict) -> None:
    del inputs["firmware_code"]
    with pytest.raises(ValueError, match="input slots"):
        build(tmp_path, inputs)


def test_cloud_init_preserves_identity_and_sandbox_checks() -> None:
    template = TEMPLATE.read_text()
    document = yaml.safe_load(template)
    script = next(
        item["content"]
        for item in document["write_files"]
        if item["path"] == "/usr/local/sbin/tobkiri-packvm-bootstrap"
    )
    python_source = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    compile(python_source, str(TEMPLATE), "exec")
    assert 'bwrap["architecture"] != "amd64"' in script
    assert builder.BWRAP_SCHEMA in script
    assert builder.SERVICE_SCHEMA in script
    assert builder.GUEST_PROTOCOL in script
    assert "hashlib.sha256(runner_path.read_bytes())" in script
    assert "hashlib.sha256(bwrap_path.read_bytes())" in script
    assert "materialize_seed_artifact(" in script
    assert 'copy_private(agent_key, runtime_dir / "agent-ed25519.pem", 0o600)' in script
    assert 'dpkg -i "$agent_mount/bubblewrap_amd64.deb"' in script
    assert "dpkg --audit" in script
    assert "/dev/ttyS0" in script
    assert "arm64" not in template
