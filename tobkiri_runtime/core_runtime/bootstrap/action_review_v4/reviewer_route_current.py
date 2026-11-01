"""Read-only revision fences owned by the authenticated route contributors."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityDenied, authority_digest
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_model_catalog_pack.runtime import catalog


def capture_local_route_current(
    model_context: Any,
    provider_context: Any,
    catalog_context: Any,
    *,
    assert_current: Callable[[], None],
) -> Callable[[Mapping[str, Any]], bool]:
    """Fence actual route-owner revisions without Broker/network/write work.

    Inputs are actual private Host capture contexts of the signed model profile,
    provider registry and catalog source Functions. Capture before the formal
    quote. A catalog refresh during quoting invalidates capture, requiring a
    fresh attempt; it cannot silently replace the native approval's route.
    """
    expected = (
        (model_context, "rumi_model_registry_pack.model-registry.profile"),
        (provider_context, "rumi_provider_registry_pack.provider-registry.resource"),
        (catalog_context, "rumi_model_catalog_pack.model-catalog.bundled"),
    )
    for context, function in expected:
        if not context.provider_bindings or any(
            binding.function.function_id != function for binding in context.provider_bindings
        ):
            raise AuthorityDenied("authenticated reviewer route owner unavailable")
    if (
        not model_context.profile_id
        or provider_context.profile_id != model_context.profile_id
        or model_context.user_data_root is None
        or provider_context.user_data_root != model_context.user_data_root
    ):
        raise AuthorityDenied("reviewer route owner Profile/root rebound")
    models = ModelRegistry(model_context.profile_id, user_data_root=model_context.user_data_root)
    providers = ProviderRegistry(
        provider_context.profile_id, user_data_root=provider_context.user_data_root
    )

    def fingerprint() -> str:
        assert_current()
        # These owner snapshot paths perform bounded local reads only. Never
        # call create_model_catalog_operation: it may refresh remote inventory.
        bundled_providers, bundled_models = catalog._load_catalog()
        return authority_digest(
            {
                "model_registry": models.snapshot(),
                "provider_registry": providers.snapshot(),
                "catalog_revision": catalog.CATALOG_REVISION,
                "bundled_providers": bundled_providers,
                "bundled_models": bundled_models,
                "memory_inventory": catalog._valid_inventory(catalog._OPENROUTER_MEMORY_INVENTORY),
                "persisted_inventory": catalog._load_openrouter_inventory_cache(),
            }
        )

    captured = fingerprint()

    def current(route: Mapping[str, Any]) -> bool:
        try:
            return (
                isinstance(route, Mapping)
                and route.get("catalog_revision") == catalog.CATALOG_REVISION
                and route.get("pricing_revision") == catalog.CATALOG_REVISION
                and fingerprint() == captured
            )
        except Exception:
            return False

    return current
