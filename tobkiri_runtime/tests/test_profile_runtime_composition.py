"""Regression tests for the explicit application Profile composition root."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("missing", [None, "contract", "operation", "provider"])
def test_http_selection_requires_exact_profile_binding(missing: str | None) -> None:
    """Unselected application routes require review, not implicit new grants."""
    from core_runtime.global_contracts.http_contract_dispatch import (
        HTTPContractBinding, HTTPContractTarget,
    )
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        _require_http_provider_selection,
    )
    from ecosystem.defaultspack.domain.runtime_v4 import ProfileReconfirmationRequired

    target = HTTPContractTarget("ui", "contract", "operation", "provider", "provider")
    route = HTTPContractBinding("POST", "/api/chat/turn", "broker_result", (target,))
    edge = {
        "contract_id": "different" if missing == "contract" else "contract",
        "operation_id": "different" if missing == "operation" else "operation",
        "function_principal": {
            "function_id": "different" if missing == "provider" else "provider",
        },
    }
    active = SimpleNamespace(resolved=SimpleNamespace(plan={"bindings": [edge]}))
    if missing is None:
        _require_http_provider_selection(active, (route,))
    else:
        with pytest.raises(ProfileReconfirmationRequired, match="contract/operation"):
            _require_http_provider_selection(active, (route,))
    assert active.resolved.plan == {"bindings": [edge]}


@pytest.mark.parametrize("explicit", [False, True])
def test_capture_uses_host_selected_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    """Dispatch keeps the bundle selected by Host activation or explicit injection."""
    from core_runtime.bootstrap import profile_capture
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_runtime_capture_inputs,
    )
    from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

    selected = tmp_path / "generated-bundle"
    injected = tmp_path / "explicit-bundle"
    monkeypatch.setattr(profile_capture, "_bundle_root", lambda: selected)
    observed: list[Path] = []

    class CatalogObserved(Exception):
        pass

    def load_catalog(cls: type, root: Path) -> None:
        observed.append(root)
        raise CatalogObserved

    monkeypatch.setattr(BundledCatalog, "load", classmethod(load_catalog))
    with pytest.raises(CatalogObserved):
        defaultspack_runtime_capture_inputs(
            bundle_root=injected if explicit else None,
        )
    assert observed == [injected if explicit else selected]


def test_runtime_v4_import_does_not_install_profile_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sealed catalog package remains importable without Host modules."""

    import core_runtime.profile_runtime_port as profile_port
    import ecosystem.defaultspack.domain.runtime_v4 as runtime_v4

    monkeypatch.setattr(profile_port, "_PROFILE_RUNTIME", None)
    importlib.reload(runtime_v4)

    with pytest.raises(profile_port.ProfileRuntimeUnavailable):
        profile_port.require_profile_runtime()


def test_defaultspack_application_composition_installs_profile_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the application composition root supplies Defaultspack records."""

    import core_runtime.profile_runtime_port as profile_port
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        create_defaultspack_kernel,
    )

    monkeypatch.setattr(profile_port, "_PROFILE_RUNTIME", None)
    kernel = create_defaultspack_kernel()
    try:
        assert profile_port.require_profile_runtime().bootstrap_profile_id() == "defaults"
    finally:
        kernel.shutdown()


def test_defaultspack_kernel_preserves_host_credential_factory_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The application composition forwards the exact Host factory port."""

    import core_runtime.profile_runtime_port as profile_port
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        create_defaultspack_kernel,
    )

    def credential_store_factory(*, user_data_root: Path) -> object:
        del user_data_root
        return object()

    monkeypatch.setattr(profile_port, "_PROFILE_RUNTIME", None)
    kernel = create_defaultspack_kernel(
        credential_store_factory=credential_store_factory,
    )
    try:
        capture_factory = kernel._runtime_capture_factory
        assert capture_factory.keywords["credential_store_factory"] is (
            credential_store_factory
        )
    finally:
        kernel.shutdown()


def test_profile_port_rejects_replacement_after_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later application cannot replace the captured Profile authority port."""

    import core_runtime.profile_runtime_port as profile_port
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    monkeypatch.setattr(profile_port, "_PROFILE_RUNTIME", None)
    first = install_defaultspack_profile_runtime()
    assert install_defaultspack_profile_runtime() is first
    with pytest.raises(profile_port.ProfileRuntimeAlreadyConfigured):
        profile_port.register_profile_runtime(object())
    assert profile_port.require_profile_runtime() is first


def test_defaultspack_catalog_profile_projection_preserves_pack_inventory() -> None:
    """Host Profile definitions must never replace the sealed Pack map."""

    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        DefaultspackProfileRuntime,
    )
    from ecosystem.defaultspack.domain.runtime_v4.service import BundledCatalog

    catalog = BundledCatalog(
        root=Path("bundle"),
        packs={"pack-a": {"pack": {"id": "pack-a"}}},
        bases={"base-a": {}},
        shells={"shell-a": {}},
        profiles={"old": {"profile_id": "old"}},
        artifact_root=Path("artifacts"),
        executable_catalogs={"pack-a": {"pack_id": "pack-a"}},
    )

    projected = DefaultspackProfileRuntime().catalog_with_profiles(
        catalog,
        {"selected": {"profile_id": "selected"}},
    )

    assert projected.packs is catalog.packs
    assert projected.profiles == {"selected": {"profile_id": "selected"}}
    assert projected.artifact_root == catalog.artifact_root
    assert projected.executable_catalogs is catalog.executable_catalogs
