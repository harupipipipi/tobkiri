"""Coverage diagnostics are specific to operations and do not infer semantics."""
import pytest

from scripts.quality.scan_flow_contracts import classify, inventory

pytestmark = pytest.mark.contract


def test_schema_classification_distinguishes_empty_generic_and_composite():
    assert classify({'type': 'object'}) == 'generic-object'
    assert classify({'type': 'object', 'additionalProperties': False}) == 'empty-object'
    assert classify({'oneOf': [{'type': 'object'}, {'type': 'null'}]}) == 'composite-schema'
    assert classify({'type': 'object', 'properties': {'text': {'type': 'string'}}}) == 'explicit-fields'
    assert classify({}) == 'untyped'


def test_operation_overrides_do_not_overstate_sibling_coverage():
    report = inventory({'packs': [{'pack_id': 'sample', 'provided_contracts': [{
        'contract_id': 'sample.v1', 'owner': 'sample',
        'schemas': {'input': {'type': 'object'}, 'output': {'type': 'object'}},
        'operations': [{'id': 'read'}, {'id': 'write', 'schemas': {'input': {
            'type': 'object', 'properties': {'text': {'type': 'string', 'x-tobkiri-flow-role': 'data'}}}}}],
    }]}]})
    assert report['operation_count'] == 2
    assert report['classification_counts']['input'] == {'explicit-fields': 1, 'generic-object': 1}
    assert report['operations'][0]['input']['fields'] == []
    assert report['authority'] == 'diagnostic-only'
