"""Check private selection dispatch lifetime and exact retained completion ordering."""

from types import SimpleNamespace as NS

import pytest

from core_runtime import saved_tool_policy_selection_v4

MODULE = vars(saved_tool_policy_selection_v4)


def test_selection_router_never_accepts_unregistered_wire_selection():
    router = MODULE["RetainedSelectionDispatchV4"]()
    calls = []
    port = NS(
        selection_id="actual-selection", select_prepared=lambda sid, inv: calls.append((sid, inv))
    )
    invocation = NS(assert_current=lambda: None)
    with pytest.raises(PermissionError, match="not live"):
        router.select_prepared("actual-selection", invocation)
    with router.register(port):
        router.select_prepared("actual-selection", invocation)
        with pytest.raises(PermissionError, match="already live"):
            with router.register(port):
                pass
        with pytest.raises(PermissionError, match="not live"):
            router.select_prepared("forged", invocation)
    with pytest.raises(PermissionError, match="not live"):
        router.select_prepared("actual-selection", invocation)
    assert calls == [("actual-selection", invocation)]


def test_native_selection_denial_never_invokes_broker_or_commits_receipt():
    commit = MODULE["commit_native_policy_selection"]
    command = NS(
        context=NS(request_id="selection"),
        expires_at=10,
        presentation_owner_principal_id="owner",
        presentation_owner_session_id="session",
    )
    invocation = NS(assert_current=lambda: None)
    authority = NS(
        get_interactive_approval=lambda q: NS(request_id="selection", expires_at=10, state="denied")
    )
    window = NS(
        open_authority_approval_window=lambda cmd: {"opened": True, "request_id": "selection"}
    )

    def forbidden(*args, **kwargs):
        pytest.fail("denied selection must not reach Broker or receipt")

    with pytest.raises(PermissionError, match="stopped: denied"):
        commit(
            invocation=invocation,
            policy=NS(record_native_approved_selection=forbidden),
            command=command,
            retained_port=NS(complete_after_broker=forbidden),
            dispatch_port=MODULE["RetainedSelectionDispatchV4"](),
            broker=NS(invoke_prepared=forbidden),
            authority=authority,
            window=window,
            assert_current=lambda: None,
            cancellation_proof=None,
            presentation_context=object(),
            clock=lambda: 0,
        )
