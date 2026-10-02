"""Exact selected caller operations survive signed Profile and plan boundaries."""

from __future__ import annotations

from copy import deepcopy
import io
import json
from pathlib import Path
from typing import Any
import zipfile

import pytest

from core_runtime.authority.v4 import AuthorityDenied, FunctionPrincipal
from core_runtime.bootstrap.profile_source_update import profile_source_additions
from core_runtime.profile_caller_edges_v4 import (
    SelectedCallerOperationV4,
    resolve_selected_caller,
)
from ecosystem.defaultspack.domain.runtime_v4 import resolve_default_profile
from ecosystem.defaultspack.domain.runtime_v4.service import ProfileResolutionDenied
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.errors import ProtocolError, SchemaValidationError
from tobkiri_protocol.profile_edges import (
    caller_operation_selector,
    captured_edge_identity,
    profile_edge_identity,
    profile_edge_key,
    require_profile_edge_bindings,
)
from tobkiri_protocol.validation import validate_document

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "tobkiri.action.workflow.run.v1"
DIGEST = "sha256:" + "a" * 64


def _edge(operation: str | None = None) -> dict[str, Any]:
    edge: dict[str, Any] = {
        "caller_function_id": "workflow.coordinator",
        "target_provider_id": "schedule.owner",
        "contract_id": "tobkiri.action.schedule.v1",
        "operation_id": "schedule.create",
        "requested_scope_template": {"dimensions": {"operation": ["schedule.create"]}},
        "authority_reference": "authority-ref:test.edge",
    }
    if operation is not None:
        edge.update(caller_contract_id=CONTRACT, caller_operation_id=operation)
    return edge


def _candidate(operation: str, *, revision: str = DIGEST) -> SelectedCallerOperationV4:
    return SelectedCallerOperationV4(
        CONTRACT,
        FunctionPrincipal(DIGEST, DIGEST, "workflow.coordinator", revision, operation),
    )


def _binding(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: value for key, value in edge.items() if key != "requested_scope_template"},
        "requested_scope_digest": canonical_digest(edge["requested_scope_template"]),
    }


def test_legacy_edge_keys_and_unambiguous_selection_are_unchanged() -> None:
    """An omitted selector preserves exact old keys and the selected principal."""
    edge = _edge()
    expected = "workflow.coordinator|schedule.owner|tobkiri.action.schedule.v1|schedule.create"
    assert profile_edge_key(edge) == expected
    assert captured_edge_identity(edge) == (
        "workflow.coordinator",
        "tobkiri.action.schedule.v1",
        "schedule.create",
    )
    candidate = _candidate("run.advance")
    assert resolve_selected_caller(edge, [candidate, candidate]) is candidate.principal


def test_explicit_sibling_callers_never_share_an_authority_identity() -> None:
    """Advance and cancel may call one target while remaining different principals."""
    advance, cancel = _candidate("run.advance"), _candidate("run.cancel")
    for candidate in (advance, cancel):
        edge = _edge(candidate.principal.operation_id)
        assert resolve_selected_caller(edge, [cancel, advance]) is candidate.principal
    assert profile_edge_identity(_edge("run.advance")) != profile_edge_identity(_edge("run.cancel"))
    assert captured_edge_identity(_edge("run.advance")) != captured_edge_identity(
        _edge("run.cancel")
    )


@pytest.mark.parametrize(
    "selector",
    [
        {"caller_contract_id": CONTRACT},
        {"caller_operation_id": "run.advance"},
        {"caller_contract_revision_digest": DIGEST},
        {"caller_contract_id": None, "caller_operation_id": "run.advance"},
        {"caller_contract_id": CONTRACT, "caller_operation_id": "*"},
    ],
)
def test_partial_or_invalid_selector_is_rejected(selector: dict[str, Any]) -> None:
    """No malformed selector becomes a legacy function-name fallback."""
    edge = {**_edge(), **selector}
    with pytest.raises(ProtocolError):
        caller_operation_selector(edge)
    with pytest.raises(AuthorityDenied):
        resolve_selected_caller(edge, [_candidate("run.advance")])


@pytest.mark.parametrize(
    "mutation",
    [
        {"caller_contract_id": "tobkiri.action.other.v1"},
        {"caller_operation_id": "run.unselected"},
        {"caller_contract_revision_digest": "sha256:" + "b" * 64},
        {"caller_function_id": "other.coordinator"},
    ],
)
def test_explicit_selector_matches_only_an_actual_selected_candidate(
    mutation: dict[str, Any],
) -> None:
    """Wrong Contract, unselected sibling, stale revision and another Function deny."""
    with pytest.raises(AuthorityDenied, match="one selected principal"):
        resolve_selected_caller({**_edge("run.advance"), **mutation}, [_candidate("run.advance")])


def test_ambiguous_legacy_and_conflicting_selected_revisions_fail_closed() -> None:
    """Explicit operation selection never chooses the first matching revision."""
    with pytest.raises(AuthorityDenied):
        resolve_selected_caller(_edge(), [_candidate("run.advance"), _candidate("run.cancel")])
    old, new = _candidate("run.advance"), _candidate("run.advance", revision="sha256:" + "b" * 64)
    with pytest.raises(AuthorityDenied):
        resolve_selected_caller(_edge("run.advance"), [old, new])
    assert (
        resolve_selected_caller(
            {**_edge("run.advance"), "caller_contract_revision_digest": DIGEST}, [old, new]
        )
        is old.principal
    )


@pytest.mark.parametrize("change", ["strip", "sibling", "scope", "mode", "reference", "extra"])
def test_plan_cannot_strip_replace_or_widen_the_signed_caller_edge(change: str) -> None:
    """A rehashed plan still needs exact source selector and scope lineage."""
    edge = _edge("run.advance")
    binding = _binding(edge)
    bindings = [binding]
    if change == "strip":
        binding.pop("caller_contract_id")
        binding.pop("caller_operation_id")
    elif change == "sibling":
        binding["caller_operation_id"] = "run.cancel"
    elif change == "scope":
        binding["requested_scope_digest"] = "sha256:" + "b" * 64
    elif change == "mode":
        binding["authority_mode"] = "interactive_only"
    elif change == "reference":
        binding["authority_reference"] = "authority-ref:test.other"
    else:
        bindings.append(_binding(_edge("run.cancel")))
    with pytest.raises(ProtocolError):
        require_profile_edge_bindings([edge], bindings)


def test_source_additions_keep_distinct_explicit_caller_edges() -> None:
    """The additive review proposal preserves each sibling caller independently."""
    current = {
        "profile_id": "personal",
        "base": {},
        "shell": {},
        "packs": [],
        "requested_edges": [_edge("run.advance")],
    }
    source = deepcopy(current)
    source["requested_edges"].append(_edge("run.cancel"))
    assert profile_source_additions(current, source) == source
    assert current["requested_edges"] == [_edge("run.advance")]


def test_native_guest_declares_the_pure_selector_and_its_transitive_ids() -> None:
    """The finite guest closure contains neutral validation, never Host authority."""
    from scripts.build_packvm_guest_bundle import build_guest_bundle

    with zipfile.ZipFile(io.BytesIO(build_guest_bundle(ROOT))) as archive:
        for name in ("profile_edges.py", "ids.py"):
            relative = f"tobkiri_protocol/{name}"
            assert archive.read(relative) == (ROOT / relative).read_bytes()
        assert "core_runtime/profile_caller_edges_v4.py" not in archive.namelist()


@pytest.mark.parametrize("schema", ["profile_intent", "profile_v4.schema.json", "profile"])
def test_profile_schema_accepts_only_the_finite_complete_caller_selector(schema: str) -> None:
    """All current intent/Profile forms accept the pair and reject partial bytes."""
    bundle = ROOT / "ecosystem/defaultspack/v4"
    filename = (
        "defaults.profile.intent.v1.json"
        if schema == "profile_intent"
        else "defaults.profile.v5.json"
    )
    document = json.loads((bundle / filename).read_text())
    if schema == "profile_v4.schema.json":
        document["profile_api_version"] = "io.tobkiri.profile.v4"
        document["shell"].pop("executable_artifact_digest", None)
        document.pop("frontend_entry_id", None)
        for edge in document["requested_edges"]:
            edge.pop("authority_mode", None)
    document["requested_edges"][0].update(
        caller_contract_id=CONTRACT, caller_operation_id="run.advance"
    )
    validate_document(document, schema)
    document["requested_edges"][0].pop("caller_operation_id")
    with pytest.raises(SchemaValidationError):
        validate_document(document, schema)


@pytest.fixture(scope="module")
def selected_profile_result() -> Any:
    """Resolve genuine packaged fixture operations through the normal compiler."""
    from tests.conformance_support.packaged_profile import load_packaged_profile_catalog

    catalog = load_packaged_profile_catalog()
    source = deepcopy(catalog.profiles["defaults"])
    function_id = "rumi_file_inspect_pack.file-inspect.service"
    template = next(
        edge for edge in source["requested_edges"] if edge["target_provider_id"] == function_id
    )
    operations = [
        "rumi_file_inspect_pack.file-inspect",
        "rumi_file_inspect_pack.file-inspect.for-media",
    ]
    for operation in operations:
        if not any(
            edge["target_provider_id"] == function_id and edge["operation_id"] == operation
            for edge in source["requested_edges"]
        ):
            incoming = deepcopy(template)
            incoming["operation_id"] = operation
            incoming["requested_scope_template"]["dimensions"]["operation"] = [operation]
            source["requested_edges"].append(incoming)
        outgoing = deepcopy(template)
        outgoing.update(
            caller_function_id=function_id,
            caller_contract_id=template["contract_id"],
            caller_operation_id=operation,
        )
        source["requested_edges"].append(outgoing)
    catalog.profiles["defaults"] = source
    references = {
        profile_edge_key(edge): "authority-ref:test."
        + canonical_digest(profile_edge_key(edge)).removeprefix("sha256:")
        for edge in source["requested_edges"]
    }
    result = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests={
            item["pack"]["artifact_digest"] for item in catalog.packs.values()
        },
        authority_snapshot_digest=DIGEST,
        authority_bindings=references,
        security_epoch=1,
    )
    return catalog, result, references


def test_normal_compiler_preserves_exact_sibling_selector_in_profile_and_plan(
    selected_profile_result: Any,
) -> None:
    """Two selected caller operations survive immutable signed-plan projection."""
    catalog, result, references = selected_profile_result
    edges = [edge for edge in result.profile["requested_edges"] if "caller_operation_id" in edge]
    bindings = [binding for binding in result.plan["bindings"] if "caller_operation_id" in binding]
    assert len(edges) == len(bindings) == 2
    assert {profile_edge_key(edge) for edge in edges} == {
        profile_edge_key(binding) for binding in bindings
    }
    require_profile_edge_bindings(result.profile["requested_edges"], result.plan["bindings"])
    repeated = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests={
            item["pack"]["artifact_digest"] for item in catalog.packs.values()
        },
        authority_snapshot_digest=DIGEST,
        authority_bindings=references,
        security_epoch=1,
    )
    assert repeated.plan == result.plan


def test_normal_compiler_does_not_accept_an_unselected_operation(
    selected_profile_result: Any,
) -> None:
    """A valid inventory sibling cannot supply authority when absent from the plan."""
    catalog, _, _ = selected_profile_result
    candidate = deepcopy(catalog)
    source = candidate.profiles["defaults"]
    source["requested_edges"] = [
        edge
        for edge in source["requested_edges"]
        if not (
            edge["target_provider_id"] == "rumi_file_inspect_pack.file-inspect.service"
            and edge["operation_id"] == "rumi_file_inspect_pack.file-inspect.for-media"
        )
    ]
    references = {
        profile_edge_key(edge): "authority-ref:test."
        + canonical_digest(profile_edge_key(edge)).removeprefix("sha256:")
        for edge in source["requested_edges"]
    }
    with pytest.raises(ProfileResolutionDenied, match="one selected principal"):
        resolve_default_profile(
            candidate,
            "defaults",
            approved_artifact_digests={
                item["pack"]["artifact_digest"] for item in candidate.packs.values()
            },
            authority_snapshot_digest=DIGEST,
            authority_bindings=references,
            security_epoch=1,
        )
