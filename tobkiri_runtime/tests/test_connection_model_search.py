"""Focused connection-bound public catalog search contract checks."""
from pathlib import Path
from types import SimpleNamespace as NS
import importlib.util
import json
import sys

import pytest

BASE = Path(__file__).resolve().parents[1]
RUNTIME = next(
    candidate / 'tobkiri_runtime' for candidate in Path(__file__).resolve().parents
    if (candidate / 'tobkiri_runtime/tobkiri_protocol').is_dir()
)
sys.path[:0] = [str(RUNTIME), str(RUNTIME / 'ecosystem/defaultspack')]


def load(relative):
    path = BASE / relative
    name = 'test_copy_' + path.stem
    if path.stem == 'http_surface_presentation':
        name = 'ecosystem.defaultspack.defaultspack.' + name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


host = load('ecosystem/tobkiri_ui_settings_pack/runtime/model_search.py')
projection = load('ecosystem/defaultspack/domain/ai_client/connection_model_search.py')
providers = {'providers': [{'provider_instance_id': 'openrouter-main',
    'enabled': True, 'adapter_id': 'openai-compatible',
    'catalog_provider_id': 'openrouter'}]}
filters = {'connection_id': 'openrouter-main', 'provider_id': 'openrouter'}
models = [{'provider_id': 'openrouter', 'qualified_model_id': 'openrouter/google/gemini',
    'provider_model_id': 'google/gemini', 'type': 'chat',
    'capabilities': ['chat', 'text_input'], 'metadata': {'secret': 'secret-marker'}},
    {'provider_id': 'google', 'qualified_model_id': 'google/private',
    'provider_model_id': 'private', 'type': 'chat', 'capabilities': ['chat']}]
profiles = [{'model_profile_id': 'saved-mine', 'model_id': 'google/gemini',
    'metadata': {'provider_connection_id': 'openrouter-main'},
    'credential_handle': 'credential:secret-marker'},
    {'model_profile_id': 'private-other', 'model_id': 'private-model',
    'metadata': {'provider_connection_id': 'openrouter-other'}}]


def test_connection_catalog_transport_binding_and_private_profile_isolation(monkeypatch):
    from ecosystem.defaultspack.backend.ai_client import provider_catalog
    monkeypatch.setattr(provider_catalog, '_merge_runtime_inventory',
                        lambda *args: pytest.fail('ambient inventory accessed'))
    resolved = host.connection_search_filters(filters, providers)
    result = projection.search_connection_catalog(resolved, profiles, models)
    assert result['filters_applied']['connection_id'] == 'openrouter-main'
    ids = {item['profile_id'] for item in result['models']}
    assert 'saved-mine' in ids
    assert 'openrouter/google/gemini' in ids
    assert 'private-other' not in ids
    assert 'google/private' not in ids
    for item in result['models']:
        assert item['connection_id'] == 'openrouter-main'
        assert item['provider_id'] == 'openrouter'
        assert item['provenance'] == 'provider_public_catalog'
        assert item['reachability'] == 'unverified'
        assert 'requires_api_key' not in item
        assert 'configured' not in item
        assert item['route_configured'] is (item['profile_id'] == 'saved-mine')
    assert 'secret-marker' not in json.dumps(result)
    assert 'credential_handle' not in json.dumps(result)


@pytest.mark.parametrize('change', [
    {'connection_id': 'unknown'}, {'connection_id': ''},
    {'provider_id': 'google'}, {'provider': 'google'},
])
def test_connection_request_fails_closed(change):
    with pytest.raises(PermissionError):
        host.connection_search_filters({**filters, **change}, providers)


@pytest.mark.parametrize('change', [
    {'enabled': False}, {'adapter_id': 'anthropic'}, {'catalog_provider_id': ''},
])
def test_disabled_or_incompatible_connection_fails_closed(change):
    snapshot = {'providers': [{**providers['providers'][0], **change}]}
    with pytest.raises(PermissionError):
        host.connection_search_filters(filters, snapshot)


def test_bound_search_has_no_shared_connection_cache():
    first = projection.search_connection_catalog(filters, profiles, models)
    second = projection.search_connection_catalog(
        {**filters, 'connection_id': 'openrouter-other'}, profiles, models)
    assert 'saved-mine' not in {item['profile_id'] for item in second['models']}
    assert all(item['connection_id'] == 'openrouter-other' for item in second['models'])
    assert all(item['connection_id'] == 'openrouter-main' for item in first['models'])


def test_schema_and_presentation_admit_only_optional_connection_filter():
    fixture = json.loads((BASE / 'tests/fixtures/legacy_executable_sources.v1.json').read_text())
    def entries(value):
        if isinstance(value, dict):
            if value.get('function_id') == host.FUNCTION_ID:
                yield value
            for child in value.values():
                yield from entries(child)
        elif isinstance(value, list):
            for child in value:
                yield from entries(child)
    schema = next(entries(fixture))['schemas']['input']
    assert schema['properties']['connection_id']['maxLength'] == 256
    assert 'connection_id' not in schema['required']
    presentation = load('ecosystem/defaultspack/defaultspack/http_surface_presentation.py')
    assert 'connection_id' in presentation._MODEL_SEARCH_FILTER_KEYS
    assert 'credential_handle' not in presentation._MODEL_SEARCH_FILTER_KEYS


def test_host_capture_reads_exact_registry_before_search(tmp_path):
    from tobkiri_host.broker import RequestEnvelope
    binding = NS(function=NS(function_id=host.FUNCTION_ID, implementation_digest='digest'),
        operation=NS(contract_id=host.CONTRACT_ID, operation_id=host.OPERATION_ID,
                     contract_version='1.0.0'), principal_ref=NS(value='principal'),
        artifact=NS(digest='artifact'))
    context = NS(profile_id='defaults', model_search_port=None,
        provider_bindings=(binding,), domain_ids={(host.CONTRACT_ID, host.OPERATION_ID,
        'principal'): 'domain'}, plan_digest='plan', security_epoch=1,
        user_data_root=tmp_path)
    seen = []
    class Port:
        def search_models(self, command):
            seen.append(command)
            if "connection_id" not in command.filters:
                return {"models": [], "filters_applied": dict(command.filters)}
            return projection.search_connection_catalog(command.filters,
                list(command.profiles), list(command.catalog_models))
    context.model_search_port = Port()
    envelope = object.__new__(RequestEnvelope)
    for key, value in {'contract_id': host.CONTRACT_ID, 'operation_id': host.OPERATION_ID,
        'target_principal': binding.principal_ref, 'context': NS(profile_id='defaults',
        plan_digest='plan', security_epoch=1)}.items():
        object.__setattr__(envelope, key, value)
    calls = []
    class Client:
        def invoke(self, contract, operation, payload, **kwargs):
            calls.append((contract, operation, payload))
            if contract == host.PROVIDER_REGISTRY_CONTRACT:
                return providers
            if contract == host.MODEL_PROFILE_CONTRACT:
                return {'profiles': profiles}
            return {'models': models}
    invocation = NS(envelope=envelope, assert_current=lambda: None,
        contract_client=lambda **kwargs: Client())
    invoke = host.ModelSearchHostFactoryV4().capture(context).contributions[0].invoke
    result = invoke(host.OPERATION_ID, {'profile_id': 'defaults', **filters}, invocation)
    assert calls[0] == (host.PROVIDER_REGISTRY_CONTRACT,
        host.PROVIDER_REGISTRY_OPERATION, {'profile_id': 'defaults'})
    assert seen[0].filters['provider_id'] == 'openrouter'
    assert result['models']
    calls.clear()
    with pytest.raises(PermissionError):
        invoke(host.OPERATION_ID, {'profile_id': 'defaults', **filters,
            'provider_id': 'google'}, invocation)
    assert len(calls) == 1
    calls.clear()
    old = invoke(host.OPERATION_ID, {'profile_id': 'defaults', 'query': 'old'}, invocation)
    assert old == {'models': [], 'filters_applied': {'query': 'old'}}
    assert all(call[0] != host.PROVIDER_REGISTRY_CONTRACT for call in calls)


def test_http_payload_binds_profile_and_rejects_identity_or_credentials():
    presentation = load('ecosystem/defaultspack/defaultspack/http_surface_presentation.py')
    identity = presentation._MODEL_SEARCH_TARGET
    target = NS(**dict(zip(('contribution_id', 'contract_id', 'operation_id',
                           'provider_id', 'function_id'), identity)))
    session = NS(profile_id='captured-profile', assert_current=lambda: None)
    normalize = presentation.DefaultspackHTTPPresentation().normalize_payload
    assert normalize(target, filters, session=session, workspace_binding_resolver=None) == {
        **filters, 'profile_id': 'captured-profile'}
    for extra in ({'profile_id': 'other'}, {'credential_handle': 'secret'}):
        with pytest.raises(ValueError):
            normalize(target, {**filters, **extra}, session=session,
                      workspace_binding_resolver=None)
    assert normalize(target, {'query': 'old'}, session=session,
        workspace_binding_resolver=None) == {'query': 'old', 'profile_id': 'captured-profile'}


def test_registry_public_snapshot_includes_binding_and_no_credential_material():
    module = load('ecosystem/rumi_provider_registry_pack/runtime/process.py')
    result = module._provider_connection_snapshot({'revision': 1, 'providers': [{
        'provider_instance_id': 'openrouter-main', 'display_name': 'Main',
        'enabled': True, 'adapter_id': 'openai-compatible',
        'credential_handle': 'credential:secret-marker',
        'endpoint': 'https://secret-endpoint.invalid',
        'metadata': {'catalog_provider_id': 'openrouter', 'api_key': 'secret-marker'},
    }]})
    assert result['providers'][0]['adapter_id'] == 'openai-compatible'
    assert result['providers'][0]['catalog_provider_id'] == 'openrouter'
    assert 'secret-marker' not in json.dumps(result)
    assert 'secret-endpoint' not in json.dumps(result)


def test_composition_bound_result_retains_canonical_journal_projection(monkeypatch):
    from tobkiri_protocol.canonical import canonical_json
    name = 'ecosystem.defaultspack.domain.ai_client.connection_model_search'
    monkeypatch.setitem(sys.modules, name, projection)
    composition = load('ecosystem/defaultspack/defaultspack/runtime_composition.py')
    saved = [{**profiles[0], 'max_context': 4096.5}]
    result = composition._model_search(filters, saved, models, {})
    canonical_json(result)
    mine = next(item for item in result['models'] if item['profile_id'] == 'saved-mine')
    assert not isinstance(mine['max_context'], float)
