"""Captured workflow schemas cannot create an ambient network/file capability."""
from urllib import request

import pytest

from core_runtime.workflow_v4.integration import _SchemaValidator
from core_runtime.workflow_v4.models import WorkflowDenied


@pytest.mark.parametrize("reference", ["https://schema.example/private", "file:///private/schema", "urn:missing:schema"])
def test_uncaptured_schema_reference_never_uses_urlopen(monkeypatch, reference):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("ambient schema retrieval is forbidden")

    monkeypatch.setattr(request, "urlopen", forbidden)
    validator = _SchemaValidator({"schema": {"$ref": reference}})
    assert validator.validate("schema", {}) == ("schema reference is outside the captured catalog",)
    assert calls == []


def test_captured_and_local_schema_references_remain_usable():
    validator = _SchemaValidator({
        "shared": {"$id": "urn:captured:text", "type": "string"},
        "input": {"type": "object", "properties": {"text": {"$ref": "urn:captured:text"}}},
        "local": {"$defs": {"text": {"type": "string"}}, "type": "object", "properties": {"text": {"$ref": "#/$defs/text"}}},
    })
    assert validator.validate("input", {"text": "hello"}) == ()
    assert validator.validate("local", {"text": "hello"}) == ()
    assert validator.validate("input", {"text": {"secret": "never echoed"}}) == ("input does not satisfy the captured schema",)


def test_conflicting_captured_schema_ids_fail_closed():
    with pytest.raises(WorkflowDenied, match="ambiguous"):
        _SchemaValidator({"one": {"$id": "urn:duplicate", "type": "string"},
                          "two": {"$id": "urn:duplicate", "type": "integer"}})
