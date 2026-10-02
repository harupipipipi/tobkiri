"""Portable VM assets receive authority only through a sealed role scope."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from core_runtime import packaged_application_bundle as bindings

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / ".github/scripts/sealed_python_sources/tobkiri_sealed/bootstrap.py"


@pytest.fixture
def bootstrap(monkeypatch):
    monkeypatch.syspath_prepend(str(BOOTSTRAP.parents[1]))
    monkeypatch.delitem(sys.modules, "tobkiri_sealed.bootstrap", raising=False)
    monkeypatch.delitem(sys.modules, "tobkiri_sealed", raising=False)
    return importlib.import_module("tobkiri_sealed.bootstrap")


def fixture(root: Path, accelerator: str = "kvm") -> tuple[Path, str, str]:
    app = root / "app"
    bundle = app / "packvm-qemu"
    bundle.mkdir(parents=True)
    data = json.dumps(
        {
            "schema": "io.tobkiri.packvm-qemu-provisioning.v1",
            "architecture": "amd64",
            "accelerator": accelerator,
        }
    ).encode()
    (bundle / "packvm-qemu-provisioning.v1.json").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    outer = json.dumps(
        {
            "schema": "io.tobkiri.runtime-resource-manifest.v1",
            "entries": [
                {
                    "path": "packvm-qemu/packvm-qemu-provisioning.v1.json",
                    "size": len(data),
                    "sha256": digest,
                }
            ],
        }
    ).encode()
    (app / "runtime-resource-manifest.v1.json").write_bytes(outer)
    return app, digest, hashlib.sha256(outer).hexdigest()


def test_portable_binding_is_immutable_and_bound_to_outer_inventory(tmp_path, bootstrap) -> None:
    app, digest, outer = fixture(tmp_path)
    raw = bootstrap._verify_packvm_bundle_binding(str(app), digest, "", "", outer)
    assert isinstance(raw, MappingProxyType)
    assert dict(raw) == {
        "root": str(app / "packvm-qemu"),
        "provisioning_sha256": "sha256:" + digest,
        "platform": "linux",
    }
    checked = bindings._validated_binding(raw)
    assert isinstance(checked, bindings.PortablePackVMBundleBinding)
    assert checked.root == app / "packvm-qemu"
    with pytest.raises(TypeError):
        raw["platform"] = "windows"


def test_portable_binding_rejects_windows_bundle_on_linux(tmp_path, bootstrap) -> None:
    app, digest, outer = fixture(tmp_path, "whpx")
    with pytest.raises(bootstrap.SealedBootstrapError, match="platform"):
        bootstrap._verify_packvm_bundle_binding(str(app), digest, "", "", outer)


@pytest.mark.parametrize("target", ["manifest", "outer"])
def test_portable_binding_rejects_replaced_inventory(tmp_path, bootstrap, target) -> None:
    app, digest, outer = fixture(tmp_path)
    path = app / (
        "packvm-qemu/packvm-qemu-provisioning.v1.json"
        if target == "manifest"
        else "runtime-resource-manifest.v1.json"
    )
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(bootstrap.SealedBootstrapError, match="changed"):
        bootstrap._verify_packvm_bundle_binding(str(app), digest, "", "", outer)


def test_portable_binding_rejects_self_consistent_unbound_manifest(tmp_path, bootstrap) -> None:
    app, digest, _ = fixture(tmp_path)
    outer = json.dumps(
        {"schema": "io.tobkiri.runtime-resource-manifest.v1", "entries": []}
    ).encode()
    (app / "runtime-resource-manifest.v1.json").write_bytes(outer)
    with pytest.raises(bootstrap.SealedBootstrapError, match="does not bind"):
        bootstrap._verify_packvm_bundle_binding(
            str(app), digest, "", "", hashlib.sha256(outer).hexdigest()
        )


def test_portable_binding_rejects_symlink_root(tmp_path, bootstrap) -> None:
    app, digest, outer = fixture(tmp_path)
    link = tmp_path / "alternate"
    link.symlink_to(app)
    with pytest.raises(bootstrap.SealedBootstrapError):
        bootstrap._verify_packvm_bundle_binding(str(link), digest, "", "", outer)


def test_runtime_does_not_accept_raw_mutable_portable_binding(tmp_path) -> None:
    app, digest, _ = fixture(tmp_path)
    raw = {
        "root": str(app / "packvm-qemu"),
        "provisioning_sha256": "sha256:" + digest,
        "platform": "linux",
    }
    with pytest.raises(bindings.PackagedApplicationBundleBindingError, match="immutable"):
        bindings._validated_binding(raw)


def test_portable_readonly_binding_cannot_change_after_install(tmp_path, monkeypatch) -> None:
    app, digest, _ = fixture(tmp_path)
    raw = MappingProxyType(
        {
            "root": str(app / "packvm-qemu"),
            "provisioning_sha256": "sha256:" + digest,
            "platform": "linux",
        }
    )

    class Scope:
        def app_root_for(self, module_file):
            return app

        def packvm_bundle_binding_for(self, module_file):
            return raw

    monkeypatch.setattr(bindings, "_PACKVM_BUNDLE_BINDING_INITIALIZED", False)
    monkeypatch.setattr(bindings, "_PACKVM_BUNDLE_BINDING", None)
    accepted = bindings.install_packvm_bundle_binding_from_sealed_scope(Scope(), __file__)
    assert accepted == bindings.packvm_bundle_binding()
    raw = MappingProxyType(
        {
            "root": str(app / "packvm-qemu"),
            "provisioning_sha256": "sha256:" + "0" * 64,
            "platform": "linux",
        }
    )
    with pytest.raises(bindings.PackagedApplicationBundleBindingError, match="already established"):
        bindings.install_packvm_bundle_binding_from_sealed_scope(Scope(), __file__)
