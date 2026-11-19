"""Confine native Model Registry calls to the signed Settings routes."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

from .global_contracts.http_contract_dispatch import HTTPContractBinding

if TYPE_CHECKING:
    from tobkiri_host.broker import RequestEnvelope


PACK = "rumi_model_registry_pack"
FUNCTION = f"{PACK}.model-registry.profile"
CONTRACT = "tobkiri.resource.ai.model.profile.v1"
OPERATION = f"{PACK}.model-profile-resource"
SAVE_FUNCTION = f"{PACK}.model-registry.manage"
SAVE_CONTRACT = "tobkiri.action.ai.model.profile.manage.v1"
SAVE_OPERATION = f"{PACK}.model-profile-manage"
_SAVE_PAYLOAD_KEYS = frozenset({
    "model_profile_id",
    "model_id",
    "provider_instance_id",
    "display_name",
    "expected_revision",
    "provider_registry_revision",
    "max_output_tokens",
})
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


def is_model_settings_save(binding: HTTPContractBinding) -> bool:
    """Recognize the signed Settings POST and its exact sole target."""
    if (
        binding.method != "POST"
        or binding.path != "/api/ai/profiles"
        or binding.presentation != "model_profile_saved"
        or binding.application_id != "runtime.tauri.application.default"
        or len(binding.targets) != 1
    ):
        return False
    target = binding.targets[0]
    return (
        target.contribution_id == "defaults.models.profiles.save"
        and target.contract_id == SAVE_CONTRACT
        and target.operation_id == SAVE_OPERATION
        and target.provider_id == target.function_id == SAVE_FUNCTION
        and target.allowed_payload_keys == _SAVE_PAYLOAD_KEYS
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


@dataclass(frozen=True)
class ModelSaveHTTPScope(ModelSettingsHTTPScope):
    """Keep the signed request forms and its one possible publication together."""

    runtime_instance: str
    request: Mapping[str, object]
    normalized_payload: Mapping[str, object]
    _publication_lock: Lock = field(
        default_factory=Lock, init=False, repr=False, compare=False,
    )
    _publication_envelopes: list[RequestEnvelope] = field(
        default_factory=list, init=False, repr=False, compare=False,
    )

    def mark_publication_possible(self, envelope: RequestEnvelope) -> None:
        """Arm the marker before offering a permit for this exact Broker call."""
        with self._publication_lock:
            if (
                self._publication_envelopes
                or envelope.cancellation_requested.is_set()
            ):
                raise PermissionError("Model Settings publication is no longer permitted")
            self._publication_envelopes.append(envelope)

    def publication_possible(self, envelope: RequestEnvelope) -> bool:
        """Tell Broker cancellation whether this exact call might have published."""
        with self._publication_lock:
            return bool(
                self._publication_envelopes and self._publication_envelopes[0] is envelope
            )

    def publication_was_possible(self) -> bool:
        """Keep the journal uncertain if dispatch lost the Broker outcome."""
        with self._publication_lock:
            return bool(self._publication_envelopes)


_save_scope: ContextVar[ModelSaveHTTPScope | None] = ContextVar(
    "native_model_save_http_scope", default=None,
)


@contextmanager
def bind_model_save_http_scope(scope: ModelSaveHTTPScope) -> Iterator[None]:
    """Open the save borrow only around the authenticated Broker dispatch."""
    token = _save_scope.set(scope)
    try:
        yield
    finally:
        _save_scope.reset(token)


def current_model_save_http_scope() -> ModelSaveHTTPScope | None:
    """Return the private save proof inherited by the Broker worker."""
    return _save_scope.get()


def publication_possible(envelope: RequestEnvelope) -> bool:
    """Check publication on the same envelope across copied Broker contexts."""
    scope = current_model_save_http_scope()
    return scope is not None and scope.publication_possible(envelope)


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
