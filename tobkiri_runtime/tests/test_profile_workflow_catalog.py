"""Selected Workflow source binding; these fixtures grant no real authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core_runtime.profile_content_projection as projections
from core_runtime.profile_workflow_catalog import (
    compile_selected_workflow,
    selected_workflow_definitions,
)
from core_runtime.resolved_profile_scope import (
    V4ResolvedProfileView,
    activate_resolved_profile,
    restore_resolved_profile,
)
from core_runtime.workflow_v4.engine import WorkflowEngineV4
from core_runtime.workflow_v4.models import WorkflowValidationError
from core_runtime.workflow_v4.store import WorkflowStoreV4
from tests.test_workflow_v4 import Authority, Catalog, Invoker, Validator, definition


def _selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(projections, "RUNTIME_ROOT", tmp_path)
    root = tmp_path / "profile_projections/example/workflows"
    root.mkdir(parents=True)
    (root / "example.workflow.v4.json").write_text(json.dumps(definition()))
    resolved, _files = projections.resolve_intent_projection(
        {
            "projection_id": "example.source",
            "kind": "profile_content",
            "artifact_root": "profile_projections/example",
            "content_digest": None,
        }
    )
    return resolved


def _view(*selections: dict) -> V4ResolvedProfileView:
    return V4ResolvedProfileView(
        "example", "sha256:" + "1" * 64, "sha256:" + "2" * 64, (), (), (), selections
    )


def test_selected_workflow_uses_real_compiler_without_authority_or_store_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = _selection(tmp_path, monkeypatch)
    token = activate_resolved_profile(_view(selection))
    store = WorkflowStoreV4(tmp_path / "workflow.sqlite3")
    authority = Authority()
    invoker = Invoker()
    catalog = Catalog()
    engine = WorkflowEngineV4(
        store=store, catalog=catalog, authority=authority, invoker=invoker, validator=Validator()
    )
    try:
        assert selected_workflow_definitions()[0]["definition_id"] == "example"
        compiled = compile_selected_workflow(engine, "example")
        assert compiled["steps"][0]["contract_request"] == definition()["steps"][0]["request"]
        assert store.list_definitions() == []
        assert authority.reservations == {} and authority.commit_count == 0
        assert invoker.requests == []
        catalog.value["operations"] = []
        with pytest.raises(WorkflowValidationError, match="unavailable"):
            compile_selected_workflow(engine, "example")
        catalog.value = Catalog().value
        catalog.value["operations"].append(catalog.value["operations"][0])
        with pytest.raises(WorkflowValidationError, match="duplicate"):
            compile_selected_workflow(engine, "example")
    finally:
        store.close()
        restore_resolved_profile(token)


def test_unselected_workflow_is_not_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _selection(tmp_path, monkeypatch)
    token = activate_resolved_profile(_view())
    try:
        assert selected_workflow_definitions() == ()
        # The engine is never touched for an unselected ID.
        with pytest.raises(ValueError, match="outside"):
            compile_selected_workflow(None, "example")  # type: ignore[arg-type]
    finally:
        restore_resolved_profile(token)


def test_selected_workflow_mutation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = _selection(tmp_path, monkeypatch)
    token = activate_resolved_profile(_view(selection))
    try:
        path = tmp_path / selection["artifact_root"] / "workflows/example.workflow.v4.json"
        path.write_text(json.dumps({**definition(), "name": "Changed"}))
        with pytest.raises(projections.ProfileContentProjectionError, match="stale"):
            selected_workflow_definitions()
    finally:
        restore_resolved_profile(token)


def test_duplicate_workflow_ids_across_selected_origins_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _selection(tmp_path, monkeypatch)
    root = tmp_path / "profile_projections/other/workflows"
    root.mkdir(parents=True)
    (root / "example.workflow.v4.json").write_text(json.dumps(definition()))
    second, _ = projections.resolve_intent_projection(
        {
            "projection_id": "other.source",
            "kind": "profile_content",
            "artifact_root": "profile_projections/other",
            "content_digest": None,
        }
    )
    token = activate_resolved_profile(_view(first, second))
    try:
        with pytest.raises(ValueError, match="duplicate"):
            selected_workflow_definitions()
    finally:
        restore_resolved_profile(token)


@pytest.mark.parametrize("function_id", ["example.echo", "unknown.function"])
def test_source_intent_binds_exact_function_from_actual_engine_palette(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, function_id: str
) -> None:
    selection = _selection(tmp_path, monkeypatch)
    root = tmp_path / selection["artifact_root"] / "workflows"
    (root / "example.workflow.v4.json").unlink()
    document = definition()
    document["workflow_intent_api_version"] = "io.tobkiri.profile-workflow-intent.v1"
    del document["workflow_api_version"]
    request = document["steps"][0]["request"]
    del request["function_principal_id"]
    request["function_id"] = function_id
    (root / "example.workflow.intent.v1.json").write_text(json.dumps(document))
    selection, _ = projections.resolve_intent_projection(
        {
            "projection_id": "example.source",
            "kind": "profile_content",
            "artifact_root": "profile_projections/example",
            "content_digest": None,
        }
    )
    token = activate_resolved_profile(_view(selection))
    store = WorkflowStoreV4(tmp_path / "workflow.sqlite3")
    authority = Authority()
    invoker = Invoker()
    engine = WorkflowEngineV4(
        store=store, catalog=Catalog(), authority=authority, invoker=invoker, validator=Validator()
    )
    try:
        if function_id == "example.echo":
            compiled = compile_selected_workflow(engine, "example")
            assert (
                compiled["steps"][0]["contract_request"]["function_principal_id"]
                == "example.echo.provider"
            )
        else:
            with pytest.raises(ValueError, match="unique exact"):
                compile_selected_workflow(engine, "example")
        assert store.list_definitions() == [] and not authority.reservations
        assert authority.commit_count == 0 and not invoker.requests
    finally:
        store.close()
        restore_resolved_profile(token)
