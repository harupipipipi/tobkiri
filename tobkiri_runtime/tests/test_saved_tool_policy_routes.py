"""Capture existing signed Python topology into bounded native selections."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from core_runtime.authority.v4 import AuthorityScope, LeaseState
from core_runtime.saved_tool_policy_routes_v4 import (
    COORDINATOR,
    FILE_TOOL,
    INNER_CONTRACT,
    INNER_OPERATION,
    LOCAL_CONTRACT,
    LOCAL_EXECUTOR,
    LOCAL_OPERATIONS,
    PYTHON_SANDBOX_OPERATION,
    finite_saved_policy_routes,
)
from tobkiri_host.models import EffectClass
from tobkiri_host.policy_delegation_boundary import BoundedPolicySelectionController


def captured_inputs(tmp_path: Path) -> NS:
    """Model verified bindings using the canonical Profile's unchanged scopes."""
    root = Path(__file__).resolve().parents[1]
    profile = json.loads(
        (root / "ecosystem/defaultspack/v4/defaults.profile.v5.json").read_text()
    )
    edges, bindings = [], []
    for item in profile["requested_edges"]:
        scope = AuthorityScope.from_dict(item["requested_scope_template"])
        binding = NS(
            function=NS(function_id=item["target_provider_id"]),
            principal_ref=NS(value=item["target_provider_id"]),
            artifact=NS(publisher_lineage="publisher.fixture"),
            operation=NS(
                contract_id=item["contract_id"],
                operation_id=item["operation_id"],
                revision_digest=scope.semantics_digest,
                effect_class=EffectClass.READ,
            ),
        )
        edges.append(
            NS(
                caller=NS(
                    function_id=item["caller_function_id"],
                    principal_id=item["caller_function_id"],
                ),
                target=NS(principal_id=item["target_provider_id"]),
                resolved_binding=binding,
                ceilings=NS(caller_effect=scope),
            )
        )
        if not any(b.principal_ref == binding.principal_ref for b in bindings):
            bindings.append(binding)
    invocation = NS(
        assert_current=lambda: None,
        envelope=NS(lease=NS(token=b"fixture-parent")),
    )
    parent = NS(call_chain=("saved-owner",), caller=NS(principal_id="executor-parent"))
    return NS(
        invocation=invocation,
        store=NS(inspect_lease_token=lambda token: (parent, LeaseState.DISPATCHED)),
        edges=edges,
        bindings=bindings,
        workspace_binding=NS(
            workspace_id="workspace",
            canonical_root=tmp_path.resolve(),
            mount_revision=1,
            root_st_dev=1,
            root_st_ino=2,
        ),
    )


def capture(inputs: NS) -> tuple[dict, ...]:
    """Run the production route producer with captured test topology."""
    return finite_saved_policy_routes(
        inputs.invocation,
        store=inputs.store,
        edges=tuple(inputs.edges),
        bindings=tuple(inputs.bindings),
        workspace_binding=inputs.workspace_binding,
    )


def test_canonical_four_routes_keep_python_scope_and_exclude_guest(
    tmp_path: Path,
) -> None:
    inputs = captured_inputs(tmp_path)
    routes = capture(inputs)
    assert {(r["contract"], r["operation"]) for r in routes} == {
        *((LOCAL_CONTRACT, operation) for operation in LOCAL_OPERATIONS),
        (INNER_CONTRACT, INNER_OPERATION),
    }
    python = next(r for r in routes if r["operation"] == PYTHON_SANDBOX_OPERATION)
    signed = next(
        e
        for e in inputs.edges
        if e.resolved_binding.operation.operation_id == PYTHON_SANDBOX_OPERATION
    )
    assert python["ceiling"] == signed.ceilings.caller_effect.to_dict()
    assert python["caller_principal_id"] == LOCAL_EXECUTOR
    assert python["ancestor_chain"] == ["saved-owner", "executor-parent"]
    assert python["workspace_receipt"]["canonical_root"] == str(tmp_path.resolve())
    assert all(
        r["operation"] != "defaultspack.python-sandbox-guest-execute" for r in routes
    )
    create = next(r for r in routes if r["operation"] == INNER_OPERATION)
    assert create["caller_principal_id"] == COORDINATOR
    assert create["ancestor_chain"] == [FILE_TOOL]


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "contract", "target", "scope"]
)
def test_python_capture_rejects_missing_duplicate_or_rebound_route(
    tmp_path: Path,
    change: str,
) -> None:
    inputs = captured_inputs(tmp_path)
    python = next(
        e
        for e in inputs.edges
        if e.resolved_binding.operation.operation_id == PYTHON_SANDBOX_OPERATION
    )
    if change == "missing":
        inputs.edges.remove(python)
    elif change == "duplicate":
        inputs.edges.append(python)
    elif change == "contract":
        python.resolved_binding.operation.contract_id = "foreign.contract.v1"
    elif change == "target":
        python.target.principal_id = "foreign-target"
    else:
        scope = python.ceilings.caller_effect.to_dict()
        scope["dimensions"]["operation"] = ["foreign-operation"]
        python.ceilings.caller_effect = AuthorityScope.from_dict(scope)
    with pytest.raises(PermissionError):
        capture(inputs)


def test_four_route_full_native_metadata_preserves_existing_display_budget(
    tmp_path: Path,
) -> None:
    routes = deepcopy(capture(captured_inputs(tmp_path)))
    for route in routes:
        route.update(
            target_principal_id="principal-" + "a" * 64,
            artifact_digest="sha256:" + "b" * 64,
            implementation_digest="sha256:" + "c" * 64,
        )
    boundary = {
        "authorization_kind": "native_selected_policy_exact_v1",
        "routes": list(routes),
        "reviewer_boundary": {},
        "workspace_root": str(tmp_path.resolve()),
    }
    controller = object.__new__(BoundedPolicySelectionController)
    metadata = controller._native_boundary_metadata(
        {
            "delegation_boundary": boundary,
            "reviewer": {},
            "mode": "full",
        }
    )
    parts = [
        value
        for key, value in sorted(metadata.items())
        if key.startswith("boundary_part_")
    ]
    assert len(parts) <= 12
    assert all(len(value) <= 512 for value in metadata.values())
    assert json.loads("".join(parts)) == boundary
    assert PYTHON_SANDBOX_OPERATION in metadata["allowed_operations"]
