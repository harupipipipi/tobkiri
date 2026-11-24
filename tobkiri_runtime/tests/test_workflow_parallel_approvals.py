"""A ready wave can contain more than one independently approved node."""
import copy
from dataclasses import replace

import pytest

from core_runtime.workflow_v4.models import ApprovalState
from tests.test_workflow_v4 import definition

pytest_plugins = ["tests.test_workflow_v4"]


@pytest.mark.parametrize('concurrency', [1, 2])
def test_independent_approval_steps_stay_resumable_until_all_complete(runtime, concurrency):
    provider, _catalog, authority, invoker = runtime
    authority.state = ApprovalState.WAITING_APPROVAL
    document = definition('one')
    document['max_concurrency'] = concurrency
    second = copy.deepcopy(document['steps'][0])
    second['id'] = 'second'
    second['request']['input'] = {'message': 'two'}
    document['steps'].append(second)
    created = provider.invoke('definition.create', {'definition_id': 'approval-wave', 'document': document})
    provider.invoke('definition.publish', {'definition_id': 'approval-wave', 'if_match': created['etag']})
    provider.invoke('run.create', {'definition_id': 'approval-wave', 'run_id': 'wave', 'inputs': {}})
    result = provider.invoke('run.advance', {'run_id': 'wave'})
    assert result['run']['state'] == 'waiting_approval'
    assert not invoker.requests
    attempts = provider._engine.store.list_attempts('wave')
    assert len(attempts) == 2
    assert all(attempt['state'] == 'waiting_approval' for attempt in attempts)
    for index, attempt in enumerate(attempts):
        key = attempt['authority_reservation_id']
        authority.reservations[key] = replace(authority.reservations[key], state=ApprovalState.APPROVED)
        provider.invoke('run.step.resume', {'run_id': 'wave', 'step_id': attempt['step_id']})
        observed = provider.invoke('run.get', {'run_id': 'wave'})
        assert observed['run']['state'] == ('succeeded' if index == 1 else 'waiting_approval')
    assert len(invoker.requests) == 2
