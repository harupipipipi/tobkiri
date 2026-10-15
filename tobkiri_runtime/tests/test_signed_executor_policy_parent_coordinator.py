"""Exercise the shared fixture's actual signed coordinator provider branch."""

from pathlib import Path
from typing import Any

import pytest

from core_runtime.host_contract import bind_host_contract
from core_runtime.interactive_effect_coordinator import InteractiveEffectUnavailable
from tests.fixtures.signed_executor_policy_parent_v4 import (
    provision_signed_executor_policy_parent,
)
from tests.test_interactive_approval_v4 import _CONTRACT
from tobkiri_host.errors import ProviderExecutionError
from tobkiri_host.interactive_effects import PendingEffectError
from tobkiri_host.models import InvocationFrame, OpaqueAuthorityRef


def test_signed_parent_coordinator_status_routes_to_owned_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine Broker dispatch reaches the owned controller, without writes."""
    with bind_host_contract(_CONTRACT):
        provision = provision_signed_executor_policy_parent(tmp_path, monkeypatch)
        calls: list[tuple[str, str, str]] = []
        controller_errors: list[PendingEffectError] = []
        original_status = provision.controller.status_for_presentation

        def observe_status(
            *,
            effect_id: str,
            presentation_owner_principal_id: str,
            presentation_owner_session_id: str,
        ) -> Any:
            calls.append(
                (effect_id, presentation_owner_principal_id, presentation_owner_session_id)
            )
            try:
                return original_status(
                    effect_id=effect_id,
                    presentation_owner_principal_id=presentation_owner_principal_id,
                    presentation_owner_session_id=presentation_owner_session_id,
                )
            except PendingEffectError as exc:
                controller_errors.append(exc)
                raise

        monkeypatch.setattr(provision.controller, "status_for_presentation", observe_status)
        assert provision.h.store.get_host_pending_effect("absent-effect") is None
        context = provision.context_for(
            "tool",
            "coordinator",
            chain=(
                OpaqueAuthorityRef(provision.h.caller.principal_id),
                OpaqueAuthorityRef(provision.principals["executor"].principal_id),
            ),
        )
        with pytest.raises(ProviderExecutionError) as rejected:
            provision.broker.invoke(
                InvocationFrame(
                    contract_id=provision.specs["coordinator"][0],
                    version_range=None,
                    operation_id=provision.specs["coordinator"][1],
                    payload={"phase": "status", "effect_id": "absent-effect"},
                ),
                context,
                effect_scope=provision.scopes["coordinator"].to_dict(),
            )
        assert isinstance(rejected.value.__cause__, InteractiveEffectUnavailable)
        assert calls == [
            (
                "absent-effect",
                provision.root.caller_principal.value,
                provision.root.caller_session_id,
            )
        ]
        assert all(isinstance(value, str) for value in calls[0])
        assert len(controller_errors) == 1
        assert rejected.value.__cause__.__cause__ is controller_errors[0]
        traceback = controller_errors[0].__traceback__
        origin_functions = []
        while traceback is not None:
            origin_functions.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert "_load" in origin_functions
        assert provision.effects == []
        assert not (provision.workspace / "created.txt").exists()
