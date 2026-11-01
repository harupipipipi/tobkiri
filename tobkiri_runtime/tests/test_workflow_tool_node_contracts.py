"""Typed tool envelopes and finite Flow edges, not native desktop acceptance."""
from pathlib import Path
import json

import pytest
from jsonschema import Draft202012Validator

from ecosystem.rumi_tool_validation_pack.runtime.validator import _host_bind as validation_bind
from ecosystem.rumi_tool_result_pack.runtime.normalizer import _host_bind as result_bind
from tobkiri_protocol.canonical import canonical_digest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[1]


def operation(pack_id):
    source = json.loads((ROOT / 'schemas/pack_v4_catalog.v1.json').read_text())
    pack = next(p for p in source['packs'] if p['pack_id'] == pack_id)
    contract = pack['provided_contracts'][0]
    return contract, contract['operations'][0]


@pytest.mark.parametrize('pack_id', [
    'rumi_tool_broker_pack', 'rumi_tool_validation_pack', 'rumi_tool_result_pack',
])
def test_generated_operation_pins_actual_typed_envelope(pack_id):
    contract, op = operation(pack_id)
    for schema in op['schemas'].values():
        Draft202012Validator.check_schema(schema)
    generated = json.loads((ROOT / 'ecosystem' / pack_id / 'contracts.v4.json').read_text())
    actual = next(c for c in generated['contracts'] if c['contract_id'] == contract['contract_id'])
    actual_op = next(o for o in actual['operations'] if o['operation_id'] == op['id'])
    assert actual_op['input_schema_digest'] == canonical_digest(op['schemas']['input'])
    assert actual_op['output_schema_digest'] == canonical_digest(op['schemas']['output'])


def test_broker_input_is_a_registered_tool_call_not_arbitrary_executor_authority():
    schema = operation('rumi_tool_broker_pack')[1]['schemas']['input']
    validator = Draft202012Validator(schema)
    payload = {'tool_id': 'calculator', 'tool_call_id': 'call.1', 'arguments': {'expression': '2+3'}}
    assert validator.is_valid(payload)
    assert validator.is_valid({**payload, 'expected_definition_hash': 'a' * 64})
    for key in ('approved', 'definition', 'executor', 'credential_handle', 'api_key'):
        assert not validator.is_valid({**payload, key: True})
    assert not validator.is_valid({**payload, 'expected_definition_hash': 'stale'})
    assert not validator.is_valid({**payload, 'arguments': [1, 2]})
    assert not validator.is_valid({**payload, 'tool_id': 'calculator\n'})
    assert not validator.is_valid({**payload, 'expected_definition_hash': 'a' * 64 + '\n'})


@pytest.mark.parametrize('value', [None, 'text', 7, True, [1, 'two'], {'nested': [False]}])
def test_validation_and_normalization_host_implementations_match_json_ports(value):
    validation = validation_bind(None)
    normalized = result_bind(None)
    validation_payload = {'schema': {}, 'arguments': value}
    checked = validation(validation_payload, None)
    result_payload = {'tool_id': 'test', 'tool_call_id': 'call.1',
                      'executor_provider_instance_id': 'fixture.provider',
                      'executor_content_hash': 'fixture.hash', 'value': value}
    result = normalized(result_payload, None)
    for pack, payload, output in [
        ('rumi_tool_validation_pack', validation_payload, checked),
        ('rumi_tool_result_pack', result_payload, result),
    ]:
        schemas = operation(pack)[1]['schemas']
        Draft202012Validator(schemas['input']).validate(payload)
        Draft202012Validator(schemas['output']).validate(output)
        assert canonical_digest(output).startswith('sha256:')
    assert checked['arguments'] == value
    assert result['result'] == value


def test_profile_adds_only_explicit_tool_composition_edges():
    intent = json.loads((ROOT / 'ecosystem/defaultspack/v4/defaults.profile.intent.v1.json').read_text())
    edges = [e for e in intent['requested_edges'] if e['caller_function_id'] == 'tobkiri.workflow.provider']
    expected = {
        'rumi_ai_gateway_pack.ai-gateway.text-generate',
        'rumi_ai_gateway_pack.ai-gateway.messages-generate',
        'rumi_ai_modality_pack.ai-transcribe', 'rumi_ai_modality_pack.ai-speech',
        'rumi_tool_broker_pack.tool-invoke',
        'rumi_tool_validation_pack.tool-arguments-validate',
        'rumi_tool_result_pack.tool-result-normalize',
        'messages_build',
    }
    assert {e['operation_id'] for e in edges} == expected
    for edge in edges:
        scope = edge['requested_scope_template']
        assert scope['dimensions'] == {'contract': [edge['contract_id']], 'operation': [edge['operation_id']]}
        assert scope['opaque'] is False
        assert 'local-executor' not in edge['target_provider_id']


def test_two_non_voice_nodes_transfer_structured_values_through_real_workflow_engine(tmp_path):
    """Real owners and engine; authority/catalog doubles do not prove desktop grants."""
    from core_runtime.workflow_v4 import WorkflowEngineV4, WorkflowProviderV4, WorkflowStoreV4
    from core_runtime.workflow_v4.integration import _SchemaValidator
    from core_runtime.workflow_v4.models import InvocationOutcome
    from tests.test_workflow_v4_step_output_binding import Authority, Catalog, _step, publish

    catalog = Catalog()
    catalog.value['operations'] = []
    catalog.value['schemas'] = {}
    catalog.value['operation_output_schemas'] = []
    handlers = {}
    operations = []
    for pack, handler in [('rumi_tool_validation_pack', validation_bind(None)),
                          ('rumi_tool_result_pack', result_bind(None))]:
        contract, op = operation(pack)
        row = {'contract_id': contract['contract_id'],
               'contract_revision_digest': canonical_digest(contract),
               'operation_id': op['id'], 'function_principal_id': contract['provider_id'],
               'provider_id': contract['provider_id'],
               'input_schema_digest': canonical_digest(op['schemas']['input']),
               'effect_ceiling': ['operation.invoke']}
        operations.append(row)
        catalog.value['operations'].append(row)
        for schema in op['schemas'].values():
            catalog.value['schemas'][canonical_digest(schema)] = schema
        catalog.value['operation_output_schemas'].append({**row,
            'output_schema_digest': canonical_digest(op['schemas']['output'])})
        handlers[op['id']] = handler
    calls = []

    class Invoker:
        def invoke(self, request, **kwargs):
            calls.append(request)
            return InvocationOutcome(output=handlers[request['operation_id']](request['input'], None))

        def cancel(self, request_id):
            pass

    engine = WorkflowEngineV4(store=WorkflowStoreV4(tmp_path / 'tools.sqlite3', clock=lambda: 100.0),
                              catalog=catalog, authority=Authority(), invoker=Invoker(),
                              validator=_SchemaValidator(catalog.value['schemas']), clock=lambda: 100.0)
    provider = WorkflowProviderV4(engine)
    value = {'items': [1, 'two', None, True], 'nested': {'a.b': 'literal key'}}
    document = {'workflow_api_version': 'io.tobkiri.workflow.v4', 'name': 'Typed JSON tools',
                'steps': [_step('check', operations[0], input={'schema': {}, 'arguments': value}),
                          _step('normalize', operations[1], depends_on=['check'], input={
                              'tool_id': 'example', 'tool_call_id': 'call.1',
                              'executor_provider_instance_id': 'fixture',
                              'executor_content_hash': 'fixture',
                              'value': '${steps.check.output.arguments}'})]}
    assert engine.validate_definition(document)['valid']
    publish(provider, 'tools.chain', document)
    provider.invoke('run.create', {'definition_id': 'tools.chain', 'run_id': 'tools-run', 'inputs': {}})
    for _ in range(3):
        result = provider.invoke('run.advance', {'run_id': 'tools-run'})
        if result['run']['state'] == 'succeeded':
            break
    assert result['run']['state'] == 'succeeded'
    assert len(calls) == 2
    assert calls[1]['input']['value'] == value
