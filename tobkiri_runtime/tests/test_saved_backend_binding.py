"""Saved bridge forwarding cannot promote readiness or legacy drivers."""

from types import SimpleNamespace

import pytest

from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.platform_backends import ProductionIsolationBackend


def _backend(binder=None) -> ProductionIsolationBackend:
    driver = SimpleNamespace(
        backend_id="tobkiri.python-pack-v4", platform="macos-arm64",
        backend_digest="sha256:" + "a" * 64, capability=lambda: (False, "not provisioned"),
        bind_capability_bridge=lambda callback: None,
    )
    if binder is not None:
        driver.bind_saved_capability_bridge = binder
    return ProductionIsolationBackend(driver)


def test_saved_binding_forwards_both_hooks_without_changing_readiness() -> None:
    calls = []
    backend = _backend(lambda *hooks: calls.append(hooks))
    callback = lambda request, frame: {"status": "ok", "value": {}}
    preflight = lambda request: None
    before = backend.status
    backend.bind_saved_capability_bridge(callback, preflight)
    assert calls == [(callback, preflight)]
    assert backend.status == before
    assert not backend.status.production_enabled
    with pytest.raises(BackendUnavailableError, match="already bound"):
        backend.bind_saved_capability_bridge(callback, lambda request: None)


def test_v1_support_does_not_imply_saved_support() -> None:
    backend = _backend()
    with pytest.raises(BackendUnavailableError, match="does not support"):
        backend.bind_saved_capability_bridge(lambda request, frame: {}, lambda request: None)
    assert backend._saved_bridge is None


@pytest.mark.parametrize("state", ["_domains", "_reservations"])
def test_materialized_or_reserved_backend_cannot_change_saved_hooks(state: str) -> None:
    calls = []
    backend = _backend(lambda *hooks: calls.append(hooks))
    getattr(backend, state)["existing"] = object()
    with pytest.raises(BackendUnavailableError, match="after materialization"):
        backend.bind_saved_capability_bridge(lambda request, frame: {}, lambda request: None)
    assert not calls


def test_failed_supervisor_binding_does_not_record_a_successful_binding() -> None:
    def denied(*args: object) -> None:
        raise BackendUnavailableError("supervisor rejected binding")
    backend = _backend(denied)
    with pytest.raises(BackendUnavailableError, match="supervisor rejected"):
        backend.bind_saved_capability_bridge(lambda request, frame: {}, lambda request: None)
    assert backend._saved_bridge is None
