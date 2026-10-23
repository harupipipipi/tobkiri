"""Captured Workflow Stop regressions at the real encrypted journal boundary.

Suggested insertion: new tests/test_workflow_captured_cancellation_journal.py.
The invocation is a Host-context fixture, but the service guard, owner checks,
route checks, adapter, encrypted private journal, public store, and Stop engine
all run unchanged. No _guard/_owned/revoke/cancel methods are monkeypatched.
"""
from contextlib import nullcontext
from types import SimpleNamespace as NS
import threading

import pytest

from core_runtime.workflow_v4.attempt_adapter import CapturedWorkflowAttemptAdapterV4
from core_runtime.workflow_v4.attempt_port import (
    CapturedWorkflowAttemptRouteV4, WorkflowAttemptServiceConfigV4,
)
from core_runtime.workflow_v4.attempt_service import HostWorkflowAttemptServiceV4
from core_runtime.workflow_v4.attempt_store import attempt_identity
from core_runtime.workflow_v4.engine import WorkflowEngineV4
from core_runtime.workflow_v4.models import (
    RunState, StepAttemptState, WorkflowCancellationUnconfirmed, WorkflowDenied, digest,
)
from core_runtime.workflow_v4.store import WorkflowStoreV4
from tobkiri_host.models import OpaqueAuthorityRef
from tests.test_workflow_v4 import Catalog, Validator
from tests.test_workflow_v4_cancel_race import _publish_run


@pytest.fixture
def captured_stop(tmp_path):
    coordinator, stop = OpaqueAuthorityRef('workflow-execute'), OpaqueAuthorityRef('workflow-stop')
    route = CapturedWorkflowAttemptRouteV4(
        caller_principal=coordinator,
        binding=NS(
            operation=NS(
                contract_id='example.echo.v1', revision_digest='sha256:' + '1' * 64,
                operation_id='echo', input_schema={}, effect_class=NS(value='capability:echo'),
            ),
            principal_ref=OpaqueAuthorityRef('example.echo.provider'),
            function=NS(function_id='example.echo'),
        ),
        caller_effect_ceiling=NS(), authority_mode='profile_grant',
    )
    service = HostWorkflowAttemptServiceV4(WorkflowAttemptServiceConfigV4(
        broker=NS(), authority=NS(), approvals=NS(), routes=(route,),
        context_for_attempt=lambda *args: None,
        execution_scope=lambda *args: nullcontext(),
        presentation_owner_scope=lambda *args: nullcontext(),
        assert_current_capture=lambda: None,
        state_path=tmp_path / 'private-attempts.sqlite3',
        coordinator_principal=coordinator,
        coordinator_publisher_lineage='test-publisher',
        profile_id='profile', activation_id='activation', activation_digest='activation-digest',
        plan_digest='plan', security_epoch=7, stop_principals=(stop,),
        clock=lambda: 100.0, monotonic_clock=lambda: 100.0,
    ))
    cancellation_calls = []

    def invocation(owner='owner', principal=stop):
        def active(reference):
            cancellation_calls.append(('active', reference))
            return False

        return NS(
            assert_current=lambda: None,
            presentation_owner_principal_id=owner,
            presentation_owner_session_id='owner-session',
            envelope=NS(
                target_principal=principal, contract_id='tobkiri.workflow.stop.v4', operation_id='run.stop',
                cancellation_requested=threading.Event(), deadline_monotonic=1000.0,
                context=NS(
                    profile_id='profile', activation_id='activation', activation_digest='activation-digest',
                    plan_digest='plan', security_epoch=7,
                ),
            ),
            cancellation=NS(active_for=active),
        )

    request = {
        'request_id': 'sealed-request', 'contract_id': 'example.echo.v1',
        'contract_revision_digest': 'sha256:' + '1' * 64, 'operation_id': 'echo',
        'function_principal_id': 'example.echo.provider', 'provider_id': 'example.echo',
        'input_schema_digest': digest({}), 'effect_ceiling': ['capability:echo'],
        'input': {'message': 'hi'}, 'timeout_ms': 1000, 'idempotency_key': 'unique-key',
    }
    public = WorkflowStoreV4(tmp_path / 'workflow.sqlite3', clock=lambda: 100.0)
    owner = invocation()
    adapter = CapturedWorkflowAttemptAdapterV4(store=public, port=service, invocation=owner)
    engine = WorkflowEngineV4(
        store=public, catalog=Catalog(), authority=adapter, invoker=adapter,
        validator=Validator(), clock=lambda: 100.0,
    )
    _publish_run(engine, 'captured-stop')
    public.transition_run('captured-stop', expected={RunState.QUEUED}, target=RunState.RUNNING)
    attempt = public.create_attempt(
        run_id='captured-stop', step_id='echo', attempt_number=1, request=request,
    )
    result = NS(
        service=service, invocation=invocation, owner=owner, adapter=adapter,
        engine=engine, public=public, attempt=attempt, request=request,
        reservation_id=attempt_identity('profile', request['request_id']),
        cancellation_calls=cancellation_calls,
    )
    yield result
    public.close()


def _insert_private(case, *, state='dispatched'):
    record = {
        'reservation_id': case.reservation_id, 'owner': case.service._owner(case.owner),
        'request': case.request, 'state': state, 'effect_id': None,
        'idempotency_identity': 'exact-test-identity',
    }
    assert case.service._store.insert(case.reservation_id, record)
    return record


def _admit_public(case):
    case.public.transition_attempt(
        case.attempt['attempt_id'], expected={StepAttemptState.PENDING},
        target=StepAttemptState.DISPATCHING,
    )
    case.public.transition_attempt(
        case.attempt['attempt_id'], expected={StepAttemptState.DISPATCHING},
        target=StepAttemptState.RUNNING,
    )
    case.public.admit_dispatch(case.attempt['attempt_id'], case.request['request_id'])


def test_missing_private_reservation_is_unconfirmed_through_captured_adapter(captured_stop):
    case = captured_stop
    with pytest.raises(WorkflowCancellationUnconfirmed):
        case.adapter.cancel(case.request['request_id'])
    assert case.service._store.list_attempts() == []


def test_captured_stop_closes_pending_public_attempt_before_private_reserve(captured_stop):
    case = captured_stop
    result = case.engine.cancel_run('captured-stop')
    assert result['state'] == RunState.CANCELLED.value
    assert case.public.get_attempt(case.attempt['attempt_id'])['state'] == StepAttemptState.CANCELLED.value
    assert case.service._store.list_attempts() == []


def test_captured_stop_cannot_launder_foreign_owner_denial_as_absence(captured_stop):
    case = captured_stop
    original = _insert_private(case)
    foreign = CapturedWorkflowAttemptAdapterV4(
        store=case.public, port=case.service, invocation=case.invocation('foreign-owner'),
    )
    with pytest.raises(WorkflowDenied) as error:
        foreign.cancel(case.request['request_id'])
    assert not isinstance(error.value, WorkflowCancellationUnconfirmed)
    assert case.service._store.get(case.reservation_id) == (1, original)
    assert case.cancellation_calls == []


@pytest.mark.parametrize('private_row', [False, True])
def test_captured_stop_keeps_admitted_dispatch_unknown_without_live_drain(captured_stop, private_row):
    case = captured_stop
    _admit_public(case)
    if private_row:
        _insert_private(case)
    with pytest.raises(WorkflowCancellationUnconfirmed):
        case.engine.cancel_run('captured-stop')
    current = case.public.get_attempt(case.attempt['attempt_id'])
    assert current['state'] == StepAttemptState.RUNNING.value
    assert current['dispatch_admission'] == 'admitted'
    assert case.public.get_run('captured-stop')['state'] == RunState.RUNNING.value
    if private_row:
        assert case.service._store.get(case.reservation_id)[1]['state'] == 'ambiguous'
    else:
        assert case.service._store.list_attempts() == []


def test_missing_private_row_still_requires_exact_stop_principal(captured_stop):
    case = captured_stop
    unauthorized = CapturedWorkflowAttemptAdapterV4(
        store=case.public, port=case.service,
        invocation=case.invocation(principal=OpaqueAuthorityRef('foreign-stop')),
    )
    with pytest.raises(WorkflowDenied) as error:
        unauthorized.cancel(case.request['request_id'])
    assert not isinstance(error.value, WorkflowCancellationUnconfirmed)
    assert case.service._store.list_attempts() == []
    assert case.cancellation_calls == []
