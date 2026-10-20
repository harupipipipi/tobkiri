"""Source/evidence checks only, not Python-on-Wasm or native Core acceptance."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.ai-gateway-tool-caller-python"
SCRIPT = ROOT / "scripts/wasm/prepare_gateway_tool_caller.py"
SPEC = importlib.util.spec_from_file_location("gateway_caller_extraction", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)
OUTPUT_NAMES = {"original_gateway_tool_caller.py", "inventory.json", "provenance.json"}


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pointer(value: object, pointer: str) -> object:
    for key in pointer.removeprefix("/").split("/"):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def test_checked_in_source_and_dependencies_match() -> None:
    """All original source/dependency identities agree with the current checkout."""
    result = prepare.verify(PACK / "python")
    assert result["build_performed"] is False
    assert result["production_enabled"] is False
    assert result["protocol"] == "ordered-finite-json-v2"
    assert {entry["path"] for entry in result["transport_dependencies"]} == {
        *prepare.CODEC_PATHS, prepare.CORE_WIT, prepare.BRIDGE_WIT,
    }
    for item in result["transport_dependencies"]:
        assert _digest((ROOT / item["path"]).read_bytes()) == item["sha256"]


def test_complete_lines_and_bytes_are_exact(tmp_path: Path) -> None:
    """Source line/byte/output offsets independently establish slice fidelity."""
    provenance = prepare.prepare(tmp_path)
    output = (tmp_path / "original_gateway_tool_caller.py").read_bytes()
    assert _digest(output) == provenance["output"]["sha256"]
    assert len(output) == provenance["output"]["bytes"]
    source = {}
    for item in provenance["sources"]:
        content = (ROOT / item["path"]).read_bytes()
        assert len(content) == item["bytes"]
        assert _digest(content) == item["sha256"]
        source[item["path"]] = content
    assert {item["symbol"] for item in provenance["slices"]} == {
        *prepare.SYMBOLS, "GlobalContractInvocationError",
        "import:__future__", "import:typing",
    }
    for item in provenance["slices"]:
        content = source[item["source_path"]]
        selected = content[item["source_start_byte"]:item["source_end_byte"]]
        assert selected == b"".join(content.splitlines(keepends=True)[
            item["source_start_line"] - 1:item["source_end_line"]
        ])
        assert selected == output[item["output_start_byte"]:item["output_end_byte"]]
        assert selected == b"".join(output.splitlines(keepends=True)[
            item["output_start_line"] - 1:item["output_end_line"]
        ])
        assert _digest(selected) == item["sha256"]
    assert _digest((tmp_path / "inventory.json").read_bytes()) == (
        provenance["inventory"]["sha256"]
    )


def test_extraction_has_no_host_authority_or_normalizer() -> None:
    """Only the original error, four constants and two callers are executable."""
    tree = ast.parse((PACK / "python/original_gateway_tool_caller.py").read_bytes())
    assert [(node.module, [item.name for item in node.names])
            for node in tree.body if isinstance(node, ast.ImportFrom)] == [
        ("__future__", ["annotations"]), ("typing", ["Any", "Iterable", "Mapping"]),
    ]
    assert [node.name for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))] == [
        "GlobalContractInvocationError", "_attach_stream_tool_intents", "_tool_intents",
    ]
    assert [node.targets[0].id for node in tree.body
            if isinstance(node, ast.Assign)] == list(prepare.SYMBOLS[:4])
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in {"exec", "eval", "compile", "__import__"}
                   for node in ast.walk(tree))


def test_inventory_keeps_four_edges_not_four_exports() -> None:
    """Evidence resolves back to exact manifest entries and omitted wrappers."""
    inventory = json.loads((PACK / "python/inventory.json").read_bytes())
    expected = {
        (f"rumi_ai_gateway_pack.ai-gateway.{name}", f"{prepare.PREFIX}.{operation}")
        for name, operation in (("generate", "generate"), ("stream", "stream"),
                                ("text-generate", "generate"),
                                ("messages-generate", "generate"))
    }
    assert {(item["caller_function_id"], item["operation_id"])
            for item in inventory["caller_edges"]} == expected
    assert inventory["exported_helper_operations"] == [
        "generate-intents", "stream-intents", "stream-event-intents",
    ]
    assert "_invoke" in inventory["not_migrated"]
    sources = {}
    for item in inventory["sources"]:
        content = (ROOT / item["path"]).read_bytes()
        assert _digest(content) == item["sha256"]
        assert len(content) == item["bytes"]
        sources[item["path"]] = content
    for item in inventory["operations"] + inventory["caller_edges"]:
        for evidence in item["declarations"]:
            entry = _pointer(json.loads(sources[evidence["path"]]),
                             evidence["json_pointer"])
            assert entry["operation_id"] == item["operation_id"]
            if "caller_function_id" in item:
                assert entry["caller_function_id"] == item["caller_function_id"]
                assert entry["target_provider_id"] == item["target_provider_id"]
    evidence = (
        inventory["gateway_evidence"] + inventory["gateway_tool_intent_call_sites"]
    )
    for wrapper in inventory["wrapper_evidence"]:
        assert wrapper["migrated"] is False
        assert [item["symbol"] for item in wrapper["gateway_delegation_calls"]] == [
            "gateway.create_generate_operation", "generate",
        ]
        evidence += wrapper["definitions"] + wrapper["gateway_delegation_calls"]
    for item in evidence:
        assert _digest(sources[item["source_path"]][
            item["source_start_byte"]:item["source_end_byte"]
        ]) == item["sha256"]
    assert [item["streaming_expression"]
            for item in inventory["gateway_tool_intent_call_sites"]] == [
        "True", "streaming", "True",
    ]


def test_required_import_and_own_helper_exports_only() -> None:
    """The source app is linked to generated bridge bindings, not an injected client."""
    wit = (PACK / "wit/pack.wit").read_text()
    assert "include tobkiri:core/pack-minimal@1.0.0;" in wit
    assert "import local:ai-tool-bridge-ordered-python/bridge@2.0.0;" in wit
    assert "export caller;" in wit
    assert wit.count("-> result<string, error>") == 3
    assert "runtime-info: func() -> string;" in wit
    adapter = ast.parse((PACK / "python/gateway_tool_caller_adapter.py").read_bytes())
    imports = [node for node in ast.walk(adapter)
               if isinstance(node, ast.ImportFrom)]
    assert any(node.module == "wit_world.imports" and
               [item.name for item in node.names] == ["bridge"] for node in imports)
    assert any(node.module == "ordered_json_v2" for node in imports)
    calls = [node for node in ast.walk(adapter) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "bridge"]
    assert len(calls) == 1 and calls[0].func.attr == "normalize"
    assert not any(node.module and any(fragment in node.module for fragment in (
        "http", "socket", "subprocess", "host_provider", "original_bridge",
        "core_runtime", "caller_adapter_v2",
    )) for node in imports)
    assert not (PACK / "ordered_json_v2.py").exists()
    assert not (PACK / "python/ordered_json_v2.py").exists()


def test_deterministic_preparation_and_readonly_verification(tmp_path: Path) -> None:
    """No original source file or existing verified result is changed."""
    before = (ROOT / prepare.GATEWAY).read_bytes()
    one, two = tmp_path / "one", tmp_path / "two"
    assert prepare.prepare(one) == prepare.prepare(two)
    assert prepare.verify(one) == prepare.verify(two)
    assert {item.name for item in one.iterdir()} == OUTPUT_NAMES
    for name in OUTPUT_NAMES:
        assert (one / name).read_bytes() == (two / name).read_bytes()
    assert (ROOT / prepare.GATEWAY).read_bytes() == before


@pytest.mark.parametrize("name", sorted(OUTPUT_NAMES))
def test_changed_extraction_rejected_without_rewrite(tmp_path: Path, name: str) -> None:
    prepare.prepare(tmp_path)
    target = tmp_path / name
    target.write_bytes(target.read_bytes() + b"\n")
    changed = target.read_bytes()
    with pytest.raises(ValueError, match=name):
        prepare.verify(tmp_path)
    assert target.read_bytes() == changed


@pytest.mark.parametrize("path", [prepare.GATEWAY, *prepare.CODEC_PATHS,
                                   prepare.CORE_WIT, prepare.BRIDGE_WIT])
def test_changed_full_source_or_dependency_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    prepare.prepare(tmp_path)
    capture = prepare._capture
    monkeypatch.setattr(prepare, "_capture", lambda source: (
        capture(source) + b"\n# changed omitted source\n"
        if source == path else capture(source)
    ))
    with pytest.raises(ValueError):
        prepare.verify(tmp_path)


@pytest.mark.parametrize("filename,_", prepare.WRAPPERS.values())
def test_changed_wrapper_source_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, _: str,
) -> None:
    prepare.prepare(tmp_path)
    capture = prepare._capture
    monkeypatch.setattr(prepare, "_capture", lambda path: capture(path) + (
        b"\n# omitted wrapper changed\n" if path.endswith(filename) else b""
    ))
    with pytest.raises(ValueError, match="inventory.json"):
        prepare.verify(tmp_path)


def test_bad_wrapper_identity_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = prepare._capture
    monkeypatch.setattr(prepare, "_capture", lambda path: capture(path).replace(
        b'FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.text-generate"',
        b'FUNCTION_ID = "not.the.original.caller"',
    ) if path.endswith("/text_generate.py") else capture(path))
    with pytest.raises(ValueError, match="wrapper identity"):
        prepare._prepared_files()


def test_missing_verification_does_not_create_directory(tmp_path: Path) -> None:
    output = tmp_path / "missing"
    with pytest.raises(ValueError):
        prepare.verify(output)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["leaf", "ancestor", "fifo", "oversized"])
def test_bounded_read_rejects_unsafe_files(tmp_path: Path, kind: str) -> None:
    real = tmp_path / "real"
    real.mkdir()
    target = real / "data"
    target.write_bytes(b"original")
    path = target
    if kind == "leaf":
        path = real / "link"
        path.symlink_to(target)
    elif kind == "ancestor":
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        path = link / "data"
    elif kind == "fifo":
        path = real / "fifo"
        os.mkfifo(path)
    with pytest.raises(ValueError):
        prepare.bounded_read(path, 2 if kind == "oversized" else 1024)
    assert target.read_bytes() == b"original"


@pytest.mark.parametrize("kind", ["leaf", "ancestor", "hardlink", "fifo"])
def test_preparation_rejects_unsafe_targets_without_writes(
    tmp_path: Path, kind: str,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    target = tmp_path / "untouched"
    target.write_bytes(b"untouched")
    output = real
    leaf = real / "original_gateway_tool_caller.py"
    if kind == "ancestor":
        output = tmp_path / "link"
        output.symlink_to(real, target_is_directory=True)
    elif kind == "leaf":
        leaf.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, leaf)
    else:
        os.mkfifo(leaf)
    with pytest.raises(ValueError):
        prepare.prepare(output)
    assert target.read_bytes() == b"untouched"
    assert not (real / "inventory.json").exists()


def test_duplicate_evidence_keys_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        prepare._strict_json(b'{"a":1,"a":2}')


def test_relative_parent_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parent traversal"):
        prepare._directory(Path("one/../two"), create=True)
    with pytest.raises(ValueError, match="parent traversal"):
        prepare.bounded_read(tmp_path / ".." / "data")


def test_source_only_cli(tmp_path: Path) -> None:
    for flag in ([], ["--verify"]):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(tmp_path), *flag],
            capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["build_performed"] is False
    assert {path.name for path in tmp_path.iterdir()} == OUTPUT_NAMES
