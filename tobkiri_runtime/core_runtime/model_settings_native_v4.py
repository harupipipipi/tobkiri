"""Keep the native Model Registry projection inside the signed Settings read."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .global_contracts.http_contract_dispatch import HTTPContractBinding


PACK = "rumi_model_registry_pack"
FUNCTION = f"{PACK}.model-registry.profile"
CONTRACT = "tobkiri.resource.ai.model.profile.v1"
OPERATION = f"{PACK}.model-profile-resource"
MINIMUM_MEMORY = 10 * 1024 * 1024


def is_model_settings_read(binding: HTTPContractBinding) -> bool:
    """Recognize the original signed application route and its sole target."""
    if (
        binding.method != "GET"
        or binding.path != "/api/ai/profiles"
        or binding.presentation != "model_profile_list"
        or binding.application_id != "runtime.tauri.application.default"
        or len(binding.targets) != 1
    ):
        return False
    target = binding.targets[0]
    return (
        target.contribution_id == "defaults.models.profiles.list"
        and target.contract_id == CONTRACT
        and target.operation_id == OPERATION
        and target.provider_id == target.function_id == FUNCTION
        and not target.allowed_payload_keys
    )


@dataclass(frozen=True)
class ModelSettingsHTTPScope:
    """Retain the authenticated panel session and exact serving generation."""

    panel_session_id: str
    session: object
    runtime_instance: str | None
    assert_current: Callable[[], None]


_scope: ContextVar[ModelSettingsHTTPScope | None] = ContextVar(
    "native_model_settings_http_scope", default=None,
)


@contextmanager
def bind_model_settings_http_scope(scope: ModelSettingsHTTPScope) -> Iterator[None]:
    """Open the borrow only around the authenticated request's Broker dispatch."""
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def current_model_settings_http_scope() -> ModelSettingsHTTPScope | None:
    """Return the private HTTP proof inherited by the Broker worker."""
    return _scope.get()


def model_settings_presenter_input(
    projected: Mapping[str, object],
) -> dict[str, object]:
    """Validate projected rows and adapt only those fields for the signed presenter."""
    invalid = "native Model Registry returned an invalid Settings projection"
    if not isinstance(projected, Mapping) or set(projected) != {
        "profiles", "count", "registry_revision",
    }:
        raise RuntimeError(invalid)
    profiles = projected["profiles"]
    count = projected["count"]
    revision = projected["registry_revision"]
    if (
        not isinstance(profiles, list)
        or type(count) is not int
        or count != len(profiles)
        or type(revision) is not int
    ):
        raise RuntimeError(invalid)

    required = {"profile_id", "display_name", "model_id"}
    allowed = required | {"max_output_tokens", "provider_id", "route_configured"}
    rows: list[dict[str, object]] = []
    for profile in profiles:
        if not isinstance(profile, Mapping) or not required <= set(profile) <= allowed:
            raise RuntimeError(invalid)
        if any(
            not isinstance(profile[key], str) or not profile[key].strip()
            for key in required
        ):
            raise RuntimeError(invalid)
        row = {
            "model_profile_id": profile["profile_id"],
            "display_name": profile["display_name"],
            "model_id": profile["model_id"],
            "enabled": True,
        }
        if "max_output_tokens" in profile:
            cap = profile["max_output_tokens"]
            if type(cap) is not int or not 1 <= cap <= 131072:
                raise RuntimeError(invalid)
            row["parameters"] = {"max_tokens": cap}
        if "provider_id" in profile or "route_configured" in profile:
            provider = profile.get("provider_id")
            if not isinstance(provider, str) or not provider or (
                profile.get("route_configured") is not True
            ):
                raise RuntimeError(invalid)
            row["metadata"] = {"provider_connection_id": provider}
        rows.append(row)
    return {"profiles": rows, "revision": revision}
