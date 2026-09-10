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
    assert calculator["execution"]["provider_instance_id"] == "rumi_default_tools_pack.calculator"
    assert calculator["execution"]["operation"] == "rumi_default_tools_pack.calculator-evaluate"
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


@pytest.mark.parametrize(
    "change",
    [
        {"contract_id": "foreign.contract.v1"}, {"provider_instance_id": ""},
        {"operation": "invalid operation"}, {"approved": True},
        {"provider_instance_id": None},
    ],
)
def test_local_descriptor_rejects_malformed_or_authority_fields(captured_data, change):
    source = captured_data[0]
    files = []
    for item in source.files:
        value = json.loads(item.content)
        if value["id"] == "calculator":
            value["config"]["execution"].update(change)
            content = json.dumps(value).encode()
            item = replace(item, content=content, digest="sha256:" + hashlib.sha256(content).hexdigest())
        files.append(item)
    with pytest.raises(ValueError, match="local operation declaration"):
        definitions_from_pack_data((replace(source, files=tuple(files)),))


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


def test_descriptor_aliases_and_disabled_entries_are_data(
    captured_data, registry_host, tmp_path
):
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
    data = (replace(source, files=tuple(files)),)
    definitions = definitions_from_pack_data(data)
    assert len(definitions) == 1
    assert definitions[0]["aliases"] == ["calc"]
    invoke, _client = registry_host
    with pytest.raises(ValueError, match="packaged tool names"):
        invoke(
            "manage",
            {"operation": "save", "definition": _definition("calc"), "expected_revision": 0},
            pack_data=data,
        )
    assert not (tmp_path / "packs").exists()


def test_all_registry_factories_capture_the_same_packaged_namespace():
    for factory in host.HOST_PROVIDER_FACTORY.values():
        assert {(item.pack_id, item.path_prefix) for item in factory.declared_pack_data} == {
            ("defaultspack", "tools/"),
            ("defaultspack", "extensions/tools/"),
            ("rumi_default_tools_pack", "tools/"),
        }


@pytest.mark.parametrize(
    "kind", ["save_id", "save_name", "save_alias", "alias", "migrate_id", "migrate_alias"]
)
def test_captured_pack_names_are_rejected_before_any_mutation(
    captured_data, registry_host, tmp_path, kind
):
    invoke, client = registry_host
    definition = _definition("calculator" if kind.endswith("id") else "sample.read")
    if kind == "save_name":
        definition.pop("tool_id")
        definition["name"] = " calculator "
    if kind == "save_alias":
        definition["aliases"] = ["calculator"]
    if kind == "alias":
        payload = {
            "operation": "alias", "alias": "calculator",
            "target_tool_id": "sample.read", "expected_revision": 0,
        }
    elif kind.startswith("migrate"):
        source = {
            "definitions": [definition],
            "aliases": {"calculator": "sample.read"} if kind.endswith("alias") else {},
        }
        source_hash = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        payload = {"operation": "migrate", **source, "expected_source_hash": source_hash}
    else:
        payload = {"operation": "save", "definition": definition, "expected_revision": 0}
    with pytest.raises(ValueError, match="packaged tool names"):
        invoke(
            "migrate" if kind.startswith("migrate") else "manage",
            payload, pack_data=captured_data,
        )
    assert not (tmp_path / "packs").exists()
    assert client.calls == []
    listed = invoke("definition", {"operation": "list"}, pack_data=captured_data)
    assert listed["revision"] == 0 and len(listed["definitions"]) == 30


@pytest.fixture
def defaults_data() -> tuple[CapturedHostPackDataV4, ...]:
    """Capture both real Defaults descriptor sources through the sealed reader."""
    from tobkiri_host.artifact_materialization import capture_declared_pack_data

    root = Path(__file__).resolve().parents[1] / "ecosystem/defaultspack"
    digest = json.loads((root / "pack.v4.json").read_bytes())["pack"]["artifact_digest"]
    return tuple(
        CapturedHostPackDataV4(
            "defaultspack", digest, prefix,
            capture_declared_pack_data(
                root, pack_id="defaultspack", artifact_digest=digest, path_prefix=prefix,
            ),
        )
        for prefix in ("tools/", "extensions/tools/")
    )


def test_defaults_own_descriptors_join_the_selected_catalog(
    captured_data, defaults_data, registry_host,
):
    """Defaults' real files preserve their IDs, display labels and schemas."""
    source, extensions = defaults_data
    assert len(source.files) == 117
    assert len(extensions.files) == 2
    invoke, _client = registry_host
    data = (*captured_data, *defaults_data)
    result = invoke("definition", {"operation": "list"}, pack_data=data)
    assert len(result["definitions"]) == 149
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
    assert len(result["pack_data_sources"]) == 2
    definitions = {item["tool_id"]: item for item in result["definitions"]}
    assert definitions["memo_note_upsert"]["input_schema"]["required"] == ["content"]
    assert definitions["settings_update"]["authority"] == "service.mutate"
    assert definitions["settings_inspect"]["widget"]["group_id"] == "settings"
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


@pytest.mark.parametrize("change", ["path", "category", "pack", "digest"])
def test_extension_sources_cannot_change_the_captured_namespace(defaults_data, change):
    source, extensions = defaults_data
    item = extensions.files[0]
    if change == "path":
        changed = replace(item, path="tools/settings_inspect/manifest.json")
        extensions = replace(extensions, files=(changed,))
    elif change == "category":
        raw = json.loads(item.content)
        raw["category"] = "skill"
        content = json.dumps(raw).encode()
        changed = replace(
            item, content=content,
            digest="sha256:" + hashlib.sha256(content).hexdigest(),
        )
        extensions = replace(extensions, files=(changed,))
    elif change == "pack":
        extensions = replace(extensions, pack_id="rumi_default_tools_pack")
    else:
        extensions = replace(extensions, artifact_digest="sha256:" + "f" * 64)
    with pytest.raises(ValueError):
        definitions_from_pack_data((source, extensions))
