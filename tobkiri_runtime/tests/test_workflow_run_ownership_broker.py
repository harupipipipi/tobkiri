"""Rejected cross-owner Stop must leave the real Broker-backed run untouched."""
from copy import deepcopy
from pathlib import Path
import pytest
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile, prepare_default_profile_confirmation,
    capture_profile, prepare_profile_confirmation,
)
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.test_workflow_v4_pack_control_integration import _capture_defaultspack_dispatch

VALIDATE = 'tobkiri.service.tool.arguments.validate.v1'
CONTRACT = 'tobkiri.workflow.v4'
RUNTIME = Path(__import__('core_runtime').__path__[0]).parent


def test_actual_broker_foreign_stop_cannot_mutate_run_before_owner_rejection(tmp_path, monkeypatch):
    user_data = tmp_path / 'user-data'
    monkeypatch.setenv('TOBKIRI_USER_DATA', str(user_data))
    monkeypatch.setenv('RUMI_USER_DATA', str(user_data))
    capture_default_profile(confirmation=prepare_default_profile_confirmation())
    definitions = ProfileDefinitionStore(user_data)
    fixture = deepcopy(dict(definitions.get_profile('defaults').profile))
    changed = 0
    for edge in fixture['requested_edges']:
        if edge['caller_function_id'] == 'tobkiri.workflow.provider' and edge['contract_id'] == VALIDATE:
            edge['authority_mode'] = 'interactive_only'
            changed += 1
    assert changed == 1  # Narrow existing authority; never add a route or grant.
    definitions.create_profile(fixture, profile_id='wfaudit', display_name='Workflow Stop owner audit')
    active = capture_profile('wfaudit', confirmation=prepare_profile_confirmation('wfaudit'))
    authority = AuthorityStore(user_data / 'authority/v4.sqlite3')
    session = _capture_defaultspack_dispatch(
        active, bundle_root=packaged_profile_bundle_root(), ecosystem_root=RUNTIME / 'ecosystem',
        authority_store=authority,
    )
    try:
        def call(operation, payload, *, owner='owner-a', contract=CONTRACT):
            return session.invoke(contract, operation, {**payload, '_session_id': owner})

        target = next(item for item in call('operation.palette', {})['operations'] if item['contract_id'] == VALIDATE)
        document = {
            'workflow_api_version': 'io.tobkiri.workflow.v4', 'name': 'Owner isolation proof',
            'max_concurrency': 1, 'steps': [{
                'id': 'validate', 'depends_on': [],
                'request': {
                    **{key: target[key] for key in ('contract_id', 'contract_revision_digest', 'operation_id', 'function_principal_id')},
                    'input': {'schema': {'type': 'object'}, 'arguments': {'message': 'hi'}},
                }, 'retry': {'max_attempts': 1, 'backoff_ms': 0},
            }],
        }
        created = call('definition.create', {'definition_id': 'owner-proof', 'document': document})
        call('definition.publish', {'definition_id': 'owner-proof', 'if_match': created['etag']})
        call('run.create', {'definition_id': 'owner-proof', 'run_id': 'owner-proof', 'inputs': {}})
        call('run.advance', {'run_id': 'owner-proof'})
        before = call('run.get', {'run_id': 'owner-proof'})
        assert before['run']['state'] == 'waiting_approval', before
        assert not before['run'].get('cancel_requested')
        with pytest.raises(Exception) as error:
            call('run.stop', {'run_id': 'owner-proof'}, owner='owner-b', contract='tobkiri.workflow.stop.v4')
        after = call('run.get', {'run_id': 'owner-proof'})
        assert type(error.value).__name__ == "ProviderExecutionError"
        assert after == before
        stopped = call('run.stop', {'run_id': 'owner-proof'}, contract='tobkiri.workflow.stop.v4')
        assert stopped['state'] == 'cancelled'
        final = call('run.get', {'run_id': 'owner-proof'})
        assert final['attempts'][0]['state'] == 'cancelled'
    finally:
        session.close()
        authority.close()
