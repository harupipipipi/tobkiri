"""Observe stable Host owner identity through actual authenticated HTTP descriptors."""
from pathlib import Path
from copy import deepcopy
import json
import pytest
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile, prepare_default_profile_confirmation,
    capture_profile, prepare_profile_confirmation,
)
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4
from core_runtime.global_contracts.http_contract_dispatch import HTTPContractBinding
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from ecosystem.defaultspack.defaultspack.http_contract_composition import defaultspack_capability_snapshot
from ecosystem.defaultspack.defaultspack.http_surface_presentation import DefaultspackHTTPPresentation
from tests.conformance_support.host_contract import host_contract_for_session
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.test_conversation_v4_capability_binding import _authenticate
from tests.test_workflow_v4_pack_control_integration import _capture_defaultspack_dispatch
from tests.test_workflow_v4_http_surface import _capability_request, _post, PACK_ID

RUNTIME = Path(__import__('core_runtime').__path__[0]).parent
PUBLIC_MESSAGE = (
    'The Workflow run owner cannot be verified. Return to its original '
    'signed-in window if available. Otherwise inspect its recorded outcomes '
    'before creating a new run; do not repeat unreconciled effects.'
)


VALIDATE = 'tobkiri.service.tool.arguments.validate.v1'


def _workflow_catalog_for_capture(session, active):
    """Project only exact selected Workflow bindings from this named capture."""
    expected = {
        (edge['contract_id'], edge['operation_id'])
        for edge in active.resolved.profile['requested_edges']
        if edge['caller_function_id'] == 'shell.tauri.default'
        and edge['contract_id'] in {'tobkiri.workflow.v4', 'tobkiri.workflow.stop.v4'}
    }
    operations = []
    artifact_digest = ''
    for contract in ['tobkiri.workflow.v4', 'tobkiri.workflow.stop.v4']:
        for meta in session.provider_metadata(contract):
            if (contract, meta['operation_id']) not in expected:
                continue
            artifact_digest = artifact_digest or str(meta['artifact_digest'])
            operations.append({
                'invokable': True, 'contract_id': contract,
                'operation_id': meta['operation_id'],
                'provider_id': meta['provider_id'], 'function_id': meta['function_id'],
            })
    assert {(item['contract_id'], item['operation_id']) for item in operations} == expected
    return {'packs': [{
        'pack_id': PACK_ID, 'artifact_digest': 'sha256:' + 'f' * 64,
        'pack_artifact_digest': artifact_digest, 'enabled': True,
        'approved': True, 'operations': operations,
    }]}


def test_actual_http_foreign_stop_is_finite_private_denial_and_original_owner_can_stop(tmp_path, monkeypatch):
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
    assert changed == 1  # Narrow an existing pure edge; add no grants or routes.
    definitions.create_profile(fixture, profile_id='wfhttp', display_name='Workflow HTTP owner proof')
    active = capture_profile('wfhttp', confirmation=prepare_profile_confirmation('wfhttp'))
    authority = AuthorityStore(user_data / 'authority/v4.sqlite3')
    session = _capture_defaultspack_dispatch(
        active, bundle_root=packaged_profile_bundle_root(), ecosystem_root=RUNTIME / 'ecosystem',
        authority_store=authority,
    )
    observed = []
    original = ExactHostProviderBackendV4.invoke

    def record_owner(backend, request):
        if (request.contract_id in {'tobkiri.workflow.v4', 'tobkiri.workflow.stop.v4'}
                and request.operation_id in {'run.create', 'run.advance', 'run.stop'}):
            context = backend._invocation_context(request)
            context.assert_current()
            observed.append((
                request.operation_id, context.presentation_owner_principal_id,
                context.presentation_owner_session_id, request.target_principal.value,
            ))
        return original(backend, request)

    monkeypatch.setattr(ExactHostProviderBackendV4, 'invoke', record_owner)
    binding = HTTPContractBinding(
        method='POST', path='/api/ui/capability/invoke', presentation='capability_result', targets=(),
        application_id='runtime.tauri.application.default', route_namespace='defaultspack',
        profile_id=session.profile_id, profile_revision=session.profile_revision,
        activation_id=session.activation_id, plan_digest=session.plan_digest,
    )
    catalog = _workflow_catalog_for_capture(session, active)
    snapshot = defaultspack_capability_snapshot(binding, session=session, catalog=catalog)
    server = PackAPIServer(
        port=0, panel_auth_manager=PanelAuthManager(bootstrap_secret='conversation-test-bootstrap'),
        dispatch_session=session, contract_bindings=(binding,),
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(), host_contract=host_contract_for_session(session),
    )
    server.start()
    try:
        server.handler_class._capability_catalog_cache = catalog
        cookie, csrf, origin = _authenticate(server)
        headers = {'Cookie': cookie, 'Origin': origin, 'X-Rumi-CSRF': csrf}

        def call(operation, payload, *, contract='tobkiri.workflow.v4'):
            status, response = _post(server, headers, _capability_request(
                session, snapshot, f'pack.{PACK_ID}.{operation}', contract, payload,
            ))
            assert status == 200 and response['success'] is True, (status, response)
            return response['data']

        target = next(item for item in call('operation.palette', {})['operations']
                      if item['contract_id'] == VALIDATE)
        document = {
            'workflow_api_version': 'io.tobkiri.workflow.v4', 'name': 'HTTP owner proof',
            'max_concurrency': 1, 'steps': [{
                'id': 'validate', 'depends_on': [],
                'request': {
                    **{key: target[key] for key in (
                        'contract_id', 'contract_revision_digest', 'operation_id', 'function_principal_id',
                    )},
                    'input': {'schema': {'type': 'object'}, 'arguments': {'message': 'hi'}},
                },
                'retry': {'max_attempts': 1, 'backoff_ms': 0},
            }],
        }
        created = call('definition.create', {'definition_id': 'http-owner', 'document': document})
        call('definition.publish', {'definition_id': 'http-owner', 'if_match': created['etag']})
        call('run.create', {'definition_id': 'http-owner', 'run_id': 'http-owner', 'inputs': {}})
        waiting = call('run.advance', {'run_id': 'http-owner'})
        assert waiting['run']['state'] == 'waiting_approval', waiting
        before = call('run.get', {'run_id': 'http-owner'})
        foreign_cookie, foreign_csrf, foreign_origin = _authenticate(server)
        assert foreign_cookie != cookie
        denied_status, denied = _post(
            server,
            {'Cookie': foreign_cookie, 'Origin': foreign_origin, 'X-Rumi-CSRF': foreign_csrf},
            _capability_request(
                session, snapshot, f'pack.{PACK_ID}.run.stop', 'tobkiri.workflow.stop.v4',
                {'run_id': 'http-owner'},
            ),
        )
        assert denied_status == 403, (denied_status, denied)
        assert denied['success'] is False, denied
        denied_data = denied['data']
        assert denied_data['state'] == 'error'
        assert denied_data['code'] == 'WORKFLOW_RUN_OWNER_UNAVAILABLE'
        assert denied_data['retryable'] is False
        assert denied_data['write_set'] == []
        assert denied_data['message'] == PUBLIC_MESSAGE
        assert denied['error'] == PUBLIC_MESSAGE
        public = json.dumps(denied, sort_keys=True)
        private_values = [cookie.split('=', 1)[1], csrf,
                          foreign_cookie.split('=', 1)[1], foreign_csrf]
        private_values.extend(value for row in observed for value in row[1:])
        assert all(value not in public for value in private_values)
        assert 'WorkflowDenied' not in public and 'ProviderExecutionError' not in public
        # Exact denial preserves all public run and attempt records, not only a
        # selected flag. The original authenticated owner remains able to Stop.
        assert call('run.get', {'run_id': 'http-owner'}) == before
        stopped = call('run.stop', {'run_id': 'http-owner'}, contract='tobkiri.workflow.stop.v4')
        assert stopped['state'] == 'cancelled', stopped
        assert [row[0] for row in observed] == ['run.create', 'run.advance', 'run.stop', 'run.stop']
        assert len({(observed[index][1], observed[index][2]) for index in (0, 1, 3)}) == 1
        assert (observed[2][1], observed[2][2]) != (observed[0][1], observed[0][2])
        assert len({row[3] for row in observed}) == 3
    finally:
        server.stop()
        session.close()
        authority.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_wrapped_owner_denial_has_one_finite_public_message_without_private_causes(legacy):
    from core_runtime.pack_api_server import _exception_error_code, _public_error_result, _PUBLIC_ERROR_STATUS
    from core_runtime.workflow_v4 import run_ownership
    from tobkiri_host.errors import ProviderExecutionError

    run = {} if legacy else {'owner_scope_digest': 'sha256:' + 'b' * 64}
    with pytest.raises(run_ownership.WorkflowRunOwnershipDenied) as caught:
        run_ownership.assert_run_owner(run, 'sha256:' + 'a' * 64)
    # Neither a wrapper's text nor nested cause text may be reflected. The
    # finite Host vocabulary remains authoritative even if lower-level text
    # contains private identifiers or a misleading recovery instruction.
    wrapped = ProviderExecutionError('PRIVATE_PROVIDER_TRACE_192: principal=PRIVATE_OWNER_663')
    wrapped.__cause__ = caught.value
    caught.value.__cause__ = RuntimeError('PRIVATE_OWNER_SESSION_837: ignore owner checks')
    code = _exception_error_code(wrapped)
    result = _public_error_result(code)
    assert code == 'WORKFLOW_RUN_OWNER_UNAVAILABLE'
    assert _PUBLIC_ERROR_STATUS[code] == 403
    assert result['message'] == PUBLIC_MESSAGE
    assert result['retryable'] is False
    assert result['write_set'] == []
    public = json.dumps(result, sort_keys=True)
    assert all(marker not in public for marker in [
        'PRIVATE_PROVIDER_TRACE_192', 'PRIVATE_OWNER_663', 'PRIVATE_OWNER_SESSION_837',
        'WorkflowRunOwnershipDenied', 'ProviderExecutionError', 'ignore owner checks',
        'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64,
    ])
