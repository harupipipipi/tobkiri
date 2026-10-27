"""Observe stable Host owner identity through actual authenticated HTTP descriptors."""
from pathlib import Path
from copy import deepcopy
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


def test_actual_http_run_create_advance_stop_share_one_presentation_owner(tmp_path, monkeypatch):
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
        stopped = call('run.stop', {'run_id': 'http-owner'}, contract='tobkiri.workflow.stop.v4')
        assert stopped['state'] == 'cancelled', stopped
        assert [row[0] for row in observed] == ['run.create', 'run.advance', 'run.stop']
        assert len({(row[1], row[2]) for row in observed}) == 1, observed
        assert len({row[3] for row in observed}) == 3, observed
        print('HTTP_OWNER_PROOF:', [row[0] for row in observed], 'one stable owner, three exact operation principals')
    finally:
        server.stop()
        session.close()
        authority.close()
