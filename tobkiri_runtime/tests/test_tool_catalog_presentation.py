"""Tool UI data must not advertise execution from definition registration alone."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from ecosystem.defaultspack.defaultspack.tool_catalog_presentation import present_tool_catalog


class Session:
    """Exact metadata/readiness fixture; no invocation capability is provided."""

    profile_id = "defaults"

    def __init__(self) -> None:
        self.metadata = (
            {"provider_instance_id": "local.provider", "operation_id": "local.operation"},
        )
        self.ready = True
        self.stale = False
        self.checks = 0

    def assert_current(self) -> None:
        if self.stale:
            raise PermissionError("stale captured session")

    def provider_metadata(self, contract: str) -> tuple[dict[str, str], ...]:
        assert contract == "local.contract"
        return self.metadata

    def assert_operation_ready(self, contract: str, operation: str) -> None:
        assert (contract, operation) == ("local.contract", "local.operation")
        self.checks += 1
        if not self.ready:
            raise RuntimeError("backend unavailable")


def _catalog() -> dict[str, Any]:
    return {
        "profile_id": "defaults",
        "revision": 3,
        "definitions": [
            {
                "tool_id": "coding_file_read",
                "display_name": "Read file",
                "description": "Read a workspace file",
                "input_schema": {"type": "object"},
                "execution": {
                    "kind": "local",
                    "contract_id": "local.contract",
                    "operation": "local.operation",
                    "provider_instance_id": "local.provider",
                },
                "authority": "file.read",
                "risk": "low",
                "policy_tags": ["file"],
                "widget": {},
                "private": "must-not-appear",
            }
        ],
    }


def test_catalog_reuses_ui_classification_without_disclosing_owner_records() -> None:
    source = _catalog()
    before = deepcopy(source)
    result = present_tool_catalog(source, session=Session())
    assert result["count"] == 1 and result["registry_revision"] == 3
    tool = result["tools"][0]
    assert tool["service_id"] == "coding"
    assert tool["action_class"] == "read"
    assert tool["connection_status"] == "connected"
    assert "must-not-appear" not in json.dumps(result)
    assert "execution" not in tool and "authority" not in tool
    assert source == before


@pytest.mark.parametrize(
    "state", ["absent", "wrong_provider", "wrong_operation", "ambiguous", "unready", "remote"]
)
def test_descriptor_remains_visible_without_claiming_connection(state: str) -> None:
    session = Session()
    source = _catalog()
    if state == "absent":
        session.metadata = ()
    elif state == "wrong_provider":
        session.metadata = ({"provider_instance_id": "foreign", "operation_id": "local.operation"},)
    elif state == "wrong_operation":
        session.metadata = ({"provider_instance_id": "local.provider", "operation_id": "foreign"},)
    elif state == "ambiguous":
        session.metadata *= 2
    elif state == "unready":
        session.ready = False
    else:
        source["definitions"][0]["execution"]["kind"] = "mcp"
    result = present_tool_catalog(source, session=session)
    assert result["tools"][0]["connection_status"] == "unavailable"
    assert result["services"][0]["connection_status"] == "unavailable"


def test_captured_session_expiring_during_readiness_cannot_publish_catalog() -> None:
    session = Session()

    def expire(_contract: str, _operation: str) -> None:
        session.stale = True
        raise RuntimeError("expired")

    session.assert_operation_ready = expire
    with pytest.raises(PermissionError, match="stale"):
        present_tool_catalog(_catalog(), session=session)


@pytest.mark.parametrize(
    "change",
    [
        {"profile_id": "foreign"},
        {"revision": True},
        {"revision": -1},
        {"definitions": {}},
        {"definitions": [None]},
    ],
)
def test_invalid_owner_result_is_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        present_tool_catalog({**_catalog(), **change}, session=Session())


def test_ui_projection_imports_no_runtime_registry_executor_or_policy() -> None:
    runtime = str(Path(__file__).resolve().parents[1])
    script = f"""
import sys
sys.path.insert(0, {runtime!r})
from ecosystem.defaultspack.domain.tool.definition_projection import project_tool_definition
from ecosystem.defaultspack.domain.tool.service_catalog import ToolServiceCatalog
assert not any(name.endswith(('.tool.registry', '.tool.executor', '.tool.security', '.tool.schema_adapter', '.catalog_contract_client')) for name in sys.modules)
"""
    subprocess.run([sys.executable, "-I", "-S", "-c", script], check=True, timeout=5)
