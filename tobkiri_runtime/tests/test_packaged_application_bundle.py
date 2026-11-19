"""Focused tests for sealed packaged PackVM bundle binding."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock

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
    def __init__(self, app_root: Path, binding: MappingProxyType | None) -> None:
        self.app_root = app_root
        self.binding = binding
        self.app_root_calls: list[str | Path] = []
        self.binding_calls: list[str | Path] = []
        self.native_first_setup = False
        self.setup_policy_calls: list[str | Path] = []
        self.packvm_disabled = False
        self.packvm_policy_calls: list[str | Path] = []

    def app_root_for(self, module_file: str | Path) -> Path:
        self.app_root_calls.append(module_file)
        return self.app_root

    def packvm_bundle_binding_for(
        self, module_file: str | Path,
    ) -> MappingProxyType | None:
        self.binding_calls.append(module_file)
        return self.binding

    def native_first_setup_for(self, module_file: str | Path) -> bool:
        self.setup_policy_calls.append(module_file)
        return self.native_first_setup

    def packvm_disabled_for(self, module_file: str | Path) -> bool:
        self.packvm_policy_calls.append(module_file)
        return self.packvm_disabled


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
    scope = _Scope(app_root, None)

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


@pytest.mark.parametrize("packvm_disabled", [False, True])
def test_kernel_and_defaultspack_sealed_hooks_apply_launch_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packvm_disabled: bool,
) -> None:
    """Both role targets capture the Launcher-issued policy for their target."""

    import app
    from ecosystem.defaultspack.defaultspack import desktop_app, runtime_composition

    root = tmp_path / "Tobkiri Launcher.app"
    root.mkdir()
    snapshot_app_root = tmp_path / "sealed-python" / "app"
    snapshot_app_root.mkdir(parents=True)
    scope = _Scope(snapshot_app_root, None if packvm_disabled else _binding(root))
    scope.native_first_setup = True
    scope.packvm_disabled = packvm_disabled
    import_path_calls: list[None] = []
    monkeypatch.setattr(desktop_app, "_SEALED_SCOPE", None)
    monkeypatch.setattr(desktop_app, "_SEALED_PACKVM_DISABLED", None)
    monkeypatch.setattr(app, "_SEALED_NATIVE_FIRST_SETUP", None)
    monkeypatch.setattr(app, "_SEALED_PACKVM_DISABLED", None)
    monkeypatch.setattr(
        desktop_app,
        "_ensure_import_path",
        lambda: import_path_calls.append(None),
    )

    app.prepare_for_sealed_dispatch(scope)
    desktop_app.prepare_for_sealed_dispatch(scope)

    assert (packvm_bundle_binding() is None) is packvm_disabled
    assert desktop_app._SEALED_SCOPE is scope
    assert import_path_calls == [None]
    assert scope.setup_policy_calls == [app.__file__]
    assert scope.packvm_policy_calls == [app.__file__, desktop_app.__file__]
    assert app._SEALED_NATIVE_FIRST_SETUP is True
    assert app._SEALED_PACKVM_DISABLED is packvm_disabled
    assert desktop_app._SEALED_PACKVM_DISABLED is packvm_disabled
    kernel_factory = Mock()
    monkeypatch.setattr(runtime_composition, "create_defaultspack_kernel", kernel_factory)
    assert app._create_defaultspack_kernel() is kernel_factory.return_value
    kernel_factory.assert_called_once_with(
        native_first_setup=True, packvm_disabled=packvm_disabled,
    )
    scope.packvm_disabled = not packvm_disabled
    with pytest.raises(RuntimeError, match="PackVM policy was already established"):
        app.prepare_for_sealed_dispatch(scope)
    with pytest.raises(RuntimeError, match="PackVM policy was already established"):
        desktop_app.prepare_for_sealed_dispatch(scope)
    scope.native_first_setup = False
    with pytest.raises(RuntimeError, match="already established"):
        app.prepare_for_sealed_dispatch(scope)
    with pytest.raises(SystemExit):
        app._parser().parse_args(["--native-first-setup"])
    with pytest.raises(SystemExit):
        app._parser().parse_args(["--packvm-disabled"])
    with pytest.raises(SystemExit):
        desktop_app._parse_cli_args(["--packvm-disabled"])


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
