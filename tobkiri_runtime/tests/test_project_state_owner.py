"""Canonical Project owner and signed presentation boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.global_contracts.http_contract_dispatch import HTTPContractTarget
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
    _PROJECT_READ_TARGET,
    _PROJECT_WRITE_TARGET,
)
from ecosystem.tobkiri_ui_settings_pack.runtime.projects import ProjectStateStore
from ecosystem.tobkiri_ui_settings_pack.runtime.projects import (
    ProjectStateHostFactoryV4,
    READ_CONTRACT_ID,
    READ_FUNCTION_ID,
    READ_OPERATION_ID,
    WRITE_CONTRACT_ID,
    WRITE_FUNCTION_ID,
    WRITE_OPERATION_ID,
)


def _project(identifier: str = "group-one") -> dict[str, object]:
    return {
        "id": identifier,
        "title": "One",
        "workspace_id": "workspace-one",
        "workspace_label": "Workspace One",
        "workspace_root": "/repo/one",
        "rumi_data_path": "/repo/one/.rumiDP",
    }


def test_project_owner_isolated_by_captured_profile_and_caller(tmp_path: Path) -> None:
    """State namespaces cannot be selected through a client payload."""
    first = ProjectStateStore(tmp_path, "defaults", "shell.one")
    second = ProjectStateStore(tmp_path, "defaults", "shell.two")
    other_profile = ProjectStateStore(tmp_path, "other", "shell.one")

    acknowledgement = first.replace(
        projects=[_project()], expected_revision=0, mutation_id="mutation-one",
        migration_digest=None,
    )

    assert acknowledgement["revision"] == 1
    assert acknowledgement["receipt"].startswith("sha256:")
    assert first.read()["projects"] == [_project()]
    assert second.read()["projects"] == []
    assert other_profile.read()["projects"] == []


def test_project_owner_cas_and_idempotency_fail_closed(tmp_path: Path) -> None:
    """A retry is exact while stale or conflicting writes are rejected."""
    store = ProjectStateStore(tmp_path, "defaults", "shell.one")
    first = store.replace(
        projects=[_project()], expected_revision=0, mutation_id="mutation-one",
        migration_digest="sha256:" + "1" * 64,
    )
    assert store.replace(
        projects=[_project()], expected_revision=0, mutation_id="mutation-one",
        migration_digest="sha256:" + "1" * 64,
    ) == first
    with pytest.raises(ValueError, match="reused"):
        store.replace(
            projects=[_project("group-two")], expected_revision=0,
            mutation_id="mutation-one", migration_digest="sha256:" + "1" * 64,
        )
    with pytest.raises(ValueError, match="revision conflict"):
        store.replace(
            projects=[], expected_revision=0, mutation_id="mutation-two",
            migration_digest=None,
        )


def test_project_http_presentation_injects_profile_and_rejects_identity() -> None:
    """The browser supplies Project values, never owner identity or paths."""
    presentation = DefaultspackHTTPPresentation()
    session = SimpleNamespace(profile_id="defaults", assert_current=lambda: None)
    assert presentation.normalize_payload(
        HTTPContractTarget(*_PROJECT_READ_TARGET), {}, session=session,
        workspace_binding_resolver=None,
    ) == {"profile_id": "defaults"}
    payload = {
        "projects": [], "expected_revision": 0, "mutation_id": "mutation-one",
    }
    assert presentation.normalize_payload(
        HTTPContractTarget(*_PROJECT_WRITE_TARGET), payload, session=session,
        workspace_binding_resolver=None,
    ) == {**payload, "profile_id": "defaults"}
    for forbidden in ({"profile_id": "other"}, {"path": "/tmp/owner.json"}, {"approved": True}):
        with pytest.raises(ValueError):
            presentation.normalize_payload(
                HTTPContractTarget(*_PROJECT_WRITE_TARGET),
                {**payload, **forbidden}, session=session,
                workspace_binding_resolver=None,
            )


def _capture(root: Path, *, write: bool) -> SimpleNamespace:
    contract_id, operation_id, function_id = (
        (WRITE_CONTRACT_ID, WRITE_OPERATION_ID, WRITE_FUNCTION_ID)
        if write else (READ_CONTRACT_ID, READ_OPERATION_ID, READ_FUNCTION_ID)
    )
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=function_id, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=contract_id, operation_id=operation_id,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="project-owner"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    return SimpleNamespace(
        profile_id="defaults", user_data_root=root, provider_bindings=(binding,),
        domain_ids={(contract_id, operation_id, "project-owner"): "domain"},
    )


class _Invocation:
    envelope = SimpleNamespace()
    presentation_owner_principal_id = "shell.tauri.default"
    presentation_owner_session_id = "session-one"

    def assert_current(self) -> None:
        """Represent a current Host-authenticated call."""


def test_project_provider_uses_host_caller_and_rejects_client_authority(tmp_path: Path) -> None:
    """Caller/session/path/approval cannot be selected by browser JSON."""
    writer = ProjectStateHostFactoryV4(WRITE_FUNCTION_ID).capture(
        _capture(tmp_path, write=True)
    ).contributions[0]
    payload = {
        "profile_id": "defaults", "projects": [_project()],
        "expected_revision": 0, "mutation_id": "mutation-one",
    }
    result = writer.invoke(WRITE_OPERATION_ID, payload, _Invocation())
    assert result["revision"] == 1
    assert len(result["caller_session_digest"]) == 64
    for forbidden in ({"owner": "other"}, {"path": "/tmp/state"}, {"approved": True}):
        with pytest.raises(PermissionError, match="request is invalid"):
            writer.invoke(
                WRITE_OPERATION_ID,
                {**payload, "expected_revision": 1, "mutation_id": "next", **forbidden},
                _Invocation(),
            )


def _multiroot() -> dict[str, object]:
    project = _project()
    project["workspace_bindings"] = [
        {
            "workspace_id": "workspace-one",
            "workspace_label": "Workspace One",
            "workspace_root": "/repo/one",
        },
        {
            "workspace_id": "workspace-two",
            "workspace_label": "Workspace Two",
            "workspace_root": "/repo/two",
        },
    ]
    return project


def test_multiple_workspaces_survive_owner_reload_and_exact_retry(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path, "defaults", "shell.one")
    request = dict(
        projects=[_multiroot()],
        expected_revision=0,
        mutation_id="multi-one",
        migration_digest=None,
    )
    receipt = store.replace(**request)
    reloaded = ProjectStateStore(tmp_path, "defaults", "shell.one")
    assert reloaded.read()["projects"] == [_multiroot()]
    assert reloaded.replace(**request) == receipt
    assert reloaded.read()["revision"] == 1


@pytest.mark.parametrize(
    "variant",
    [
        "empty",
        "duplicate",
        "mismatch",
        "unknown",
        "relative",
        "dot",
        "null",
        "too_many",
    ],
)
def test_invalid_multiroot_does_not_mutate_owner(tmp_path: Path, variant: str) -> None:
    project = _multiroot()
    bindings = project["workspace_bindings"]
    if variant == "empty":
        bindings.clear()
    elif variant == "duplicate":
        bindings.append(dict(bindings[0]))
    elif variant == "mismatch":
        project["workspace_label"] = "Different"
    elif variant == "unknown":
        bindings[0]["trusted"] = True
    elif variant == "relative":
        bindings[0]["workspace_root"] = "relative"
    elif variant == "dot":
        bindings[0]["workspace_root"] = "/repo/../one"
    elif variant == "null":
        bindings[0]["workspace_id"] = None
    elif variant == "too_many":
        bindings.extend(dict(bindings[0]) for _ in range(31))
    store = ProjectStateStore(tmp_path, "defaults", "shell.one")
    with pytest.raises(ValueError):
        store.replace(
            projects=[project],
            expected_revision=0,
            mutation_id="bad-one",
            migration_digest=None,
        )
    assert store.read()["revision"] == 0
    assert store.read()["projects"] == []


@pytest.mark.parametrize(
    ("root", "valid"),
    [
        ("C:\\repos\\one", True),
        ("\\\\server\\share\\one", True),
        ("C:relative", False),
        ("\\\\server", False),
        ("\\\\server\\", False),
        ("\\\\server\\share\\..\\one", False),
    ],
)
def test_cross_platform_workspace_metadata(
    tmp_path: Path, root: str, valid: bool,
) -> None:
    """Absolute metadata roots never grant filesystem access."""
    project = _multiroot()
    project["workspace_root"] = root
    project["workspace_bindings"][0]["workspace_root"] = root
    store = ProjectStateStore(tmp_path, "defaults", "shell.one")
    request = {
        "projects": [project], "expected_revision": 0,
        "mutation_id": "platform-one", "migration_digest": None,
    }
    if valid:
        store.replace(**request)
        assert store.read()["projects"] == [project]
    else:
        with pytest.raises(ValueError):
            store.replace(**request)
        assert store.read()["revision"] == 0
