"""Additive Profile proposals preserve user state and are not approvals."""

from copy import deepcopy

import pytest

from core_runtime.bootstrap.profile_source_update import profile_source_additions
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStoreConflict


def _profile() -> dict:
    return {
        "profile_id": "personal",
        "display_name": "My existing Profile",
        "base": {"pack_id": "base"},
        "shell": {"provider_id": "shell"},
        "packs": [{"pack_id": "existing", "role": "provider"}],
        "requested_edges": [
            {
                "caller_function_id": "shell",
                "target_provider_id": "existing.read",
                "contract_id": "resource.read.v1",
                "operation_id": "read",
                "requested_scope_template": {"dimensions": {"operation": ["read"]}},
            }
        ],
        "state": {"user_setting": "preserve"},
    }


def test_additions_preserve_user_choices_and_leave_inputs_detached() -> None:
    current = _profile()
    source = deepcopy(current)
    source["display_name"] = "Packaged name"
    source["state"] = {}
    source["packs"].append({"pack_id": "new", "role": "provider"})
    edge = deepcopy(source["requested_edges"][0])
    edge["target_provider_id"] = "new.read"
    source["requested_edges"].append(edge)
    before = deepcopy((current, source))

    result = profile_source_additions(current, source)

    assert result["display_name"] == current["display_name"]
    assert result["state"] == current["state"]
    assert result["packs"] == source["packs"]
    assert result["requested_edges"] == source["requested_edges"]
    result["requested_edges"][-1]["requested_scope_template"]["dimensions"].clear()
    assert (current, source) == before


def test_source_omissions_do_not_remove_existing_selections_or_edges() -> None:
    current = _profile()
    source = deepcopy(current)
    source["packs"] = []
    source["requested_edges"] = []
    assert profile_source_additions(current, source) == current


@pytest.mark.parametrize("field", ["profile_id", "base", "shell"])
def test_profile_identity_and_presentation_replacement_are_rejected(field: str) -> None:
    current = _profile()
    source = deepcopy(current)
    source[field] = "replacement"
    with pytest.raises(ProfileDefinitionStoreConflict, match="cannot replace"):
        profile_source_additions(current, source)


@pytest.mark.parametrize("field", ["packs", "requested_edges"])
def test_conflicting_existing_definitions_are_not_silently_overwritten(field: str) -> None:
    current = _profile()
    source = deepcopy(current)
    if field == "packs":
        source[field][0]["role"] = "application"
    else:
        source[field][0]["requested_scope_template"]["dimensions"]["operation"].append(
            "write"
        )
    with pytest.raises(ProfileDefinitionStoreConflict, match="conflicts"):
        profile_source_additions(current, source)


@pytest.mark.parametrize("side", ["current", "source"])
@pytest.mark.parametrize("field", ["packs", "requested_edges"])
def test_duplicate_identities_are_rejected(side: str, field: str) -> None:
    profiles = {"current": _profile(), "source": _profile()}
    profiles[side][field].append(deepcopy(profiles[side][field][0]))
    with pytest.raises(ProfileDefinitionStoreConflict, match="duplicate"):
        profile_source_additions(**profiles)
