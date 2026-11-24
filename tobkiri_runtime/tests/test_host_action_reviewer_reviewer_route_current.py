"""Authenticated route owner local revision checks do no network or Broker IO."""

from types import SimpleNamespace as NS
import pytest
from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.action_review_v4.reviewer_route_current import (
    capture_local_route_current,
)
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_model_catalog_pack.runtime import catalog


def contexts(root):
    def context(function):
        return NS(
            profile_id="profile-1",
            user_data_root=root,
            provider_bindings=(NS(function=NS(function_id=function)),),
        )

    return (
        context("rumi_model_registry_pack.model-registry.profile"),
        context("rumi_provider_registry_pack.provider-registry.resource"),
        context("rumi_model_catalog_pack.model-catalog.bundled"),
    )


def test_actual_owner_snapshots_detect_registry_and_catalog_changes(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network catalog refresh forbidden")

    monkeypatch.setattr(catalog, "_openrouter_inventory", forbidden)
    guard = capture_local_route_current(*contexts(tmp_path), assert_current=lambda: None)
    route = {
        "catalog_revision": catalog.CATALOG_REVISION,
        "pricing_revision": catalog.CATALOG_REVISION,
    }
    assert guard(route)
    monkeypatch.setattr(ModelRegistry, "snapshot", lambda self: {"revision": 123})
    assert not guard(route)


def test_provider_revision_change_and_catalog_price_revision_deny(tmp_path, monkeypatch):
    guard = capture_local_route_current(*contexts(tmp_path), assert_current=lambda: None)
    route = {"catalog_revision": catalog.CATALOG_REVISION, "pricing_revision": "wrong"}
    assert not guard(route)
    route["pricing_revision"] = catalog.CATALOG_REVISION
    monkeypatch.setattr(ProviderRegistry, "snapshot", lambda self: {"revision": 321})
    assert not guard(route)


def test_wrong_source_owner_fails_closed(tmp_path):
    model, provider, bundled = contexts(tmp_path)
    provider.provider_bindings = (NS(function=NS(function_id="wrong")),)
    with pytest.raises(AuthorityDenied):
        capture_local_route_current(model, provider, bundled, assert_current=lambda: None)
