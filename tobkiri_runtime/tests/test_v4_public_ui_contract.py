"""Current v4 view and mediated operation contracts, independent of Pack internals."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import pytest
from jsonschema.exceptions import ValidationError

from core_runtime.global_contracts.capability_capture import (
    capture_capability_binding_snapshot,
)
from core_runtime.global_contracts.http_contract_dispatch import HTTPContractBinding
from ecosystem.defaultspack.defaultspack.http_dynamic_targets import (
    defaultspack_dynamic_capability_targets,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from ecosystem.defaultspack.defaultspack.v4_view_contract import (
    validate_catalog_view,
    validate_public_input,
    validate_schema_declared_profile_targets,
)
from tests.test_v4_frontend_contributions import (
    DESCRIPTOR,
    PACK_ID,
    _admit_fixture,
    _digest,
    _project,
    _write,
)
from core_runtime.pack_sdk import refresh_scaffold_artifacts, scaffold_pack
from tobkiri_protocol.canonical import canonical_digest


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["profile_id", "settings"],
    "properties": {
        "profile_id": {"type": "string"},
        "settings": {
            "type": "object",
            "additionalProperties": False,
            "required": ["enabled"],
            "properties": {"enabled": {"type": "boolean"}},
        },
    },
}


def test_domain_target_schema_walk_bounds_cyclic_local_alternatives() -> None:
    """Repeated refs cannot exponentially expand the schema-only path walk."""
    schema = {
        "properties": {"policy": {"$ref": "#/$defs/target"}},
        "$defs": {"target": {
            "anyOf": [{"$ref": "#/$defs/target"}, {"$ref": "#/$defs/target"}],
            "properties": {"profile_id": {"type": "string"}},
        }},
    }
    validate_schema_declared_profile_targets(
        {"policy": {"profile_id": "model-target"}}, schema
    )


def _catalog(schema: Mapping[str, Any] = SCHEMA) -> dict[str, Any]:
    return {
        "packs": [
            {
                "pack_id": "qa.logic",
                "enabled": True,
                "approved": True,
                "artifact_digest": "sha256:logic",
                "operations": [
                    {
                        "contract_id": "qa.logic.v1",
                        "operation_id": "settings.update",
                        "provider_id": "qa.logic.update",
                        "function_id": "qa.logic.update",
                        "invokable": True,
                        "input_schema": schema,
                    }
                ],
            }
        ]
    }


class _Session:
    profile_id = "profile"
    profile_revision = "revision"
    activation_id = "activation"
    plan_digest = "plan"

    def __init__(
        self, *, ready: bool = True, current: bool = True, effect_class: str = "write"
    ) -> None:
        self.ready = ready
        self.current = current
        self.effect_class = effect_class

    def assert_current(self) -> None:
        if not self.current:
            raise RuntimeError("stale capture")

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        return (
            (
                {
                    "provider_id": "qa.logic.update",
                    "function_id": "qa.logic.update",
                    "operation_id": "settings.update",
                    "effect_class": self.effect_class,
                    "artifact_digest": "sha256:logic",
                    "profile_id": self.profile_id,
                    "profile_revision": self.profile_revision,
                    "activation_id": self.activation_id,
                    "plan_digest": self.plan_digest,
                },
            )
            if contract_id == "qa.logic.v1"
            else ()
        )

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        if not self.ready:
            raise RuntimeError("provider unavailable")


def _binding() -> HTTPContractBinding:
    return HTTPContractBinding(
        method="POST", path="/api/ui/capability/invoke", presentation="capability", targets=()
    )


@pytest.mark.parametrize(
    ("effect_class", "read_only"),
    [("pure", True), ("read", True), ("write", False), ("privileged", False),
     ("external_effect", False), ("unknown", False)],
)
def test_automatic_read_evidence_comes_only_from_selected_operation(
    effect_class: str, read_only: bool,
) -> None:
    """A catalog/display hint cannot turn a captured mutation into a source."""
    catalog = _catalog()
    catalog["packs"][0]["operations"][0]["read_only"] = True
    snapshot = _snapshot(_Session(effect_class=effect_class), catalog)
    assert len(snapshot.targets) == 1
    assert snapshot.targets[0].read_only is read_only
    assert _snapshot(_Session(effect_class="read")).catalog_hash != (
        _snapshot(_Session(effect_class="write")).catalog_hash
    )


def _snapshot(session: _Session, catalog: Mapping[str, object] | None = None):
    return capture_capability_binding_snapshot(
        _binding(),
        session=session,
        catalog=catalog or _catalog(),
        dynamic_target_factory=defaultspack_dynamic_capability_targets,
    )


def _body(snapshot) -> dict[str, Any]:
    return {
        "request_id": str(uuid.uuid4()),
        "expires_at": time.time() + 30,
        "profile_id": "profile",
        "profile_revision": "revision",
        "activation_id": "activation",
        "plan_hash": "plan",
        "catalog_hash": snapshot.catalog_hash,
        "contribution_id": "pack.qa.logic.settings.update",
        "owner_pack_id": "qa.logic",
        "contract_id": "qa.logic.v1",
        "payload": {"settings": {"enabled": True}},
    }


def test_nested_operation_schema_is_captured_and_profile_is_host_derived() -> None:
    session = _Session()
    snapshot = _snapshot(session)
    assert len(snapshot.targets) == 1
    target = snapshot.targets[0]
    assert target.allowed_payload_keys == {"settings"}
    assert json.loads(target.input_schema) == SCHEMA
    presented = DefaultspackHTTPPresentation().normalize_payload(
        target,
        {"settings": {"enabled": True}},
        session=session,
        workspace_binding_resolver=None,
    )
    assert presented == {"profile_id": "profile", "settings": {"enabled": True}}


def test_nested_model_policy_target_is_domain_data_but_execution_profile_is_captured() -> None:
    schema = {
        **SCHEMA,
        "properties": {
            **SCHEMA["properties"],
            "model_policy": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "fixed": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"profile_id": {"type": "string"}},
                    },
                },
            },
        },
    }
    session = _Session()
    target = _snapshot(session, _catalog(schema)).targets[0]
    payload = {
        "settings": {"enabled": True},
        "model_policy": {"fixed": {"profile_id": "reviewer-model-profile"}},
    }
    result = DefaultspackHTTPPresentation().normalize_payload(
        target,
        payload,
        session=session,
        workspace_binding_resolver=None,
    )
    assert result["profile_id"] == "profile"
    assert result["model_policy"] == payload["model_policy"]
    with pytest.raises(ValueError):
        DefaultspackHTTPPresentation().normalize_payload(
            target,
            {**payload, "profile_id": "execution-override"},
            session=session,
            workspace_binding_resolver=None,
        )
    with pytest.raises(ValueError):
        DefaultspackHTTPPresentation().normalize_payload(
            target,
            {"settings": {"enabled": True, "profile_id": "forged"}},
            session=session,
            workspace_binding_resolver=None,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"settings": {"enabled": "true"}},
        {"settings": {"enabled": True, "undeclared": 1}},
        {"settings": {"enabled": True, "approved": True}},
        {"profile_id": "another", "settings": {"enabled": True}},
        {"settings": {"enabled": True, "_session_id": "forged"}},
        {"settings": {"enabled": True, "constructor": {}}},
        {},
    ],
)
def test_nested_forged_and_invalid_input_is_rejected(payload: Mapping[str, object]) -> None:
    session = _Session()
    target = _snapshot(session).targets[0]
    with pytest.raises(ValueError):
        DefaultspackHTTPPresentation().normalize_payload(
            target,
            payload,
            session=session,
            workspace_binding_resolver=None,
        )


@pytest.mark.parametrize("change", ["disabled", "unapproved", "unready", "digest", "collision"])
def test_disabled_unapproved_unready_changed_or_ambiguous_operations_are_removed(
    change: str,
) -> None:
    catalog = _catalog()
    session = _Session(ready=change != "unready")
    pack = catalog["packs"][0]
    if change == "disabled":
        pack["enabled"] = False
    if change == "unapproved":
        pack["approved"] = False
    if change == "digest":
        pack["artifact_digest"] = "changed"
    if change == "collision":
        pack["operations"].append({**pack["operations"][0], "contract_id": "other.v1"})
    assert _snapshot(session, catalog).targets == ()


def test_schema_digest_changes_the_catalog_and_external_refs_are_not_admitted() -> None:
    first = _snapshot(_Session())
    second = _snapshot(
        _Session(),
        _catalog(
            {
                **SCHEMA,
                "required": ["settings"],
            }
        ),
    )
    assert first.catalog_hash != second.catalog_hash
    assert (
        _snapshot(
            _Session(),
            _catalog(
                {
                    **SCHEMA,
                    "$ref": "https://example.test/schema.json",
                }
            ),
        ).targets
        == ()
    )
    assert (
        _snapshot(
            _Session(),
            _catalog(
                {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {},
                }
            ),
        ).targets
        == ()
    )


@pytest.mark.parametrize(
    "change",
    [
        {"profile_id": "other"},
        {"profile_revision": "other"},
        {"activation_id": "other"},
        {"plan_hash": "other"},
        {"catalog_hash": "other"},
        {"owner_pack_id": "surface"},
        {"contract_id": "other.v1"},
        {"contribution_id": "unregistered"},
        {"expires_at": 0},
        {"expires_at": float("inf")},
        {"approved": True},
    ],
)
def test_capability_envelope_keeps_exact_capture_expiry_and_owner_binding(
    change: dict[str, Any],
) -> None:
    session = _Session()
    snapshot = _snapshot(session)
    body = {**_body(snapshot), **change}
    assert (
        DefaultspackHTTPPresentation().decode_request(
            _binding(),
            body=body,
            query={},
            session=session,
            snapshot=snapshot,
        )
        is None
    )


def test_stale_capture_denies_before_dispatch() -> None:
    session = _Session(current=False)
    snapshot = _snapshot(session)
    assert (
        DefaultspackHTTPPresentation().decode_request(
            _binding(),
            body=_body(snapshot),
            query={},
            session=session,
            snapshot=snapshot,
        )
        is None
    )


def _view() -> dict[str, Any]:
    return {
        "version": "tobkiri.ui.view.v1",
        "slot": "sidebar",
        "renderer": "panel",
        "title": "Independent view",
        "data_source": {
            "contribution_id": "pack.qa.logic.settings.update",
            "contract_id": "qa.logic.v1",
            "operation_id": "settings.update",
            "input": {"settings": {"enabled": True}},
        },
    }


def _thread_view() -> dict[str, Any]:
    view = _view()
    operation = {key: value for key, value in view["data_source"].items() if key != "input"}
    return {
        **view,
        "renderer": "conversation_thread",
        "conversation_thread": {
            "conversation_path": "thread.conversation",
            "messages_path": "thread.messages",
            "pending_turn_path": "thread.pending_turn",
            "model_reference_path": "thread.context.model_reference",
            "send": {
                "operation": operation,
                "content_key": "content",
                "turn_id_key": "turn_id",
                "source_bindings": {"expected_child_revision": "revision"},
            },
            "events": {"operation": operation, "turn_id_key": "turn_id"},
        },
    }


def test_thread_view_grammar_preserves_canonical_operations_and_fixed_keys() -> None:
    """A descriptor supplies inert mappings, not model/turn execution authority."""
    validate_catalog_view(_thread_view())


@pytest.mark.parametrize("scenario", [
    "content_override", "turn_override", "scope", "url", "collision", "model",
    "events_content", "prototype", "source_missing",
])
def test_thread_view_rejects_injected_scope_and_unsupported_execution(
    scenario: str,
) -> None:
    """Thread controls cannot silently replace text, tickets, scope or models."""
    view = _thread_view()
    thread = view["conversation_thread"]
    send = thread["send"]
    if scenario == "content_override":
        send["content_key"] = "model_override"
    elif scenario == "turn_override":
        send["input"] = {"turn_id": "forged"}
    elif scenario == "scope":
        send["source_bindings"]["profile_id"] = "thread.id"
    elif scenario == "url":
        thread["url"] = "/api/send"
    elif scenario == "collision":
        send["context_bindings"] = {"content": "conversation_id"}
    elif scenario == "model":
        thread["model_override"] = "another-model"
    elif scenario == "events_content":
        thread["events"]["content_key"] = "content"
    elif scenario == "prototype":
        thread["messages_path"] = "constructor.messages"
    else:
        del view["data_source"]
    with pytest.raises((ValueError, ValidationError)):
        validate_catalog_view(view)


@pytest.mark.parametrize("key", [
    "model", "provider_id", "system_prompt", "tool_selection", "thinking_level",
    "strategy_reference", "approval_mode", "permissions", "grants", "workspace_id",
    "model_policy",
])
def test_thread_view_rejects_constant_and_bound_execution_overrides(key: str) -> None:
    """Neutral threads inherit execution authority instead of selecting it."""
    view = _thread_view()
    for location in ("input", "source_bindings", "context_bindings"):
        request = view["conversation_thread"]["send"]
        original = request.get(location)
        request[location] = {key: "conversation_id" if location == "context_bindings"
                             else "thread.context.model_reference"}
        with pytest.raises(ValueError):
            validate_catalog_view(view)
        if original is None:
            del request[location]
        else:
            request[location] = original
    view["conversation_thread"]["send"]["input"] = {"nested": {key: "override"}}
    with pytest.raises(ValueError):
        validate_catalog_view(view)
    view = _thread_view()
    view["data_source"]["input"] = {key: "override"}
    with pytest.raises(ValueError):
        validate_catalog_view(view)


@pytest.mark.parametrize(
    "slot",
    [
        "workspace_tab",
        "sidebar",
        "settings",
        "chat_header",
        "composer_above",
        "composer_below",
    ],
)
def test_all_public_slots_validate_without_admitting_an_operation(slot: str) -> None:
    view = {**_view(), "slot": slot}
    validate_catalog_view(view)


@pytest.mark.parametrize(
    "input_value",
    [
        {"nested": {"approved": True}},
        {"profile_id": "forged"},
        {"nested": {"_session_id": "forged"}},
        {"count": float("nan")},
    ],
)
def test_view_inputs_cannot_claim_authority(input_value: object) -> None:
    with pytest.raises(ValueError):
        validate_public_input(input_value)


def test_selected_signed_view_is_projected_and_unregister_has_no_stale_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / PACK_ID
    scaffold_pack(root, pack_id=PACK_ID, display_name="Public view fixture")
    (root / DESCRIPTOR).parent.mkdir(parents=True)
    payload = {
        "version": "rumi.ui.contribution.v1",
        "id": "qa.frontend.route.view",
        "kind": "view",
        "mode": "declarative",
        "label": "Independent view",
        "priority": 0,
        "view": _view(),
        "accessibility": {"name": "Independent view", "keyboard": True},
    }
    _write(root / DESCRIPTOR, payload)
    refresh_scaffold_artifacts(root)
    manifest = json.loads((root / "pack.v4.json").read_text())
    next(item for item in manifest["artifacts"] if item["path"] == DESCRIPTOR)["kind"] = (
        "ui.contribution"
    )
    digest = canonical_digest(manifest["artifacts"])
    manifest["pack"]["artifact_digest"] = digest
    manifest["integrity"]["artifact_set_digest"] = digest
    _write(root / "pack.v4.json", manifest)
    index = json.loads((root / "artifact-index.v4.json").read_text())
    index["artifact_set_digest"] = digest
    next(item for item in index["artifacts"] if item["path"] == "pack.v4.json")["digest"] = _digest(
        root / "pack.v4.json"
    )
    index["integrity_seal"]["signed_digest"] = canonical_digest(
        {key: value for key, value in index.items() if key != "integrity_seal"}
    )
    _write(root / "artifact-index.v4.json", index)
    digest = manifest["pack"]["artifact_digest"]
    _admit_fixture(monkeypatch, root)
    projected, diagnostics, quarantined = _project(digest)
    assert diagnostics == []
    assert quarantined == []
    assert projected[0]["kind"] == "view"
    assert projected[0]["view"] == _view()
    assert "action_contract" not in projected[0]
    from ecosystem.defaultspack.defaultspack.v4_frontend_contributions import (
        project_selected_declarative_routes,
    )

    assert project_selected_declarative_routes(
        [],
        [],
        profile_id="profile",
        profile_revision="revision",
        activation_id="activation",
        plan_digest="plan",
    ) == ([], [], [])
