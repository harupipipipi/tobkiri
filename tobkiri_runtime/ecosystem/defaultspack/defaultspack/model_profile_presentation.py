"""Project captured model-registry reads for the Defaults model selector."""

from __future__ import annotations

from typing import Mapping
import re


MODEL_PROFILE_LIST_TARGET = (
    "defaults.models.profiles.list",
    "tobkiri.resource.ai.model.profile.v1",
    "rumi_model_registry_pack.model-profile-resource",
    "rumi_model_registry_pack.model-registry.profile",
    "rumi_model_registry_pack.model-registry.profile",
)

MODEL_PROFILE_SAVE_TARGET = (
    "defaults.models.profiles.save",
    "tobkiri.action.ai.model.profile.manage.v1",
    "rumi_model_registry_pack.model-profile-manage",
    "rumi_model_registry_pack.model-registry.manage",
    "rumi_model_registry_pack.model-registry.manage",
)


def normalize_model_profile_save(payload: Mapping[str, object]) -> dict[str, object]:
    """Accept model routing data, never credentials, authority or migration input."""
    fields = {"model_profile_id", "model_id", "provider_instance_id", "display_name", "expected_revision"}
    if set(payload) != fields:
        raise ValueError("model configuration fields are invalid")
    revision = payload["expected_revision"]
    if type(revision) is not int or revision < 0:
        raise ValueError("model configuration revision is invalid")
    for key in ("model_profile_id", "model_id", "provider_instance_id"):
        value = payload[key]
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value) is None:
            raise ValueError("model configuration identity is invalid")
    name = payload["display_name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("model configuration name is invalid")
    return {
        "operation": "save", "expected_revision": revision,
        "record": {
            "model_profile_id": payload["model_profile_id"],
            "model_id": payload["model_id"], "display_name": name,
            "requirements": {"preferred_provider_instance_id": payload["provider_instance_id"]},
        },
    }


def present_model_profile_saved(result: Mapping[str, object]) -> dict[str, object]:
    """Return only the saved model selector identity and owner revision."""
    if result.get("state") == "error":
        return dict(result)
    revision = result.get("store_revision")
    if result.get("action") != "saved" or type(revision) is not int:
        raise ValueError("model configuration save was not confirmed")
    return present_model_profiles({"profiles": [result.get("profile")], "revision": revision})


def present_model_profiles(result: Mapping[str, object]) -> dict[str, object]:
    """Expose model identities, never credentials, metadata or parameters."""

    if result.get("state") == "error":
        return dict(result)
    profiles = result.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("model registry returned an invalid profile list")
    projected: list[dict[str, object]] = []
    for profile in profiles:
        if not isinstance(profile, Mapping):
            raise ValueError("model registry returned an invalid profile")
        record: dict[str, object] = {}
        for source, destination in (
            ("model_profile_id", "profile_id"),
            ("display_name", "display_name"),
            ("model_id", "model_id"),
        ):
            value = profile.get(source)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("model registry returned an invalid identity")
            record[destination] = value
        enabled = profile.get("enabled")
        requirements = profile.get("requirements")
        if isinstance(requirements, Mapping):
            provider = requirements.get("preferred_provider_instance_id")
            if isinstance(provider, str) and provider:
                record["provider_id"] = provider
                # This confirms a stored route, not credentials or reachability.
                record["route_configured"] = True
        if not isinstance(enabled, bool):
            raise ValueError("model registry returned an invalid enabled state")
        # A configured model is not evidence that its Provider is reachable.
        # Disabled profiles are omitted rather than advertised as selectable.
        if enabled:
            projected.append(record)
    result_view: dict[str, object] = {"profiles": projected, "count": len(projected)}
    if type(result.get("revision")) is int:
        result_view["registry_revision"] = result["revision"]
    return result_view
