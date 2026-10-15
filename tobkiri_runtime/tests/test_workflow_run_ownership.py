"""Immutable creator ownership fences all production run mutation paths."""
from types import SimpleNamespace as NS

import pytest

from core_runtime.workflow_v4.engine import WorkflowEngineV4
from core_runtime.workflow_v4.models import RunState, WorkflowDenied
from core_runtime.workflow_v4.provider import WorkflowProviderV4
from core_runtime.workflow_v4.run_ownership import captured_run_owner_scope_digest
from core_runtime.workflow_v4.store import WorkflowStoreV4
from tests.test_workflow_v4 import Authority, Catalog, Invoker, Validator
from tests.test_workflow_v4_cancel_race import _publish_run

OWNER = 'sha256:' + 'a' * 64
FOREIGN = 'sha256:' + 'b' * 64


def _engine(store, owner=OWNER):
    return WorkflowEngineV4(
        store=store, catalog=Catalog(), authority=Authority(), invoker=Invoker(),
        validator=Validator(), clock=lambda: 100.0, owner_scope_digest=owner,
    )


@pytest.fixture
def owned_run(tmp_path):
    store = WorkflowStoreV4(tmp_path / 'workflow.sqlite3', clock=lambda: 100.0)
    engine = _engine(store)
    _publish_run(engine, 'owned-run')
    yield engine
    store.close()


def test_run_creation_atomically_pins_owner_scope(owned_run):
    assert owned_run.store.get_run('owned-run')['owner_scope_digest'] == OWNER
    result = owned_run.execute_step('owned-run', 'echo')
    assert result['state'] == 'succeeded'


@pytest.mark.parametrize('operation', [
    'advance', 'execute', 'cancel', 'pause', 'resume', 'reconcile',
])
def test_foreign_owner_cannot_mutate_any_run_lifecycle(owned_run, operation):
    store = owned_run.store
    foreign = _engine(store, FOREIGN)
    before = store.get_run('owned-run')
    calls = {
        'advance': lambda: foreign.advance_run('owned-run'),
        'execute': lambda: foreign.execute_step('owned-run', 'echo'),
        'cancel': lambda: foreign.cancel_run('owned-run'),
        'pause': lambda: foreign.pause_run('owned-run'),
        'resume': lambda: foreign.resume_run('owned-run'),
        'reconcile': lambda: foreign.reconcile_recovery('owned-run'),
    }
    with pytest.raises(WorkflowDenied, match='another authenticated session'):
        calls[operation]()
    assert store.get_run('owned-run') == before
    assert store.list_attempts('owned-run') == []
    assert foreign._authority.reservations == {}
    assert foreign._invoker.requests == []


def test_owner_is_checked_inside_durable_stop_transaction(owned_run):
    store = owned_run.store
    before = store.get_run('owned-run')
    with pytest.raises(WorkflowDenied):
        store.request_cancel('owned-run', owner_scope_digest=FOREIGN)
    with pytest.raises(WorkflowDenied):
        store.request_cancel('owned-run')
    assert store.get_run('owned-run') == before
    assert store.request_cancel('owned-run', owner_scope_digest=OWNER)['cancel_requested'] is True


def test_attempt_creation_cannot_cross_owner_before_private_reservation(owned_run):
    store = owned_run.store
    request = {'request_id': 'exact-request'}
    for owner in [None, FOREIGN]:
        with pytest.raises(WorkflowDenied):
            store.create_attempt(
                run_id='owned-run', step_id='echo', attempt_number=1,
                request=request, owner_scope_digest=owner,
            )
    assert store.list_attempts('owned-run') == []
    attempt = store.create_attempt(
        run_id='owned-run', step_id='echo', attempt_number=1,
        request=request, owner_scope_digest=OWNER,
    )
    assert attempt['state'] == 'pending'
    foreign = _engine(store, FOREIGN)
    with pytest.raises(WorkflowDenied):
        foreign.cancel_run('owned-run')
    assert not store.get_run('owned-run').get('cancel_requested')
    assert store.get_attempt(attempt['attempt_id'])['state'] == 'pending'


def test_owner_scope_is_immutable_under_transition_updates(owned_run):
    store = owned_run.store
    before = store.get_run('owned-run')
    for value in [None, OWNER, FOREIGN]:
        with pytest.raises(WorkflowDenied, match='immutable'):
            store.transition_run(
                'owned-run', expected={RunState.QUEUED}, target=RunState.RUNNING,
                updates={'owner_scope_digest': value},
            )
    assert store.get_run('owned-run') == before


def test_legacy_unbound_run_cannot_be_adopted_by_captured_engine(tmp_path):
    store = WorkflowStoreV4(tmp_path / 'legacy.sqlite3', clock=lambda: 100.0)
    try:
        legacy = _engine(store, None)
        _publish_run(legacy, 'legacy-run')
        before = store.get_run('legacy-run')
        captured = _engine(store, OWNER)
        with pytest.raises(WorkflowDenied, match='predates verified ownership'):
            captured.cancel_run('legacy-run')
        with pytest.raises(WorkflowDenied, match='predates verified ownership'):
            captured.advance_run('legacy-run')
        assert store.get_run('legacy-run') == before
        # Read-only public inspection remains available, and standalone unowned
        # engines can retain their explicit fixture/offline mode.
        assert WorkflowProviderV4(captured).invoke('run.get', {'run_id': 'legacy-run'})['run'] == before
        assert legacy.cancel_run('legacy-run')['state'] == 'cancelled'
    finally:
        store.close()


def test_parallel_mutators_preserve_one_immutable_owner_across_connections(owned_run):
    store = owned_run.store
    peer = WorkflowStoreV4(store.path, clock=lambda: 100.0)
    try:
        owner_peer = _engine(peer, OWNER)
        foreign = _engine(peer, FOREIGN)
        with pytest.raises(WorkflowDenied):
            foreign.cancel_run('owned-run')
        assert owner_peer.advance_run('owned-run')['run']['state'] == 'succeeded'
        assert not store.get_run('owned-run').get('cancel_requested')
    finally:
        peer.close()


def _invocation(*, owner='owner', session='stable-journal', lease_session='transient-execution'):
    return NS(
        assert_current=lambda: None,
        presentation_owner_principal_id=owner,
        presentation_owner_session_id=session,
        envelope=NS(
            operation_id='run.create', target_principal='create-principal',
            context=NS(
                profile_id='profile', activation_id='activation', activation_digest='activation-digest',
                plan_digest='plan', security_epoch=7, caller_session_id=lease_session,
            ),
        ),
    )


def test_owner_digest_is_stable_across_fresh_operation_and_execution_contexts():
    created = _invocation(lease_session='first-lease-session')
    fresh = _invocation(lease_session='fresh-resume-lease-session')
    fresh.envelope.operation_id = 'run.step.resume'
    fresh.envelope.target_principal = 'resume-principal'
    assert captured_run_owner_scope_digest(created) == captured_run_owner_scope_digest(fresh)
    fresh.envelope.operation_id = 'run.stop'
    fresh.envelope.target_principal = 'stop-principal'
    assert captured_run_owner_scope_digest(created) == captured_run_owner_scope_digest(fresh)
    assert captured_run_owner_scope_digest(created) != captured_run_owner_scope_digest(_invocation(session='different-journal'))


@pytest.mark.parametrize('field', ['profile_id', 'activation_id', 'activation_digest', 'plan_digest', 'security_epoch'])
def test_owner_digest_never_crosses_capture_boundaries(field):
    original, fresh = _invocation(), _invocation()
    setattr(fresh.envelope.context, field, 8 if field == 'security_epoch' else 'different')
    assert captured_run_owner_scope_digest(original) != captured_run_owner_scope_digest(fresh)
