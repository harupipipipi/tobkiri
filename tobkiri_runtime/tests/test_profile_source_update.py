"""Additive Profile proposals preserve user state and are not approvals."""

from copy import deepcopy
from typing import Any

import pytest

from core_runtime.bootstrap.profile_source_update import (
    profile_scope_successor,
    profile_source_additions,
)
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


def test_packaged_null_preserves_exact_pin_without_changing_authority() -> None:
    current = _profile()
    pin = "sha256:" + "a" * 64
    current["packs"][0]["artifact_digest"] = pin
    current["requested_edges"][0]["authority_mode"] = "profile_grant"
    source = deepcopy(current)
    source["packs"][0]["artifact_digest"] = None
    before = deepcopy((current, source))

    result = profile_source_additions(current, source)

    assert result == current
    assert result["packs"][0]["artifact_digest"] == pin
    result["packs"][0]["artifact_digest"] = "sha256:" + "b" * 64
    result["requested_edges"][0]["requested_scope_template"]["dimensions"].clear()
    assert (current, source) == before


@pytest.mark.parametrize(
    "change",
    ["explicit_pin", "missing_pin", "role", "new_field", "missing_role",
     "scope", "authority"],
)
def test_retained_pack_pin_does_not_hide_other_source_conflicts(change: str) -> None:
    current = _profile()
    current["packs"][0]["artifact_digest"] = "sha256:" + "a" * 64
    source = deepcopy(current)
    source["packs"][0]["artifact_digest"] = None
    if change == "explicit_pin":
        source["packs"][0]["artifact_digest"] = "sha256:" + "b" * 64
    elif change == "missing_pin":
        source["packs"][0].pop("artifact_digest")
    elif change == "role":
        source["packs"][0]["role"] = "application"
    elif change == "new_field":
        source["packs"][0]["selected"] = False
    elif change == "missing_role":
        source["packs"][0].pop("role")
    elif change == "scope":
        source["requested_edges"][0]["requested_scope_template"]["dimensions"][
            "operation"
        ].append("write")
    else:
        source["requested_edges"][0]["authority_mode"] = "delegated"
    before = deepcopy((current, source))

    with pytest.raises(ProfileDefinitionStoreConflict, match="conflicts"):
        profile_source_additions(current, source)

    assert (current, source) == before


@pytest.mark.parametrize(
    "pin", ["not-a-digest", "sha256:" + "a" * 63, "sha256:" + "A" * 64, 1],
)
def test_packaged_null_does_not_accept_an_invalid_current_pin(pin: Any) -> None:
    current = _profile()
    current["packs"][0]["artifact_digest"] = pin
    source = deepcopy(current)
    source["packs"][0]["artifact_digest"] = None

    with pytest.raises(ProfileDefinitionStoreConflict, match="conflicts"):
        profile_source_additions(current, source)


def test_scope_successor_changes_only_matching_semantics_pins() -> None:
    current = _profile()
    current["requested_edges"][0]["requested_scope_template"]["semantics_digest"] = (
        "sha256:" + "1" * 64
    )
    source = deepcopy(current)
    source["display_name"] = "New packaged name"
    source["packs"].append({"pack_id": "new", "role": "provider"})
    new_digest = "sha256:" + "2" * 64
    source["requested_edges"][0]["requested_scope_template"]["semantics_digest"] = new_digest
    before = deepcopy((current, source))
    expected = deepcopy(current)
    expected["requested_edges"][0]["requested_scope_template"]["semantics_digest"] = new_digest
    assert profile_scope_successor(current, source) == expected
    assert (current, source) == before


@pytest.mark.parametrize("change", ["scope", "authority", "provider", "missing"])
def test_scope_successor_does_not_replace_other_edge_changes(change: str) -> None:
    current = _profile()
    current["requested_edges"][0]["requested_scope_template"]["semantics_digest"] = (
        "sha256:" + "1" * 64
    )
    source = deepcopy(current)
    edge = source["requested_edges"][0]
    edge["requested_scope_template"]["semantics_digest"] = "sha256:" + "2" * 64
    if change == "scope":
        edge["requested_scope_template"]["dimensions"]["operation"].append("write")
    elif change == "authority":
        edge["authority_mode"] = "profile_grant"
    elif change == "provider":
        edge["target_provider_id"] = "other.read"
    else:
        source["requested_edges"] = []
    assert profile_scope_successor(current, source) == current


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
