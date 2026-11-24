"""Native source selection inside a genuinely dispatched executor parent."""

from dataclasses import replace
from types import SimpleNamespace as NS
import time

import pytest
from core_runtime.native_saved_tool_policy_v4 import NativeSavedToolPolicyV4
from core_runtime.saved_tool_policy_selection_v4 import RetainedSelectionDispatchV4
from core_runtime.host_contract import bind_host_contract
from core_runtime.authority.v4_models import LeaseState, AuthorityDenied
from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4
from tobkiri_host.saved_tool_context import NestedToolBinding
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.broker import RequestBroker
from tests.test_interactive_approval_v4 import _CONTRACT, _decision_command
from tests.test_authority_v4_lifecycle import _digest
from tests.test_tobkiri_host_authority_v4_adapter import _Admission, _NoAdapters
from tobkiri_host.contracts import AdapterPlanner
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.effects import InMemoryReconciliationStore
from ecosystem.rumi_host_authority_bridge_pack.runtime.native_policy_bridge import (
    HOST_PROVIDER_FACTORY,
)


from tests.fixtures.signed_executor_policy_parent_v4 import (
    provision_signed_executor_policy_parent,
)


def test_native_source_selection_and_restore_bind_actual_executor_parent(tmp_path, monkeypatch):
    """Selection caller is executor, saved root owns presentation, stale parent denies."""
    with bind_host_contract(_CONTRACT):
        p = provision_signed_executor_policy_parent(tmp_path, monkeypatch)
        selected, h, inv = p.selected, p.h, p.invocation
        port = RetainedSelectionDispatchV4()
        pinned = selected.catalog.resolve_pinned(
            selected.frame.contract_id, selected.frame.operation_id
        )
        capture = NS(
            profile_id=p.root.profile_id,
            plan_digest=p.root.plan_digest,
            security_epoch=p.root.security_epoch,
            activation={"activation_id": "activation-1"},
            provider_bindings=(pinned,),
            domain_ids={
                (
                    selected.frame.contract_id,
                    selected.frame.operation_id,
                    h.target.principal_id,
                ): h.target_domain.domain_id
            },
            action_approval_policy_port=port,
        )
        captured = HOST_PROVIDER_FACTORY.capture(capture)

        def native_invocation(envelope):
            inv.assert_current()
            return NS(
                envelope=envelope,
                assert_current=inv.assert_current,
                presentation_owner_principal_id=inv.presentation_owner_principal_id,
                presentation_owner_session_id=inv.presentation_owner_session_id,
            )

        backend = ExactHostProviderBackendV4(
            captured.contributions,
            backend_id="tobkiri.python-host-v4",
            profile_id=p.root.profile_id,
            plan_digest=p.root.plan_digest,
            security_epoch=p.root.security_epoch,
            invocation_context=native_invocation,
        )
        broker = RequestBroker(
            catalog=selected.catalog,
            adapters=AdapterPlanner(()),
            adapter_executor=_NoAdapters(),
            backends=BackendRegistry((backend,)),
            materialization=MaterializationCoordinator(),
            admission=_Admission(),
            authority=p.authority,
            audit=p.authority,
            reconciliation=InMemoryReconciliationStore(),
        )
        native_context = replace(
            selected.context,
            request_id="executor-native-selection",
            caller_principal=OpaqueAuthorityRef(p.principals["executor"].principal_id),
            caller_session_id="session-executor",
            caller_domain_id=p.domains["executor"].domain_id,
            caller_boot_epoch=p.domains["executor"].boot_epoch,
            target_backend_digest=backend.status.backend_digest,
        )
        released = []

        def bind_nested(current, contract, operation, payload):
            current.assert_current()
            parent, state = h.store.inspect_lease_token(p.auth.lease_token)
            assert state is LeaseState.DISPATCHED
            assert parent.target.principal_id == native_context.caller_principal.value
            return NestedToolBinding(
                native_context,
                selected.ceiling.to_dict(),
                p.artifacts["executor"].publisher_lineage,
                None,
                lambda: released.append(True),
            )

        saved = NS(
            root=p.root,
            mode="full",
            conversation_id="conversation-1",
            turn_id="turn-1",
            workspace_id="workspace",
            workspace_binding=NS(canonical_root=tmp_path.resolve()),
            capture_digest="sha256:saved-executor-capture",
            assert_current=inv.assert_current,
            saved_scope=NS(envelope=NS(deadline_monotonic=time.monotonic() + 120)),
        )

        def approve(command):
            inv.assert_current()
            request = h.store.get_interactive_approval_request(command.request_id)
            p.authority.approve_interactive_approval(
                replace(
                    _decision_command(
                        h,
                        command.request_id,
                        nonce="executor-native-operator",
                        phrase=request.redacted_metadata["confirmation_phrase"],
                    ),
                    context=replace(p.root, request_id=command.request_id),
                )
            )
            return {"opened": True, "request_id": command.request_id}

        def saved_guard(current, root, native):
            current.assert_current()
            assert current is inv
            assert root is p.root
            assert native["owner_principal"] == p.root.caller_principal.value
            assert (
                native["context"]["caller_principal"]["value"]
                == native_context.caller_principal.value
            )

        native = NativeSavedToolPolicyV4(
            broker=broker,
            authority=p.authority,
            store=h.store,
            kernel=h.kernel,
            catalog=selected.catalog,
            window=NS(open_authority_approval_window=approve),
            selection_dispatch=port,
            bind_nested=bind_nested,
            finite_routes=lambda *_: selected.policy.delegation_routes,
            reviewer_capture=lambda *_: None,
            reviewer_adapter=lambda *_: (None, None),
            prepared_validator=lambda _: None,
            current_guard=lambda *_: inv.assert_current(),
            saved_root_guard=saved_guard,
            clock=h.clock,
        )
        root = native(inv, saved)
        receipt = root.receipt_record()[1]
        committed = h.store.get_lease(receipt["committed_selection_lease"])[0]
        assert committed.caller == p.principals["executor"]
        assert root.capture()["context"]["caller_session_id"] == "session-executor"
        assert h.store.grant_usage(committed.grant_id) == (0, 1)
        assert native.restore(inv, saved).selection_id == root.selection_id
        h.kernel.finish(
            p.parent.lease_id, state=LeaseState.COMMITTED, outcome_digest=_digest("done")
        )
        with pytest.raises(AuthorityDenied):
            native.restore(inv, saved)
        assert released == [True]
