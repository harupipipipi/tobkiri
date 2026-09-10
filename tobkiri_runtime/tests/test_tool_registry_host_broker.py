"""Real production capture and Broker with a selected declarative tool Pack.

This isolated Profile proves the owner edge, not ordinary Defaults or native UI.
"""

from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from tests.conformance_support.host_profile import captured_host_profile
from tests.test_mcp_host_broker import _edge
from tobkiri_host.errors import ProviderExecutionError


@pytest.mark.parametrize("selected", [True, False])
def test_production_registry_uses_selected_data_without_executable_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: bool,
) -> None:
    contract = "tobkiri.resource.tool.definition.v1"
    operation = "rumi_tool_registry_pack.tool-definition-resource"
    with captured_host_profile(
        tmp_path,
        monkeypatch,
        # The Profile has one application entrypoint (Defaults). This selected
        # supporting Pack is declarative, regardless of its Profile role label.
        packs=("rumi_tool_registry_pack",) + (("rumi_default_tools_pack",) if selected else ()),
        edges=[
            _edge(
                "shell.tauri.default",
                "rumi_tool_registry_pack.tool-registry.definition",
                contract,
                operation,
            )
        ],
        backends=(),
    ) as (session, _store):
        result = session.invoke(
            contract,
            operation,
            {
                "operation": "list",
                "_session_id": "registry-data",
            },
        )
        assert len(result["definitions"]) == (30 if selected else 0)
        assert not (tmp_path / "user-data/packs/rumi_tool_registry_pack").exists()
        if selected:
            assert result["pack_data_sources"][0]["pack_id"] == "rumi_default_tools_pack"
        with pytest.raises(ProviderExecutionError):
            session.invoke(
                contract,
                operation,
                {
                    "operation": "list",
                    "pack_id": "foreign",
                    "_session_id": "registry-data",
                },
            )
        with pytest.raises(AuthorityDenied):
            session.invoke(
                "tobkiri.service.tool.local.operation.v1",
                "rumi_default_tool_projection_pack.default-tool-local-operation",
                {"_session_id": "registry-data"},
            )
