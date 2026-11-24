"""Display annotations are not validators, executable widgets, or authority."""
import pytest

from ecosystem.rumi_tool_validation_pack.runtime.validator import create_validate_operation


def validate(schema, value):
    return create_validate_operation(None)('validate', {'schema': schema, 'arguments': value})


@pytest.mark.parametrize('role', ['data', 'configuration', 'metadata'])
def test_display_roles_preserve_underlying_value_validation(role):
    schema = {'type': 'integer', 'minimum': 2, 'x-tobkiri-flow-role': role}
    assert validate(schema, 3)['valid']
    assert not validate(schema, 1)['valid']
    assert not validate(schema, '3')['valid']


@pytest.mark.parametrize('selector', ['model-profile', 'tool-definition', ['model-profile']])
def test_identifier_selectors_are_bounded_string_hints(selector):
    schema = {'type': 'string', 'minLength': 1, 'x-tobkiri-selector': selector}
    assert validate(schema, 'registered.id')['valid']
    assert not validate(schema, '')['valid']
    assert not validate(schema, 1)['valid']


def test_nested_hints_do_not_disable_structural_constraints():
    item = {'type': 'string', 'x-tobkiri-flow-role': 'configuration', 'x-tobkiri-selector': 'model-profile'}
    schema = {'type': 'object', 'properties': {'models': {'type': 'array', 'items': item}}, 'additionalProperties': False}
    assert validate(schema, {'models': ['registered']})['valid']
    assert not validate(schema, {'models': [1]})['valid']
    assert not validate(schema, {'unknown': 1})['valid']


@pytest.mark.parametrize('schema', [
    {'type': 'string', 'x-tobkiri-flow-role': 'admin'},
    {'type': 'string', 'x-tobkiri-flow-role': {'grant': True}},
    {'type': 'integer', 'x-tobkiri-selector': 'model-profile'},
    {'type': 'string', 'x-tobkiri-selector': []},
    {'type': 'string', 'x-tobkiri-selector': ['model-profile'] * 9},
    {'type': 'string', 'x-tobkiri-selector': 'credential'},
    {'type': 'string', 'x-script': 'execute()'},
    {'type': 'object', 'x-tobkiri-value-type': 'tobkiri.value.sound.v1'},
    {'type': 'string', 'pattern': '.*'},
    {'$ref': 'https://example.invalid/schema'},
])
def test_unknown_semantics_and_malformed_display_annotations_stay_rejected(schema):
    with pytest.raises(ValueError):
        validate(schema, None)
