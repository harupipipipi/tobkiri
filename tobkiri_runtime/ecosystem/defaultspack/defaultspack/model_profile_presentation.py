"""Project captured model-registry reads for the Defaults model selector."""

from __future__ import annotations

from typing import Mapping


MODEL_PROFILE_LIST_TARGET = (
    "defaults.models.profiles.list",
    "tobkiri.resource.ai.model.profile.v1",
    "rumi_model_registry_pack.model-profile-resource",
    "rumi_model_registry_pack.model-registry.profile",
    "rumi_model_registry_pack.model-registry.profile",
)


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
        if not isinstance(enabled, bool):
            raise ValueError("model registry returned an invalid enabled state")
        # A configured model is not evidence that its Provider is reachable.
        # Disabled profiles are omitted rather than advertised as selectable.
        if enabled:
            projected.append(record)
    return {"profiles": projected, "count": len(projected)}
