"""Neutral patterns carry typed intents, never presentation-derived authority."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from tobkiri_protocol.surface_templates_v1 import (
    OUTCOME_VERSION,
    PATTERN_EVENTS,
    RESOURCE_VERSION,
    normalize_surface_intent,
    validate_surface_intent,
    validate_surface_outcome,
    validate_surface_renderer,
    validate_surface_resource,
    validate_surface_resource_request,
    validate_surface_template,
)

_FIXTURE = Path(__file__).parent / "fixtures/surface_templates_v1/ten_patterns.json"


def template() -> dict[str, Any]:
    """Read the same immutable ten-pattern fixture as the frontend tests."""
    return json.loads(_FIXTURE.read_text("utf-8"))


def values(pattern: str) -> dict[str, Any]:
    """Provide one typed submission for each semantic pattern."""
    return {
        "choice": {"selected": ["one"]},
        "form": {"fields": {"title": "Review", "count": 2, "enabled": False}},
        "collection": {"selected_id": "record-one"},
        "resource_input": {
            "resource": {
                "version": RESOURCE_VERSION,
                "selection_id": "x" * 43,
                "kind": "file",
                "display_name": "Selected resource",
                "expires_at_ms": 1900000000000,
                "stage": "exchanged",
            }
        },
    }.get(pattern, {})


@pytest.mark.parametrize("pattern", list(PATTERN_EVENTS))
def test_all_patterns_produce_renderer_independent_typed_intent(pattern: str) -> None:
    """Renderer selection cannot change the declared Logic intent or outcome."""
    standard = template()
    compact = deepcopy(standard)
    compact["renderer_contribution_id"] = "fixture.compact"
    first = normalize_surface_intent(standard, pattern, "apply", values(pattern))
    second = normalize_surface_intent(compact, pattern, "apply", values(pattern))
    assert first == second
    outcome = {
        key: first[key] for key in ("template_id", "node_id", "intent_id", "event")
    }
    outcome.update(
        version=OUTCOME_VERSION, status="accepted", message="Declared intent accepted"
    )
    assert validate_surface_outcome(outcome, first) == validate_surface_outcome(
        outcome, second
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"version": "tobkiri.ui.surface-template.v2"},
        {"code": "alert(1)"},
        {"renderer_api_version": "2.0.0"},
        {"renderer_contribution_id": "https://evil.test"},
    ],
)
def test_unknown_version_or_executable_fields_fail(mutation: dict[str, Any]) -> None:
    """No executable field or future API is admitted as inert data."""
    with pytest.raises((ValidationError, ValueError)):
        validate_surface_template({**template(), **mutation})


@pytest.mark.parametrize("pattern", ["unknown", "html", "svg", "css", "javascript"])
def test_unknown_pattern_fails(pattern: str) -> None:
    value = template()
    value["nodes"][0]["pattern"] = pattern
    with pytest.raises((ValidationError, ValueError)):
        validate_surface_template(value)


@pytest.mark.parametrize(
    "pattern,missing",
    [
        ("progress", "total_path"),
        ("choice", "choices"),
        ("form", "fields"),
        ("collection", "items_path"),
        ("resource_input", "resource"),
    ],
)
def test_pattern_requires_its_exact_semantic_fields(pattern: str, missing: str) -> None:
    value = template()
    del next(node for node in value["nodes"] if node["pattern"] == pattern)[missing]
    with pytest.raises((ValidationError, ValueError)):
        validate_surface_template(value)


def test_identity_and_fixed_binding_collisions_fail() -> None:
    value = template()
    value["nodes"].append(deepcopy(value["nodes"][0]))
    with pytest.raises(ValueError, match="colliding"):
        validate_surface_template(value)
    value = template()
    value["nodes"][0]["intents"][0]["request"]["input"]["surface_intent"] = {}
    with pytest.raises(ValueError, match="binding"):
        validate_surface_template(value)


@pytest.mark.parametrize(
    "key",
    [
        "approved",
        "profile_id",
        "principal_id",
        "resource_handle",
        "__proto__",
        "_private",
    ],
)
def test_nested_client_authority_and_private_fields_fail(key: str) -> None:
    value = template()
    value["nodes"][0]["intents"][0]["request"]["input"] = {"nested": {key: "forged"}}
    with pytest.raises(ValueError):
        validate_surface_template(value)


@pytest.mark.parametrize(
    "pattern,invalid",
    [
        ("choice", {"selected": ["missing"]}),
        ("choice", {"selected": ["one", "two"]}),
        ("form", {"fields": {"title": "Review", "count": True}}),
        ("form", {"fields": {"title": "Review", "count": 11}}),
        ("form", {"fields": {"extra": "forged"}}),
        ("form", {"fields": {"count": 1}}),
        ("resource_input", {"resource": {"selection_id": "handle:forged"}}),
        ("content", {"approved": True}),
        ("confirmation", {"approved": True}),
    ],
)
def test_typed_values_cannot_widen_pattern_inputs(
    pattern: str, invalid: dict[str, Any]
) -> None:
    with pytest.raises((ValueError, ValidationError)):
        normalize_surface_intent(template(), pattern, "apply", invalid)


def test_outcome_must_match_preceding_exact_intent_and_has_no_authority_flag() -> None:
    intent = normalize_surface_intent(template(), "confirmation", "apply", {})
    outcome = {
        key: intent[key] for key in ("template_id", "node_id", "intent_id", "event")
    }
    outcome.update(version=OUTCOME_VERSION, status="pending")
    assert validate_surface_outcome(outcome, intent)["status"] == "pending"
    for changed in (
        {"node_id": "other"},
        {"event": "decline"},
        {"approved": True},
        {"status": "success"},
    ):
        with pytest.raises((ValueError, ValidationError)):
            validate_surface_outcome({**outcome, **changed}, intent)


@pytest.mark.parametrize("implementation", ["semantic_standard", "semantic_compact"])
def test_renderer_api_selects_only_finite_shipped_implementations(
    implementation: str,
) -> None:
    descriptor = {
        "version": "tobkiri.ui.surface-renderer.v1",
        "api_version": "1.0.0",
        "implementation": implementation,
        "patterns": list(PATTERN_EVENTS),
        "ttl_ms": 30000,
    }
    assert validate_surface_renderer(descriptor) == descriptor
    for changed in (
        {"implementation": "https://evil.test/module.js"},
        {"module": "evil.js"},
        {"api_version": "2.0.0"},
        {"patterns": ["unknown"]},
    ):
        with pytest.raises(ValidationError):
            validate_surface_renderer({**descriptor, **changed})


def test_resource_wire_is_inert_and_exact_stage_kind_are_required() -> None:
    resource = values("resource_input")["resource"]
    assert (
        validate_surface_resource(resource, kind="file", stage="exchanged") == resource
    )
    for changed in (
        {"stage": "selected"},
        {"kind": "image"},
        {"selection_id": "handle:forged"},
        {"approved": True},
    ):
        with pytest.raises((ValueError, ValidationError)):
            validate_surface_resource(
                {**resource, **changed}, kind="file", stage="exchanged"
            )


def test_received_intent_is_bound_to_exact_version_node_event_and_typed_values() -> (
    None
):
    """Logic can validate the same finite wire received through an action."""
    intent = normalize_surface_intent(template(), "form", "apply", values("form"))
    assert validate_surface_intent(intent, template()) == intent
    for change in (
        {"version": "tobkiri.ui.surface-intent.v2"},
        {"event": "confirm"},
        {"node_id": "other"},
        {"approved": True},
        {"values": {"fields": {"title": "Review", "profile_id": "forged"}}},
    ):
        with pytest.raises((ValueError, ValidationError)):
            validate_surface_intent({**intent, **change}, template())


def test_resource_action_request_is_finite_and_never_accepts_authority_or_paths() -> (
    None
):
    """The public action schema has no executable/client identity escape hatch."""
    acquire = {"operation": "acquire", "kind": "file"}
    exchange = {"operation": "exchange", "kind": "file", "selection_id": "x" * 43}
    assert validate_surface_resource_request(acquire) == acquire
    assert validate_surface_resource_request(exchange) == exchange
    for invalid in (
        {**acquire, "selection_id": "x" * 43},
        {"operation": "exchange", "kind": "file"},
        {**exchange, "selection_id": "handle:forged"},
        {**acquire, "approved": True},
        {**acquire, "profile_id": "other"},
        {**acquire, "file_path": "/private/file"},
    ):
        with pytest.raises(ValidationError):
            validate_surface_resource_request(invalid)
