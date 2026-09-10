"""The registry combines real sealed descriptors with its explicit data owners."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from core_runtime.host_provider_backend_v4 import CapturedHostPackDataV4
from ecosystem.rumi_tool_registry_pack.runtime import host
from ecosystem.rumi_tool_registry_pack.runtime.pack_data import definitions_from_pack_data
from tests.test_declared_pack_data import PACK_ID, _capture, data_pack as data_pack
from tests.test_pack_architecture_wave6 import _definition
from tests.test_tool_registry_host import registry_host as registry_host


@pytest.fixture
def captured_data(data_pack: tuple[Path, str]) -> tuple[CapturedHostPackDataV4, ...]:
    root, digest = data_pack
    return (CapturedHostPackDataV4(PACK_ID, digest, "tools/", _capture(root, digest)),)


def test_owner_lists_resolves_and_keeps_packaged_schemas_isolated(
    captured_data: tuple[CapturedHostPackDataV4, ...],
    registry_host,
    tmp_path: Path,
) -> None:
    invoke, client = registry_host
    result = invoke("definition", {"operation": "list"}, pack_data=captured_data)
    assert len(result["definitions"]) == 30
    assert result["pack_data_sources"] == [
        {
            "pack_id": PACK_ID,
            "artifact_digest": captured_data[0].artifact_digest,
        }
    ]
    assert client.calls == []
    assert not (tmp_path / "packs").exists()
    resolve = {"operation": "resolve", "tool_id": "calculator"}
    calculator = invoke("definition", resolve, pack_data=captured_data)["definition"]
    assert calculator["input_schema"]["required"] == ["expression"]
    assert calculator["widget"]["group_icon"] == "calculator"
    assert calculator["execution"]["contract_id"] == "tobkiri.service.tool.local.operation.v1"
    calculator["input_schema"]["properties"].clear()
    assert (
        "expression"
        in invoke("definition", resolve, pack_data=captured_data)["definition"]["input_schema"][
            "properties"
        ]
    )
    assert invoke("definition", resolve) == {"found": False}


def test_stored_and_selected_contributions_remain_visible(captured_data, registry_host):
    invoke, client = registry_host
    invoke("manage", {"operation": "save", "definition": _definition(), "expected_revision": 0})
    client.items = [
        (
            {"provider_instance_id": "selected.component", "operation_id": "component.list"},
            {
                "definitions": [_definition("component.read")],
                "aliases": {"component.alias": "component.read"},
            },
        )
    ]
    result = invoke("definition", {"operation": "list"}, pack_data=captured_data)
    assert len(result["definitions"]) == 32
    assert result["aliases"]["component.alias"] == "component.read"
    assert result["revision"] == 1
    assert client.calls[0][1] == "component.list"


@pytest.mark.parametrize("source", ["stored", "provider", "alias"])
def test_packaged_definition_cannot_silently_shadow_an_owner(captured_data, registry_host, source):
    invoke, client = registry_host
    if source == "stored":
        invoke(
            "manage",
            {"operation": "save", "definition": _definition("calculator"), "expected_revision": 0},
        )
    elif source == "alias":
        invoke("manage", {"operation": "save", "definition": _definition(), "expected_revision": 0})
        invoke(
            "manage",
            {
                "operation": "alias",
                "alias": "calculator",
                "target_tool_id": "sample.read",
                "expected_revision": 1,
            },
        )
    else:
        client.items = [
            (
                {"provider_instance_id": "selected.component", "operation_id": "list"},
                {"definitions": [_definition("calculator")], "aliases": {}},
            )
        ]
    with pytest.raises(RuntimeError, match="collides"):
        invoke("definition", {"operation": "list"}, pack_data=captured_data)


@pytest.mark.parametrize(
    "change",
    [
        {"pack_id": "foreign"},
        {"path_prefix": "other/"},
        {"files": ()},
    ],
)
def test_registry_rejects_wrong_or_empty_captured_source(captured_data, change):
    with pytest.raises(ValueError):
        definitions_from_pack_data((replace(captured_data[0], **change),))


def test_descriptor_aliases_and_disabled_entries_are_data(captured_data):
    source = captured_data[0]
    files = []
    for item in source.files:
        raw = json.loads(item.content)
        if raw["id"] == "calculator":
            raw["config"]["aliases"] = ["calc"]
        else:
            raw["enabled"] = False
        content = json.dumps(raw).encode()
        files.append(
            replace(item, content=content, digest="sha256:" + hashlib.sha256(content).hexdigest())
        )
    definitions = definitions_from_pack_data((replace(source, files=tuple(files)),))
    assert len(definitions) == 1
    assert definitions[0]["aliases"] == ["calc"]


def test_only_read_factory_requests_pack_data():
    for function, factory in host.HOST_PROVIDER_FACTORY.items():
        assert bool(factory.declared_pack_data) == function.endswith(".definition")


def test_defaults_own_descriptors_join_the_selected_catalog(captured_data, registry_host):
    """Defaults' 109 real files preserve their IDs, display labels and schemas."""
    from tobkiri_host.artifact_materialization import capture_declared_pack_data

    root = Path(__file__).resolve().parents[1] / "ecosystem/defaultspack"
    digest = json.loads((root / "pack.v4.json").read_bytes())["pack"]["artifact_digest"]
    source = CapturedHostPackDataV4(
        "defaultspack", digest, "tools/",
        capture_declared_pack_data(
            root, pack_id="defaultspack", artifact_digest=digest, path_prefix="tools/",
        ),
    )
    assert len(source.files) == 109
    invoke, _client = registry_host
    data = (*captured_data, source)
    result = invoke("definition", {"operation": "list"}, pack_data=data)
    assert len(result["definitions"]) == 139
    definition = invoke(
        "definition", {"operation": "resolve", "tool_id": "artifact_file_read"},
        pack_data=data,
    )["definition"]
    assert definition["display_name"] == "Artifact File Read"
    assert definition["source_adapter_id"] == "defaultspack"
    assert definition["input_schema"]["required"] == ["path"]
    assert definition["widget"]["group_id"] == "artifact"
    assert {item["pack_id"] for item in result["pack_data_sources"]} == {
        "defaultspack", "rumi_default_tools_pack",
    }
    with pytest.raises(ValueError, match="duplicated"):
        definitions_from_pack_data((source, source))

    item = next(item for item in source.files if "/artifact_file_read/" in item.path)
    raw = json.loads(item.content)
    raw["config"]["tool_id"] = "different.identity"
    content = json.dumps(raw).encode()
    changed = replace(
        item, content=content, digest="sha256:" + hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(ValueError, match="identity"):
        definitions_from_pack_data((replace(source, files=(changed,)),))
