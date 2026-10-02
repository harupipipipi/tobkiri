"""Cross-build metadata checks never execute Windows PE fixtures."""

from __future__ import annotations

import json
import struct

import pytest

from scripts.build_linux_packvm_bundle import build_portable_bundle
from scripts.build_packvm_guest_bundle import build_guest_bundle
from tests.test_linux_packvm_bundle import ROOT, digest
from tests.test_linux_packvm_bundle import inputs as linux_inputs


def pe(import_name: str | None = None) -> bytes:
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<HH", data, 68, 0x8664, 1)
    struct.pack_into("<H", data, 84, 240)
    struct.pack_into("<H", data, 88, 0x20B)
    struct.pack_into("<I", data, 88 + 108, 16)
    struct.pack_into("<IIII", data, 88 + 240 + 8, 512, 0x1000, 512, 512)
    if import_name:
        struct.pack_into("<II", data, 88 + 112 + 8, 0x1000, 40)
        struct.pack_into("<IIIII", data, 512, 0, 0, 0, 0x1080, 0)
        name = import_name.encode() + b"\0"
        data[640 : 640 + len(name)] = name
    return bytes(data)


@pytest.fixture
def inputs(tmp_path):
    result = linux_inputs.__wrapped__(tmp_path)
    source, _ = result["qemu"]
    source.write_bytes(pe())
    result["qemu"] = (source, digest(source.read_bytes()))
    return result


def build(tmp_path, inputs, dependencies=()):
    return build_portable_bundle(
        inputs=inputs,
        runtime_root=ROOT,
        output=tmp_path / "windows-bundle",
        image_source="https://cloud.debian.org/reviewed-amd64.raw",
        expected_agent_sha256=digest(build_guest_bundle(ROOT)),
        accelerator="whpx",
        dependencies=dependencies,
    )


@pytest.mark.parametrize("dll_name", ["runtime.dll", "libstdc++-6.dll"])
def test_windows_bundle_contains_complete_verified_dll_inventory(
    tmp_path, inputs, dll_name
):
    qemu, _ = inputs["qemu"]
    qemu.write_bytes(pe(dll_name))
    inputs["qemu"] = (qemu, digest(qemu.read_bytes()))
    dll = tmp_path / dll_name
    dll.write_bytes(pe("KERNEL32.dll"))
    path = build(tmp_path, inputs, ((dll.name, dll, digest(dll.read_bytes())),))
    manifest = json.loads(path.read_bytes())
    assert manifest["accelerator"] == "whpx"
    assert manifest["files"]["qemu"]["path"] == "bin/qemu-system-x86_64.exe"
    assert manifest["qemu_dependencies"] == [
        {
            "path": f"bin/{dll_name}",
            "sha256": digest(dll.read_bytes()),
            "size_bytes": dll.stat().st_size,
        }
    ]


def test_windows_bundle_rejects_unbundled_import(tmp_path, inputs):
    qemu, _ = inputs["qemu"]
    qemu.write_bytes(pe("missing-runtime.dll"))
    inputs["qemu"] = (qemu, digest(qemu.read_bytes()))
    with pytest.raises(ValueError, match="unbundled DLL"):
        build(tmp_path, inputs)
    assert not (tmp_path / "windows-bundle").exists()


@pytest.mark.parametrize(
    "import_name",
    ["../libstdc++-6.dll", "a/libstdc++-6.dll", "a\\libstdc++-6.dll", "C:libstdc++-6.dll"],
)
def test_windows_bundle_rejects_paths_in_pe_import_names(tmp_path, inputs, import_name):
    qemu, _ = inputs["qemu"]
    qemu.write_bytes(pe(import_name))
    inputs["qemu"] = (qemu, digest(qemu.read_bytes()))
    with pytest.raises(ValueError, match="DLL name contains a path"):
        build(tmp_path, inputs)
    assert not (tmp_path / "windows-bundle").exists()


def test_windows_bundle_rejects_dependency_digest_mismatch(tmp_path, inputs):
    dll = tmp_path / "runtime.dll"
    dll.write_bytes(pe())
    with pytest.raises(ValueError, match="digest mismatch"):
        build(tmp_path, inputs, ((dll.name, dll, "sha256:" + "0" * 64),))


@pytest.mark.parametrize("dll_name", ["dwrite.dll", "gdiplus.dll", "mswsock.dll", "ncrypt.dll"])
def test_windows_bundle_accepts_documented_windows_system_imports(
    tmp_path, inputs, dll_name
):
    qemu, _ = inputs["qemu"]
    qemu.write_bytes(pe(dll_name))
    inputs["qemu"] = (qemu, digest(qemu.read_bytes()))
    assert json.loads(build(tmp_path, inputs).read_bytes())["qemu_dependencies"] == []


@pytest.mark.parametrize(
    "name", ["../runtime.dll", "a/runtime.dll", "runtime.exe", "a:runtime.dll"]
)
def test_windows_bundle_rejects_unsafe_dependency_name(tmp_path, inputs, name):
    dll = tmp_path / "runtime.dll"
    dll.write_bytes(pe())
    with pytest.raises(ValueError, match="DLL basename"):
        build(tmp_path, inputs, ((name, dll, digest(dll.read_bytes())),))
