"""Conformance tests for renderer-neutral Surface Template contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core_runtime.surface_templates import (
    OperationBinding,
    OperationRegistry,
    ReactSurface,
    RecordingSurface,
    SurfacePack,
    SurfaceResourceDenied,
    SurfaceTemplateValidationError,
    resolve_surface_templates,
    validate_surface_template,
)


FIXTURES = Path(__file__).parent / "fixtures" / "surface_templates" / "packs.json"
DIGEST = "sha256:" + "a" * 64


@pytest.fixture()
def fixture_packs() -> list[SurfacePack]:
    """Load the split Logic, Surface, and Renderer fixture Packs."""

    return [SurfacePack.from_mapping(item) for item in json.loads(FIXTURES.read_text())]


def _profile_lock(packs: list[SurfacePack]) -> tuple[dict[str, object], dict[str, object]]:
    profile = {
        "profile_id": "fixture-profile",
        "profile_revision": "profile-revision-1",
        "plan_digest": "sha256:" + "9" * 64,
        "packs": [
            {"pack_id": item.pack_id, "artifact_digest": item.artifact_digest} for item in packs
        ],
    }
    lock = {
        "profile_revision": profile["profile_revision"],
        "plan_digest": profile["plan_digest"],
        "effective_set": [
            {"identity": item.pack_id, "artifact_digest": item.artifact_digest} for item in packs
        ],
    }
    return profile, lock


def test_split_packs_resolve_with_exact_profile_lock_and_renderer_pins(
    fixture_packs: list[SurfacePack],
) -> None:
    """Logic and Surface Packs compose independently through a Renderer Pack."""

    profile, lock = _profile_lock(fixture_packs)
    resolution = resolve_surface_templates(
        fixture_packs,
        profile=profile,
        profile_lock=lock,
    )

    assert resolution.activation_allowed
    assert resolution.profile_id == "fixture-profile"
    assert resolution.profile_revision == "profile-revision-1"
    assert resolution.plan_digest == profile["plan_digest"]
    assert {item.template_id for item in resolution.templates} == {
        "example.image.inspect.default",
        "example.search.choice.default",
    }
    assert resolution.renderer_pins[0].renderer_id == "defaultspack.react.surface"
    assert resolution.to_dict()["profile_lock"]["surface_resolution_digest"].startswith("sha256:")


def test_recording_surface_and_react_adapter_process_unchanged_image_template(
    fixture_packs: list[SurfacePack],
) -> None:
    """Renderer implementations receive the same semantic template/events."""

    resolution = resolve_surface_templates(fixture_packs)
    registry = OperationRegistry.from_packs(fixture_packs)
    recording = RecordingSurface(resolution, operation_registry=registry)
    react = ReactSurface(resolution, operation_registry=registry)
    event = {"kind": "success", "result": {"media_type": "image/png"}}

    recorded = recording.process("example.image.inspect.default", event)
    rendered = react.render_model("example.image.inspect.default", event)

    assert recorded[0].pattern == rendered[0]["type"] == "content"
    assert recorded[0].payload == rendered[0]["props"]
    assert recording.transcript_digest.startswith("sha256:")


def test_resource_input_accepts_only_host_issued_handle(
    fixture_packs: list[SurfacePack],
) -> None:
    """A GUI path never crosses the Pack boundary as a resource."""

    resolution = resolve_surface_templates(fixture_packs)
    surface = RecordingSurface(resolution)
    intent = surface.input_intent(
        "example.image.inspect.default",
        {"resource": "handle:image/fixture-1"},
    )
    assert intent.payload == {"resource": "handle:image/fixture-1"}
    with pytest.raises(SurfaceResourceDenied):
        surface.input_intent(
            "example.image.inspect.default",
            {"resource": r"C:\Users\example\image.png"},
        )
    with pytest.raises(SurfaceResourceDenied):
        surface.input_intent(
            "example.image.inspect.default",
            {"resource": "file:///tmp/image.png"},
        )
    with pytest.raises(SurfaceResourceDenied, match="kind"):
        surface.input_intent(
            "example.image.inspect.default",
            {"resource": "handle:file/not-an-image"},
        )


def test_operation_error_progress_notice_and_content_remain_distinct(
    fixture_packs: list[SurfacePack],
) -> None:
    """Typed operation outcomes map to separate semantic patterns."""

    surface = RecordingSurface(resolve_surface_templates(fixture_packs))
    template_id = "example.image.inspect.default"
    assert (
        surface.process(template_id, {"kind": "progress", "progress": {"current": 1, "total": 2}})[
            0
        ].pattern
        == "progress"
    )
    assert (
        surface.process(
            template_id, {"kind": "error", "error": {"code": "unsupported", "message": "No"}}
        )[0].pattern
        == "problem"
    )
    assert (
        surface.process(template_id, {"kind": "success", "result": {"ok": True}})[0].pattern
        == "content"
    )
    notice = {
        "kind": "notice",
        "notice": {"level": "success", "message": "Done", "dedupe_key": "x"},
    }
    assert surface.process(template_id, notice)[0].pattern == "notice"
    assert surface.process(template_id, notice) == ()


def test_registered_action_requires_authority_and_uses_bounded_binding(
    fixture_packs: list[SurfacePack],
) -> None:
    """Actions cannot become frontend callbacks or self-approved effects."""

    search = next(item for item in fixture_packs if item.pack_id == "logic.search.choice")
    registry = OperationRegistry(
        [
            OperationBinding(
                contract_id="example.search.v1",
                operation_id="search",
                authority_required=True,
                source_pack_id=search.pack_id,
            )
        ]
    )
    surface = RecordingSurface(
        resolve_surface_templates(fixture_packs), operation_registry=registry
    )
    event = {"input": {"query": "image"}}
    with pytest.raises(Exception, match="Authority denied"):
        surface.invoke_action("example.search.choice.default", 0, event)
    allowed = RecordingSurface(
        surface.resolution,
        operation_registry=registry,
        authority_checker=lambda _operation, payload: payload == {"query": "image"},
    )
    intent = allowed.invoke_action("example.search.choice.default", 0, event)
    assert intent.pattern == "action"
    assert intent.payload["operation_id"] == "search"


def test_action_payload_rejects_nested_local_paths(
    fixture_packs: list[SurfacePack],
) -> None:
    """A nested action object cannot smuggle ambient filesystem paths."""

    original = next(item for item in fixture_packs if item.pack_id == "surface.search.choice")
    document = {**original.templates[0]}
    document["actions"] = [
        {
            "contract_id": "example.search.v1",
            "operation_id": "search",
            "payload_binding": {"query": "$.input"},
        }
    ]
    replacement = SurfacePack(**{**original.__dict__, "templates": (document,)})
    packs = [item for item in fixture_packs if item.pack_id != original.pack_id] + [replacement]
    surface = RecordingSurface(
        resolve_surface_templates(packs),
        operation_registry=OperationRegistry.from_packs(packs),
    )

    with pytest.raises(SurfaceResourceDenied, match="local filesystem"):
        surface.invoke_action(
            "example.search.choice.default",
            0,
            {"input": {"filters": [{"local_path": r"C:\secrets\token.txt"}]}},
        )


def test_unselected_surface_pack_is_not_discovered(fixture_packs: list[SurfacePack]) -> None:
    """Only Packs in the active Profile are projected."""

    selected = ["logic.image.inspect", "surface.image.inspect", "renderer.defaultspack.react"]
    resolution = resolve_surface_templates(fixture_packs, selected_pack_ids=selected)
    assert [item.template_id for item in resolution.templates] == ["example.image.inspect.default"]
    assert "example.search.choice.default" not in {
        item.template_id for item in resolution.templates
    }


def test_disable_update_and_uninstall_remove_projection_without_stale_paths(
    fixture_packs: list[SurfacePack],
) -> None:
    """Lifecycle state is re-resolved and never leaves old templates active."""

    selected = {item.pack_id for item in fixture_packs}
    disabled = [
        SurfacePack(**{**item.__dict__, "enabled": False})
        if item.pack_id == "surface.image.inspect"
        else item
        for item in fixture_packs
    ]
    resolution = resolve_surface_templates(disabled, selected_pack_ids=selected)
    assert "example.image.inspect.default" not in {
        item.template_id for item in resolution.templates
    }
    assert any(item.code == "SURFACE_PACK_DISABLED" for item in resolution.diagnostics)


def test_collision_unknown_pattern_and_executable_binding_fail_closed(
    fixture_packs: list[SurfacePack],
) -> None:
    """Public IDs and the canonical document language are fail-closed."""

    first = next(item for item in fixture_packs if item.pack_id == "surface.image.inspect")
    duplicate = SurfacePack(
        pack_id="surface.image.inspect.duplicate",
        artifact_digest="sha256:" + "6" * 64,
        role="surface",
        trust_class="local",
        templates=first.templates,
    )
    resolution = resolve_surface_templates([*fixture_packs, duplicate])
    assert any(item.code == "SURFACE_TEMPLATE_COLLISION" for item in resolution.diagnostics)

    invalid = dict(first.templates[0])
    invalid["input"] = {"pattern": "unknown"}
    with pytest.raises(SurfaceTemplateValidationError):
        validate_surface_template(invalid)
    executable = dict(first.templates[0])
    executable["renderer"] = "FancyReactComponent"
    with pytest.raises(SurfaceTemplateValidationError):
        validate_surface_template(executable)


def test_security_sensitive_templates_require_system_owned_surface_pack(
    fixture_packs: list[SurfacePack],
) -> None:
    """A normal Pack cannot replace Host-owned confirmation boundaries."""

    original = next(item for item in fixture_packs if item.pack_id == "surface.image.inspect")
    document = {**original.templates[0]}
    document["input"] = {"pattern": "confirmation", "label": "Approve"}
    document["outcomes"] = {
        "success": {"pattern": "confirmation", "message": "$.result.message"}
    }
    replacement = SurfacePack(**{**original.__dict__, "templates": (document,)})
    packs = [item for item in fixture_packs if item.pack_id != original.pack_id] + [replacement]

    resolution = resolve_surface_templates(packs)

    assert "example.image.inspect.default" not in {
        item.template_id for item in resolution.templates
    }
    assert any(
        item.code == "SURFACE_TEMPLATE_INVALID" and "system Surface Pack" in item.message
        for item in resolution.diagnostics
    )


def test_renderer_capability_collision_is_deterministic(
    fixture_packs: list[SurfacePack],
) -> None:
    """Two renderer providers cannot win a pattern by load order."""

    duplicate = {
        "renderer_id": "other.react.surface",
        "renderer_api_version": "io.tobkiri.surface-renderer.v1",
        "supported_patterns": ["content"],
        "renderer_digest": DIGEST,
        "trusted": True,
    }
    resolution = resolve_surface_templates(fixture_packs, renderer_providers=[duplicate])

    assert not resolution.activation_allowed
    assert any(
        item.code == "SURFACE_RENDERER_COLLISION" and item.subject == "content"
        for item in resolution.diagnostics
    )


def test_missing_interactive_renderer_blocks_but_ordinary_content_can_fallback(
    fixture_packs: list[SurfacePack],
) -> None:
    """Interactive patterns require a provider while ordinary content falls back."""

    no_renderer = [item for item in fixture_packs if item.role != "renderer"]
    resolution = resolve_surface_templates(no_renderer)
    assert resolution.activation_allowed is False
    assert any(item.code == "SURFACE_RENDERER_MISSING" for item in resolution.diagnostics)

    ordinary_template = {
        "surface_api_version": "io.tobkiri.surface-template.v1",
        "template_id": "example.search.content",
        "version": "1.0.0",
        "binds": {"contract_id": "example.search.v1", "operation_id": "search"},
        "input": {"pattern": "content"},
        "outcomes": {"success": {"pattern": "content", "data": "$.result"}},
    }
    ordinary_pack = SurfacePack(
        pack_id="surface.search.content",
        artifact_digest="sha256:" + "7" * 64,
        role="surface",
        trust_class="local",
        templates=(ordinary_template,),
    )
    image_only = [
        item
        for item in no_renderer
        if item.pack_id not in {"surface.image.inspect", "surface.search.choice"}
    ]
    ordinary = resolve_surface_templates([*image_only, ordinary_pack])
    assert ordinary.activation_allowed
    assert "content" in ordinary.fallback_patterns


def test_profile_lock_digest_mismatch_and_renderer_api_mismatch_are_diagnostic(
    fixture_packs: list[SurfacePack],
) -> None:
    """Stale Profile/Renderer identities never silently win resolution."""

    profile, lock = _profile_lock(fixture_packs)
    bad_lock = {
        **lock,
        "effective_set": [
            *lock["effective_set"][:-1],
            {"identity": "renderer.defaultspack.react", "artifact_digest": DIGEST},
        ],
    }
    resolution = resolve_surface_templates(fixture_packs, profile=profile, profile_lock=bad_lock)
    assert not resolution.activation_allowed
    assert any(item.code == "SURFACE_PACK_DIGEST_MISMATCH" for item in resolution.diagnostics)

    mismatch = {
        "renderer_id": "old.renderer",
        "renderer_api_version": "io.tobkiri.surface-renderer.v0",
        "supported_patterns": ["content"],
        "renderer_digest": DIGEST,
        "trusted": True,
    }
    warning = resolve_surface_templates(
        [item for item in fixture_packs if item.role != "renderer"],
        renderer_providers=[mismatch],
    )
    assert any(item.code == "SURFACE_RENDERER_API_VERSION_MISMATCH" for item in warning.diagnostics)
