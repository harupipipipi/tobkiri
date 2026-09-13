"""Focused tests for sealed packaged PackVM bundle binding."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from core_runtime import packaged_application_bundle
from core_runtime.packaged_application_bundle import (
    PackagedApplicationBundleBindingError,
    install_packvm_bundle_binding_from_sealed_scope,
    packvm_bundle_binding,
)


def _digest(seed: str) -> str:
    return "sha256:" + seed * 64


class _Scope:
    def __init__(self, app_root: Path, binding: MappingProxyType) -> None:
        self.app_root = app_root
        self.binding = binding
        self.app_root_calls: list[str | Path] = []
        self.binding_calls: list[str | Path] = []

    def app_root_for(self, module_file: str | Path) -> Path:
        self.app_root_calls.append(module_file)
        return self.app_root

    def packvm_bundle_binding_for(self, module_file: str | Path) -> MappingProxyType:
        self.binding_calls.append(module_file)
        return self.binding


@pytest.fixture(autouse=True)
def _reset_process_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the process-global one-shot slot isolated between focused tests."""

    monkeypatch.setattr(packaged_application_bundle, "_PACKVM_BUNDLE_BINDING", None)
    monkeypatch.setattr(
        packaged_application_bundle,
        "_PACKVM_BUNDLE_BINDING_INITIALIZED",
        False,
    )


def _binding(root: Path, *, provisioning: str = _digest("a")) -> MappingProxyType:
    return MappingProxyType(
        {
            "root": str(root),
            "provisioning_sha256": provisioning,
            "helper_manifest_sha256": _digest("b"),
            "helper_team_id": "ABCDEFGHIJ",
        }
    )


def test_sealed_scope_installs_exact_bundle_binding_once(tmp_path: Path) -> None:
    """Only immutable scope evidence can install the process-global binding."""

    root = tmp_path / "Tobkiri Launcher.app"
    root.mkdir()
    app_root = tmp_path / "sealed-python" / "app"
    app_root.mkdir(parents=True)
    scope = _Scope(app_root, _binding(root))
    module_file = "/sealed/app/app.py"

    first = install_packvm_bundle_binding_from_sealed_scope(scope, module_file)
    second = install_packvm_bundle_binding_from_sealed_scope(scope, module_file)

    assert first is not None
    assert first is second
    assert packvm_bundle_binding() is first
    assert first.root == root
    assert first.provisioning_sha256 == _digest("a")
    assert scope.app_root_calls == [module_file, module_file]
    assert scope.binding_calls == [module_file, module_file]


def test_sealed_scope_cannot_replace_installed_bundle_binding(tmp_path: Path) -> None:
    """A changed manifest identity is rejected instead of silently rebinding."""

    root = tmp_path / "Tobkiri Launcher.app"
    root.mkdir()
    app_root = tmp_path / "sealed-python" / "app"
    app_root.mkdir(parents=True)
    install_packvm_bundle_binding_from_sealed_scope(
        _Scope(app_root, _binding(root)),
        "/sealed/app/app.py",
    )

    with pytest.raises(PackagedApplicationBundleBindingError, match="already established"):
        install_packvm_bundle_binding_from_sealed_scope(
            _Scope(app_root, _binding(root, provisioning=_digest("c"))),
            "/sealed/app/app.py",
        )


def test_unbundled_sealed_role_seals_packvm_bundle_absence(tmp_path: Path) -> None:
    """Non-macOS sealed roles cannot gain a PackVM bundle later in process."""

    app_root = tmp_path / "sealed-python" / "app"
    app_root.mkdir(parents=True)
    scope = _Scope(app_root, None)  # type: ignore[arg-type]

    assert install_packvm_bundle_binding_from_sealed_scope(scope, "/sealed/app/app.py") is None
    assert packvm_bundle_binding() is None
    assert install_packvm_bundle_binding_from_sealed_scope(scope, "/sealed/app/app.py") is None

    root = tmp_path / "Tobkiri Launcher.app"
    root.mkdir()
    with pytest.raises(PackagedApplicationBundleBindingError, match="already established"):
        install_packvm_bundle_binding_from_sealed_scope(
            _Scope(app_root, _binding(root)),
            "/sealed/app/app.py",
        )


def test_kernel_and_defaultspack_sealed_hooks_install_the_same_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both role targets propagate exactly the Launcher-issued bundle binding."""

    import app
    from ecosystem.defaultspack.defaultspack import desktop_app

    root = tmp_path / "Tobkiri Launcher.app"
    root.mkdir()
    snapshot_app_root = tmp_path / "sealed-python" / "app"
    snapshot_app_root.mkdir(parents=True)
    scope = _Scope(snapshot_app_root, _binding(root))
    import_path_calls: list[None] = []
    monkeypatch.setattr(desktop_app, "_SEALED_SCOPE", None)
    monkeypatch.setattr(
        desktop_app,
        "_ensure_import_path",
        lambda: import_path_calls.append(None),
    )

    app.prepare_for_sealed_dispatch(scope)
    desktop_app.prepare_for_sealed_dispatch(scope)

    assert packvm_bundle_binding() is not None
    assert desktop_app._SEALED_SCOPE is scope
    assert import_path_calls == [None]


@pytest.mark.parametrize("team_id", ("abcdefghij", "ABC123456", "ABCDEFGHIJ!"))
def test_sealed_scope_rejects_noncanonical_helper_team_identity(
    tmp_path: Path,
    team_id: str,
) -> None:
    """Only the CI identity absence or a canonical ten-character team ID is valid."""

    root = tmp_path / "Tobkiri Launcher.app"
    root.mkdir()
    app_root = tmp_path / "sealed-python" / "app"
    app_root.mkdir(parents=True)
    raw = dict(_binding(root))
    raw["helper_team_id"] = team_id

    with pytest.raises(PackagedApplicationBundleBindingError, match="team identity"):
        install_packvm_bundle_binding_from_sealed_scope(
            _Scope(app_root, MappingProxyType(raw)),
            "/sealed/app/app.py",
        )


@pytest.mark.parametrize(
    "raw_binding",
    (
        {"root": "/tmp/Tobkiri Launcher.app"},
        MappingProxyType(
            {
                "root": "/tmp/Tobkiri Launcher.app",
                "provisioning_sha256": "a" * 64,
                "helper_manifest_sha256": _digest("b"),
                "helper_team_id": "ABCDEFGHIJ",
            }
        ),
        MappingProxyType(
            {
                "root": "/tmp/Tobkiri Launcher.app",
                "provisioning_sha256": _digest("a"),
                "helper_manifest_sha256": _digest("b"),
                "helper_team_id": "ABCDEFGHIJ",
                "unexpected": "value",
            }
        ),
    ),
)
def test_sealed_scope_rejects_mutable_or_malformed_bundle_evidence(
    tmp_path: Path,
    raw_binding: object,
) -> None:
    """There is no raw-value or environment-backed bundle binding path."""

    root = tmp_path / "Tobkiri Launcher.app"
    root.mkdir()
    app_root = tmp_path / "sealed-python" / "app"
    app_root.mkdir(parents=True)
    scope = _Scope(app_root, raw_binding)  # type: ignore[arg-type]

    with pytest.raises(PackagedApplicationBundleBindingError):
        install_packvm_bundle_binding_from_sealed_scope(scope, "/sealed/app/app.py")
    assert packvm_bundle_binding() is None
