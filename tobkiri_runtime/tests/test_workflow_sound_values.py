"""Sound is a reusable value contract, not a provider-specific JSON socket."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from core_runtime.workflow_v4.binding import project_display_schema
from core_runtime.workflow_v4.integration import _SchemaValidator
from core_runtime.workflow_v4.value_binding import value_binding_errors
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.flow_values import (
    FLOW_ROLE_KEY, SOUND_TYPE, VALUE_TYPE_KEY, sound_schema, validate_sound,
)


def sound():
    data = b'\xff\xfbtest-audio'
    return {'content_id': 'sha256:' + hashlib.sha256(data).hexdigest(),
            'media_type': 'audio/mpeg', 'byte_size': len(data),
            'data_base64': base64.b64encode(data).decode()}


def wrap(name, schema):
    return {'type': 'object', 'required': [name], 'properties': {name: schema}}


def fixture(source=None, target=None):
    source = sound_schema() if source is None else source
    target = sound_schema() if target is None else target
    schemas = [wrap('text', {'type': 'string'}), wrap('sound', source),
               wrap('audio', target), wrap('done', {'type': 'boolean'})]
    digests = [canonical_digest(s) for s in schemas]
    identities = [{'contract_id': f'independent.{name}.v1',
                   'contract_revision_digest': 'sha256:' + char * 64,
                   'operation_id': f'{name}.invoke',
                   'function_principal_id': f'pack.{name}.function'}
                  for name, char in [('producer', 'a'), ('consumer', 'b')]]
    snapshot = {'operations': [{**k, 'input_schema_digest': digests[i * 2]}
                               for i, k in enumerate(identities)],
                'operation_output_schemas': [{**k, 'output_schema_digest': digests[i * 2 + 1]}
                                             for i, k in enumerate(identities)],
                'schemas': dict(zip(digests, schemas))}
    steps = [{'id': 'producer', 'request': {**identities[0], 'input': {'text': 'hello'}}},
             {'id': 'consumer', 'depends_on': ['producer'],
              'request': {**identities[1], 'input': {'audio': '${steps.producer.output.sound}'}}}]
    return steps, snapshot


def test_independent_pack_names_bind_sound_without_implementation_knowledge():
    steps, snapshot = fixture()
    assert value_binding_errors(steps, snapshot) == []
    value = sound()
    schema = wrap('audio', sound_schema())
    validator = _SchemaValidator({canonical_digest(schema): schema})
    assert validator.validate(canonical_digest(schema), {'audio': value}) == ()


@pytest.mark.parametrize('schema', [
    {'type': 'string'}, {'type': 'object'},
    {**sound_schema(), VALUE_TYPE_KEY: 'other.value.image.v1'},
])
def test_shape_or_unrelated_value_name_cannot_satisfy_sound_port(schema):
    steps, snapshot = fixture(source=schema)
    assert value_binding_errors(steps, snapshot) == ['connected value types do not match']


@pytest.mark.parametrize('change', [
    {'content_id': 'sha256:' + '0' * 64}, {'byte_size': 2},
    {'media_type': 'text/plain'}, {'data_base64': '!!!!'},
    {'url': 'https://example.invalid/secret'}, {'filename': '../clip.mp3'},
    {'byte_size': True},
])
def test_resolved_value_rejected_by_actual_validator_before_sink(change):
    schema = wrap('audio', sound_schema())
    validator = _SchemaValidator({canonical_digest(schema): schema})
    with pytest.raises(ValueError):
        validate_sound({**sound(), **change})
    assert validator.validate(canonical_digest(schema), {'audio': {**sound(), **change}})


def test_hints_project_without_granting_authority_and_sound_is_consistent():
    schema = {**sound_schema(), FLOW_ROLE_KEY: 'data', 'x-authority': 'grant'}
    projected = project_display_schema(schema)
    assert projected[VALUE_TYPE_KEY] == SOUND_TYPE
    assert projected[FLOW_ROLE_KEY] == 'data'
    assert 'x-authority' not in projected
    bad = project_display_schema({'type': 'object', VALUE_TYPE_KEY: 'https://evil/',
                                  FLOW_ROLE_KEY: {'grant': True}})
    assert VALUE_TYPE_KEY not in bad and FLOW_ROLE_KEY not in bad
    catalog = json.loads((Path(__file__).parents[1] / 'schemas/pack_v4_catalog.v1.json').read_text())
    contracts = {c['contract_id']: c for p in catalog['packs'] for c in p.get('provided_contracts', [])}
    tts = contracts['tobkiri.service.ai.audio.speech.v1']['schemas']
    stt = contracts['tobkiri.service.ai.audio.transcribe.v1']['schemas']
    assert tts['output']['properties']['content'] == stt['input']['properties']['audio']
    assert tts['input']['properties']['model_profile_id'][FLOW_ROLE_KEY] == 'configuration'
    assert tts['input']['properties']['input'][FLOW_ROLE_KEY] == 'data'
    copy_schema = sound_schema()
    copy_schema['properties'].clear()
    assert sound_schema()['properties']


def test_modified_schema_bytes_do_not_create_trusted_typed_producer():
    steps, snapshot = fixture()
    snapshot = copy.deepcopy(snapshot)
    output = snapshot['operation_output_schemas'][0]['output_schema_digest']
    snapshot['schemas'][output]['description'] = 'not pinned bytes'
    assert value_binding_errors(steps, snapshot)


@pytest.mark.parametrize("corrupt", [False, True])
def test_three_independent_operations_carry_the_same_sound_value(tmp_path, corrupt):
    from core_runtime.workflow_v4 import WorkflowEngineV4, WorkflowProviderV4, WorkflowStoreV4
    from core_runtime.workflow_v4.models import InvocationOutcome
    from tests.test_workflow_v4_step_output_binding import (
        Authority, Catalog, _operation, _catalog_operation, _step, publish,
    )
    ops = [
        _operation('make', input_schema=wrap('text', {'type': 'string'}),
                   output_schema=wrap('sound', sound_schema())),
        _operation('forward', input_schema=wrap('sound', sound_schema()),
                   output_schema=wrap('sound', sound_schema())),
        _operation('inspect', input_schema=wrap('sound', sound_schema()),
                   output_schema=wrap('size', {'type': 'integer'})),
    ]
    catalog = Catalog()
    catalog.value['operations'] = [_catalog_operation(op) for op in ops]
    catalog.value['schemas'] = {op[key]: op[schema]
                               for op in ops
                               for key, schema in [('input_schema_digest', '_input_schema'),
                                                   ('_output_schema_digest', '_output_schema')]}
    catalog.value['operation_output_schemas'] = [
        {**_catalog_operation(op), 'output_schema_digest': op['_output_schema_digest']}
        for op in ops]
    calls = []

    class Invoker:
        def invoke(self, request, **kwargs):
            calls.append(request)
            if request['operation_id'] == 'make.run':
                return InvocationOutcome(output={'sound': {**sound(), **({'byte_size': 1} if corrupt else {})}})
            validate_sound(request['input']['sound'])
            if request['operation_id'] == 'forward.run':
                return InvocationOutcome(output={'sound': request['input']['sound']})
            return InvocationOutcome(output={'size': request['input']['sound']['byte_size']})

        def cancel(self, request_id):
            pass

    authority = Authority()
    engine = WorkflowEngineV4(
        store=WorkflowStoreV4(tmp_path / 'sound.sqlite3', clock=lambda: 100.0),
        catalog=catalog, authority=authority, invoker=Invoker(),
        validator=_SchemaValidator(catalog.value['schemas']), clock=lambda: 100.0,
    )
    provider = WorkflowProviderV4(engine)
    document = {'workflow_api_version': 'io.tobkiri.workflow.v4', 'name': 'Sound transfer',
                'steps': [_step('make', ops[0], input={'text': 'hello'}),
                          _step('forward', ops[1], depends_on=['make'],
                                input={'sound': '${steps.make.output.sound}'}),
                          _step('inspect', ops[2], depends_on=['forward'],
                                input={'sound': '${steps.forward.output.sound}'})]}
    assert engine.validate_definition(document)['valid']
    publish(provider, 'sound.chain', document)
    provider.invoke('run.create', {'definition_id': 'sound.chain', 'run_id': 'sound-chain', 'inputs': {}})
    if corrupt:
        from core_runtime.workflow_v4 import WorkflowValidationError
        provider.invoke('run.step.execute', {'run_id': 'sound-chain', 'step_id': 'make'})
        with pytest.raises(WorkflowValidationError):
            provider.invoke('run.step.execute', {'run_id': 'sound-chain', 'step_id': 'forward'})
        assert len(calls) == 1
        assert authority.commit_count == 1
        return
    for _ in range(4):
        result = provider.invoke('run.advance', {'run_id': 'sound-chain'})
        if result['run']['state'] == 'succeeded':
            break
    assert result['run']['state'] == 'succeeded'
    assert len(calls) == 3
    assert calls[1]['input']['sound'] == calls[2]['input']['sound'] == sound()
    assert authority.commit_count == 3
    invalid = copy.deepcopy(document)
    invalid['steps'][1]['request']['input']['sound'] = '${steps.make.output.sound.content_id}'
    assert not engine.validate_definition(invalid)['valid']
    assert authority.commit_count == 3


def test_absent_optional_output_metadata_fails_named_connections_cleanly():
    steps, snapshot = fixture()
    snapshot['operation_output_schemas'] = None
    assert value_binding_errors(steps, snapshot) == ['connected value types do not match']


@pytest.mark.parametrize("key", ["payload", "[]", "", "a.b", "画像"])
def test_container_reference_preserves_nested_nominal_identity(key):
    source = wrap(key, sound_schema())
    target = wrap(key, {**sound_schema(), VALUE_TYPE_KEY: "other.value.image.v1"})
    steps, snapshot = fixture(source=source, target=target)
    assert value_binding_errors(steps, snapshot) == ["connected value types do not match"]
    steps, snapshot = fixture(source=source, target=source)
    assert value_binding_errors(steps, snapshot) == []


def test_pointer_object_brackets_are_not_an_array_marker():
    steps, snapshot = fixture(source=wrap("[]", sound_schema()), target={"type": "object"})
    steps[1]["request"]["input"]["audio"] = "${steps.producer.output@/sound/[]}"
    assert value_binding_errors(steps, snapshot) == ["connected value types do not match"]


@pytest.mark.parametrize("keyword", ["items", "additionalProperties"])
def test_container_schema_does_not_erase_nominal_children(keyword):
    source = {"type": "array" if keyword == "items" else "object", keyword: sound_schema()}
    target = {"type": source["type"], keyword: {"type": "object"}}
    steps, snapshot = fixture(source=source, target=target)
    assert value_binding_errors(steps, snapshot)


def test_prefix_items_resolves_exact_literal_index_without_bracket_sentinel():
    from core_runtime.workflow_v4.value_binding import _at
    schema = {"type": "array", "prefixItems": [sound_schema()], "items": {"type": "string"}}
    assert _at(schema, (0,)) == sound_schema()
    assert _at(schema, ("0",)) == sound_schema()
    assert _at(schema, ("1",)) == {"type": "string"}
    assert _at(schema, ("01",)) == {}
    assert _at(schema, ("[]",)) == {}


@pytest.mark.parametrize("schema", [
    {"type": "object", "patternProperties": {"^payload$": sound_schema()}},
    {"if": {}, "then": {"properties": {"payload": sound_schema()}}},
    {"anyOf": [{"$ref": "#/$defs/Named"}]},
    {"items": sound_schema()},
    {"type": ["array", "object"], "items": sound_schema()},
])
def test_unprovable_schema_paths_fail_closed(schema):
    from core_runtime.workflow_v4.value_binding import _at, _semantic_match
    assert not _semantic_match(_at(schema, ("payload",)), {})


def test_singleton_array_type_preserves_item_identity():
    from core_runtime.workflow_v4.value_binding import _at
    assert _at({"type": ["array"], "items": sound_schema()}, ("0",)) == sound_schema()



def test_shape_only_applicators_do_not_hide_explicit_nominal_paths():
    from core_runtime.workflow_v4.value_binding import _at, _semantic_match
    schema = {**wrap("payload", sound_schema()), "anyOf": [{"required": ["payload"]}]}
    assert _at(schema, ("payload",)) == sound_schema()
    assert _semantic_match(schema, schema)


@pytest.mark.parametrize("applicator", ["anyOf", "dependentSchemas"])
def test_applicator_nested_nominal_annotations_are_not_erased(applicator):
    from core_runtime.workflow_v4.value_binding import _semantic_match
    child = {"properties": {"payload": sound_schema()}}
    schema = {applicator: [child] if applicator == "anyOf" else {"flag": child}}
    assert not _semantic_match(schema, {})
