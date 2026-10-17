"""Contract tests for the signed Defaults workspace read surface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractTarget,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from ecosystem.defaultspack.defaultspack.workspace_presentation import (
    WORKSPACE_GET_TARGET,
    WORKSPACE_LIST_TARGET,
)


pytestmark = pytest.mark.contract

RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _target(identity: tuple[str, str, str, str, str]) -> HTTPContractTarget:
    return HTTPContractTarget(*identity)


def _binding(presentation: str) -> HTTPContractBinding:
    return HTTPContractBinding("GET", "/workspace", presentation, ())


def test_workspace_routes_and_shell_edge_are_exact() -> None:
    """The UI can only reach the workspace owner through signed exact routes."""

    frontend = json.loads((
        RUNTIME_ROOT
        / "ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json"
    ).read_text(encoding="utf-8"))
    routes = {(item["method"], item["path"]): item for item in frontend["routes"]}
    assert routes[("GET", "/api/coding/workspaces")] == {
        "method": "GET",
        "path": "/api/coding/workspaces",
        "presentation": "workspace_list",
        "targets": [{
            "contribution_id": WORKSPACE_LIST_TARGET[0],
            "contract_id": WORKSPACE_LIST_TARGET[1],
            "operation_id": WORKSPACE_LIST_TARGET[2],
            "provider_id": WORKSPACE_LIST_TARGET[3],
            "function_id": WORKSPACE_LIST_TARGET[4],
            "allowed_payload_keys": [],
        }],
    }
    assert routes[("GET", "/api/coding/workspaces/get")]["targets"] == [{
        "contribution_id": WORKSPACE_GET_TARGET[0],
        "contract_id": WORKSPACE_GET_TARGET[1],
        "operation_id": WORKSPACE_GET_TARGET[2],
        "provider_id": WORKSPACE_GET_TARGET[3],
        "function_id": WORKSPACE_GET_TARGET[4],
        "allowed_payload_keys": ["workspace_id"],
    }]

    intent = json.loads((
        RUNTIME_ROOT / "ecosystem/defaultspack/v4/defaults.profile.intent.v1.json"
    ).read_text(encoding="utf-8"))
    matches = [
        edge for edge in intent["requested_edges"]
        if edge["caller_function_id"] == "shell.tauri.default"
        and edge["target_provider_id"] == WORKSPACE_LIST_TARGET[3]
        and edge["contract_id"] == WORKSPACE_LIST_TARGET[1]
        and edge["operation_id"] == WORKSPACE_LIST_TARGET[2]
    ]
    assert len(matches) == 1
    assert matches[0]["authority_mode"] == "profile_grant"


def test_workspace_reads_are_bound_to_the_captured_profile() -> None:
    presentation = DefaultspackHTTPPresentation()
    session = SimpleNamespace(profile_id="defaults", assert_current=lambda: None)

    assert presentation.normalize_payload(
        _target(WORKSPACE_LIST_TARGET),
        {},
        session=session,
        workspace_binding_resolver=None,
    ) == {"profile_id": "defaults", "operation": "list"}
    assert presentation.normalize_payload(
        _target(WORKSPACE_GET_TARGET),
        {"workspace_id": "repo-1"},
        session=session,
        workspace_binding_resolver=None,
    ) == {
        "profile_id": "defaults",
        "operation": "get",
        "workspace_id": "repo-1",
    }
    for invalid in ("../escape", "/absolute", "", "repo/child"):
        with pytest.raises(ValueError, match="workspace identity"):
            presentation.normalize_payload(
                _target(WORKSPACE_GET_TARGET),
                {"workspace_id": invalid},
                session=session,
                workspace_binding_resolver=None,
            )


def test_workspace_owner_results_are_typed_before_reaching_the_ui() -> None:
    presentation = DefaultspackHTTPPresentation()
    mount = {
        "id": "repo-1",
        "root_path": "/workspace/repo",
        "metadata": {
            "label": "Repository",
            "trusted": True,
            "trust_granted_at": 17,
            "metadata": {"color": "blue"},
        },
        "updated_at": 19,
    }
    listed = presentation.present_result(
        _binding("workspace_list"),
        {
            "mounts": [mount],
            "selected_workspace_id": "repo-1",
            "revision": 3,
        },
        session=None,
        routes={},
        capability_snapshot=lambda *_args, **_kwargs: None,
    )
    assert listed == {
        "workspaces": [{
            "workspace_id": "repo-1",
            "label": "Repository",
            "root_path": "/workspace/repo",
            "trusted": True,
            "trust_granted_at": 17,
            "last_used_at": 19,
            "metadata": {"color": "blue"},
        }],
        "selected_workspace_id": "repo-1",
        "revision": 3,
    }
    assert presentation.present_result(
        _binding("workspace_record"),
        mount,
        session=None,
        routes={},
        capability_snapshot=lambda *_args, **_kwargs: None,
    ) == {"workspace": listed["workspaces"][0]}

    with pytest.raises(ValueError, match="invalid mount identity"):
        presentation.present_result(
            _binding("workspace_record"),
            {**mount, "root_path": "../escape"},
            session=None,
            routes={},
            capability_snapshot=lambda *_args, **_kwargs: None,
        )
