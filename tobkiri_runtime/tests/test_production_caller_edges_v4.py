"""Exact multi-operation callers traverse ordinary production Profile capture."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityStore, FunctionPrincipal
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_profile,
    host_profile_catalog,
    prepare_profile_confirmation,
)
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.test_live_production_v4_dispatch import (
    _deny_production_test_internet,  # noqa: F401
)
from tobkiri_host.backends import BackendRegistry
from tobkiri_protocol.canonical import canonical_digest

ROOT = Path(__file__).resolve().parents[1]
PROFILE_ID = "profile-production-callers"
CALLER_FUNCTION = "rumi_file_inspect_pack.file-inspect.service"
CALLER_CONTRACT = "tobkiri.service.file.inspect.v1"
CALLER_OPERATIONS = (
    "rumi_file_inspect_pack.file-inspect",
    "rumi_file_inspect_pack.file-inspect.for-media",
)
TARGET_CONTRACT = "tobkiri.resource.conversation.v1"
TARGET_OPERATION = "rumi_conversation_store_pack.conversation-resource"
TARGET_FUNCTION = "rumi_conversation_store_pack.conversation-store.resource"


@pytest.fixture
def activated_callers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    """Confirm and activate an isolated Named Profile through normal Host owners."""
    user_data = tmp_path / "isolated-host"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    host_profile_catalog()
    definitions = ProfileDefinitionStore(user_data)
    source = deepcopy(definitions.get_profile("defaults").profile)
    shell_function = str(source["shell"]["provider_id"])
    caller_template = next(
        edge for edge in source["requested_edges"] if edge["target_provider_id"] == CALLER_FUNCTION
    )
    target_template = next(
        edge for edge in source["requested_edges"] if edge["target_provider_id"] == TARGET_FUNCTION
    )
    # These public owner reads are intentionally Provider-only in this fixture.
    # A panel cannot claim one of the two callers merely by naming its session.
    source["requested_edges"] = [
        edge
        for edge in source["requested_edges"]
        if not (
            edge["caller_function_id"] == shell_function
            and edge["target_provider_id"] == TARGET_FUNCTION
        )
    ]
    for operation in CALLER_OPERATIONS:
        incoming = deepcopy(caller_template)
        incoming.update(caller_function_id=shell_function, operation_id=operation)
        incoming["requested_scope_template"]["dimensions"]["operation"] = [operation]
        source["requested_edges"].append(incoming)
        outgoing = deepcopy(target_template)
        outgoing.update(
            caller_function_id=CALLER_FUNCTION,
            caller_contract_id=CALLER_CONTRACT,
            caller_operation_id=operation,
        )
        source["requested_edges"].append(outgoing)
    definitions.create_profile(
        source, profile_id=PROFILE_ID, display_name="Exact production callers"
    )
    active = capture_profile(PROFILE_ID, confirmation=prepare_profile_confirmation(PROFILE_ID))
    return active, user_data


def _capture(active: Any, authority: AuthorityStore) -> Any:
    """Use the actual production compiler, durable loader and Host factories."""
    return capture_production_dispatch(
        active,
        bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=ROOT / "ecosystem",
        authority_store=authority,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        # No VM or Wasm backend is launched; selected Host owners remain real.
        backends=BackendRegistry(()),
    )


def test_production_capture_preserves_two_callers_to_one_target_across_restart(
    activated_callers: tuple[Any, Path],
) -> None:
    """Maps, Host factories and persisted grants retain the exact caller principal."""
    active, user_data = activated_callers
    expected_callers = {
        FunctionPrincipal.from_dict(binding["function_principal"]).principal_id
        for binding in active.resolved.plan["bindings"]
        if binding["function_principal"]["function_id"] == CALLER_FUNCTION
    }
    assert len(expected_callers) == 2
    prior_grant_ids: set[str] | None = None
    for _ in range(2):
        with AuthorityStore(user_data / "authority/v4.sqlite3") as authority:
            with _capture(active, authority) as session:
                selected = session.provider_metadata(CALLER_CONTRACT)
                assert {item["operation_id"] for item in selected} == set(CALLER_OPERATIONS)
                assert len(selected) == 2
                assert len(session.provider_metadata(TARGET_CONTRACT)) == 1
                grants = [
                    grant
                    for grant in authority.list_grants()
                    if grant.caller.function_id == CALLER_FUNCTION
                    and grant.target.function_id == TARGET_FUNCTION
                ]
                assert len(grants) == 2
                assert {grant.caller.principal_id for grant in grants} == expected_callers
                assert {grant.caller.operation_id for grant in grants} == set(CALLER_OPERATIONS)
                assert len({grant.target.principal_id for grant in grants}) == 1
                assert all(
                    grant.scope.dimensions["contract"] == (TARGET_CONTRACT,)
                    and grant.scope.dimensions["operation"] == (TARGET_OPERATION,)
                    for grant in grants
                )
                grant_ids = {grant.grant_id for grant in grants}
                if prior_grant_ids is not None:
                    assert grant_ids == prior_grant_ids
                prior_grant_ids = grant_ids


def test_production_panel_cannot_borrow_a_selected_provider_caller(
    activated_callers: tuple[Any, Path],
) -> None:
    """An external session name is not an authenticated nested principal binding."""
    active, user_data = activated_callers
    with AuthorityStore(user_data / "authority/v4.sqlite3") as authority:
        with _capture(active, authority) as session:
            for operation in CALLER_OPERATIONS:
                selected = session.context_for(
                    CALLER_CONTRACT, operation, "session.panel.exact-caller"
                )
                domain = authority.get_domain(selected.target_domain_id)
                assert domain is not None and len(domain.principals) == 1
                principal = domain.principals[0]
                assert principal.operation_id == operation
                forged_session = "session.host-provider." + principal.principal_id
                with pytest.raises(AuthorityDenied, match="captured Shell caller"):
                    session.context_for(TARGET_CONTRACT, TARGET_OPERATION, forged_session)


@pytest.mark.parametrize("change", ["strip", "sibling", "revision", "duplicate"])
def test_production_capture_rejects_rehashed_caller_selector_claims(
    activated_callers: tuple[Any, Path],
    change: str,
) -> None:
    """Untrusted activation claims cannot replace the immutable accepted plan."""
    active, user_data = activated_callers
    plan = deepcopy(active.resolved.plan)
    binding = next(
        row for row in plan["bindings"] if row.get("caller_operation_id") == CALLER_OPERATIONS[0]
    )
    if change == "strip":
        binding.pop("caller_contract_id")
        binding.pop("caller_operation_id")
    elif change == "sibling":
        binding["caller_operation_id"] = CALLER_OPERATIONS[1]
    elif change == "revision":
        binding["caller_contract_revision_digest"] = "sha256:" + "f" * 64
    else:
        duplicate = deepcopy(binding)
        duplicate["caller_contract_revision_digest"] = next(
            row["function_principal"]["contract_revision_digest"]
            for row in plan["bindings"]
            if row["operation_id"] == CALLER_OPERATIONS[0]
        )
        plan["bindings"].append(duplicate)
    plan["plan_digest"] = canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    lock = deepcopy(active.resolved.lock)
    lock["plan_digest"] = plan["plan_digest"]
    lock["lock_digest"] = canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    activation = deepcopy(active.activation)
    activation.update(plan_digest=plan["plan_digest"], lock_digest=lock["lock_digest"])
    claimed = replace(
        active,
        resolved=replace(active.resolved, plan=plan, lock=lock),
        activation=activation,
    )
    with AuthorityStore(user_data / "authority/v4.sqlite3") as authority:
        assert authority.list_grants() == ()
        with pytest.raises(AuthorityDenied, match="not bound to the captured"):
            _capture(claimed, authority)
        assert authority.list_grants() == ()
