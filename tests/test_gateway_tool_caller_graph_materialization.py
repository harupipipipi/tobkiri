"""Structural two-Pack publication tests, never guest or native acceptance.

Tests use inert component-header bytes and fabricated receipts with test-only
pin replacement. They do not compile, compose, instantiate, or execute a guest.
"""

from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import materialize_gateway_tool_caller_graph as materializer

ACTUAL_PINS = copy.deepcopy(materializer.REVIEWED_PINS)
BRIDGE_MEMBERS = {
    "component.wasm", "component.build.json", "config.json",
    "wit/bridge.shape.json", "wit/pack-v2.wit", "wit/deps/core.wit",
}
CALLER_MEMBERS = {
    "component.wasm", "component.build.json", "config.json",
    "wit/caller.shape.json", "wit/pack.wit", "wit/deps/core.wit",
}


@pytest.fixture(scope="module")
def templates(tmp_path_factory: Any) -> dict[str, dict[str, Any]]:
    """Build only source plans and inert structural receipt templates."""
    result = {}
    root = tmp_path_factory.mktemp("caller-graph-templates")
    for name, staging in (
        ("bridge", materializer.bridge_stage),
        ("caller", materializer.caller_stage),
    ):
        stage = root / name
        plan = staging.stage(stage)
        manifest = [
            {**item, "bytes": len((stage / item["source"]).read_bytes())}
            for item in plan["sources"]
        ]
        result[name] = {
            "format": "tobkiri.component-build/2", "language": "python",
            "build_plan_sha256": materializer.digest(
                (stage / "plan.json").read_bytes(),
            ),
            "source_manifest": manifest,
            "source_manifest_sha256": materializer.digest(
                materializer.canonical(manifest),
            ),
            "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
            "exports": sorted(plan["outer_exports"]),
        }
    return result


@pytest.fixture
def inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    templates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Replace production pins only within a synthetic structural test."""
    pins = copy.deepcopy(ACTUAL_PINS)
    paths = {}
    for name in ("bridge", "caller"):
        artifact = tmp_path / f"{name}.wasm"
        binary = b"\0asm\x0d\0\x01\0" + name.encode() + b"-INERT-NOT-A-COMPONENT"
        artifact.write_bytes(binary)
        receipt = {
            **copy.deepcopy(templates[name]),
            "artifact_sha256": materializer.digest(binary),
            "artifact_bytes": len(binary),
        }
        encoded = materializer.canonical(receipt)
        artifact.with_suffix(".build.json").write_bytes(encoded)
        paths[name] = artifact
        pins[name] = {
            "artifact_sha256": materializer.digest(binary),
            "receipt_sha256": materializer.digest(encoded),
        }
    monkeypatch.setattr(materializer, "REVIEWED_PINS", copy.deepcopy(pins))
    return {**paths, "pins": pins, "oracle": materializer.ORACLE}


def run(inputs: dict[str, Any], destination: Path) -> dict[str, Any]:
    """Publish synthetic bytes only, with no runtime/producer invocation."""
    return materializer.materialize(
        inputs["bridge"], inputs["caller"], inputs["oracle"], destination,
        pins=inputs["pins"],
    )


def repin_receipt(
    inputs: dict[str, Any], name: str, receipt: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reach structural checks beyond the deliberately replaced fixture hash."""
    path = inputs[name].with_suffix(".build.json")
    path.write_bytes(materializer.canonical(receipt))
    inputs["pins"][name]["receipt_sha256"] = materializer.digest(path.read_bytes())
    monkeypatch.setattr(
        materializer, "REVIEWED_PINS", copy.deepcopy(inputs["pins"]),
    )


def assert_unpublished(tmp_path: Path) -> None:
    assert not (tmp_path / "graph").exists()
    assert not list(tmp_path.glob(".gateway-caller-graph-*"))


def test_production_identity_is_current_and_not_historical() -> None:
    assert ACTUAL_PINS["source_observations_sha256"] == (
        "854fa03de651c12cb7126d8f81e4f0c62e340a5a85f71eb50784e539e207bced"
    )
    archive = ROOT / "tools/python313-producer/trials/2026-10-20-finalization/builds"
    for name, archived in (("bridge", "bridge-v2"), ("caller", "caller")):
        receipt_bytes = (archive / archived / "component.build.json").read_bytes()
        receipt = json.loads(receipt_bytes)
        assert ACTUAL_PINS[name] == {
            "artifact_sha256": receipt["artifact_sha256"],
            "receipt_sha256": materializer.digest(receipt_bytes),
        }
    assert materializer.MAX_GRAPH_BYTES == 64 * 1024 * 1024
    assert materializer.MAX_OUTER_BYTES == 4 * 1024 * 1024


def test_exact_separate_pack_members_shapes_permissions_and_index(
    inputs: dict[str, Any], tmp_path: Path,
) -> None:
    output = tmp_path / "graph"
    report = run(inputs, output)
    index_bytes = (output / "index.json").read_bytes()
    index = json.loads(index_bytes)
    assert report == {**index, "index_sha256": materializer.digest(index_bytes)}
    assert index_bytes == materializer.canonical(index)
    assert set(index) == {
        "format", "protocol", "scope", "components", "source_observations",
        "component_source_bytes", "core_component_catalog_limit_bytes",
        "caller_wit_closure", "outer_budget", "build_performed",
        "wasm_execution_performed", "native_core_acceptance", "production_enabled",
    }
    assert index["format"] == "tobkiri.gateway-tool-caller-native-inputs/1"
    assert index["scope"] == "two-separate-python-packs-not-finalizer"
    assert index["protocol"] == "ordered-finite-json-v2"
    assert index["core_component_catalog_limit_bytes"] == 67_108_864
    assert index["component_source_bytes"] == sum(
        inputs[name].stat().st_size for name in ("bridge", "caller")
    )
    assert set(index["components"]) == {"bridge", "caller"}
    for name, expected_members in (
        ("bridge", BRIDGE_MEMBERS), ("caller", CALLER_MEMBERS),
    ):
        pack = output / name
        manifest_bytes = (pack / "pack.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        component = index["components"][name]
        assert set(component) == {
            "directory", "pack_id", "pack_version", "shape", "pack_sha256",
            "artifact_sha256", "artifact_bytes", "receipt_sha256",
            "build_plan_sha256", "source_manifest_sha256",
        }
        assert component["directory"] == name
        assert component["pack_id"] == manifest["id"]
        assert component["pack_version"] == manifest["version"]
        assert component["pack_sha256"] == materializer.digest(manifest_bytes)
        assert component["artifact_sha256"] == inputs["pins"][name]["artifact_sha256"]
        assert component["receipt_sha256"] == inputs["pins"][name]["receipt_sha256"]
        assert (pack / "component.wasm").read_bytes() == inputs[name].read_bytes()
        assert {item["path"] for item in manifest["files"]} == expected_members
        assert {
            path.relative_to(pack).as_posix() for path in pack.rglob("*")
            if path.is_file()
        } == expected_members | {"pack.json"}
        for item in manifest["files"]:
            data = (pack / item["path"]).read_bytes()
            assert item["size"] == len(data)
            assert item["sha256"] == "sha256:" + materializer.digest(data)
        assert manifest["runtime"]["kind"] == "wasm"
        assert manifest["runtime"]["config"] == {"path": "config.json"}
        assert (pack / "config.json").read_bytes() == b"{}"
        assert all(manifest[key] == [] for key in (
            "inputs", "flows", "overlays", "stores",
        ))
        assert manifest["ui"] is None
        permissions = manifest["permissions"]
        assert permissions["network"] == {"origins": []}
        assert permissions["fs"] == permissions["host_io"] == permissions["proc"] == []
        assert permissions["secrets"] == []
        assert all(permissions[key] is False for key in (
            "native", "pty", "mediator", "audit_append", "audit_read",
            "cross_profile_storage",
        ))
        assert permissions["ports"] == (
            [materializer.BRIDGE_SHAPE] if name == "caller" else []
        )
    caller_manifest = json.loads((output / "caller/pack.json").read_bytes())
    assert caller_manifest["version"] == "1.0.0"
    assert caller_manifest["shapes"] == [{
        "name": materializer.CALLER_SHAPE, "version": "1.0", "kind": "port",
        "wit": "wit/pack.wit",
    }]
    assert caller_manifest["ports"] == {
        "provides": [{
            "shape": materializer.CALLER_SHAPE, "version": "1.0", "optional": False,
        }],
        "consumes": [{
            "shape": materializer.bridge_pack.SHAPE,
            "version": "2.0", "optional": False,
        }],
        "untyped": [],
    }
    source = (materializer.caller_stage.PACK / "wit/pack.wit").read_bytes()
    combined = (output / "caller/wit/pack.wit").read_bytes()
    bridge = (output / "bridge/wit/pack-v2.wit").read_bytes()
    assert len(combined) == 1099
    assert combined == (
        source + b"\n" + bridge[:49] + b" {" + bridge[50:] + b"}\n"
    )
    assert combined[:731] == source
    assert combined[732:781] + b";" + combined[783:-2] == bridge
    assert combined.count(b"interface caller") == 1
    assert combined.count(b"interface bridge") == 1
    assert combined.count(b"world pack") == 2
    assert index["caller_wit_closure"] == materializer.CALLER_WIT_CLOSURE
    assert set(index["caller_wit_closure"]) == {
        "caller_source_destination", "caller_source_sha256", "caller_source_bytes",
        "bridge_source_destination", "bridge_source_sha256", "bridge_source_bytes",
        "bridge_package_semicolon_offset", "member", "member_sha256", "member_bytes",
    }
    assert materializer.digest(combined) == (
        "687f47f6c592ee1bc195d481d02efd0c051f664729e965cc0271850735ffaa04"
    )
    assert (output / "caller/wit/deps/core.wit").read_bytes() == (
        output / "bridge/wit/deps/core.wit"
    ).read_bytes()
    assert (output / "source-observations.json").read_bytes() == (
        inputs["oracle"].read_bytes()
    )
    assert index["source_observations"] == {
        "path": "source-observations.json",
        "sha256": ACTUAL_PINS["source_observations_sha256"], "bytes": 94_513,
    }
    assert index["outer_budget"] == materializer.envelope_bounds(
        json.loads(inputs["oracle"].read_bytes())
    )
    for flag in (
        "build_performed", "wasm_execution_performed",
        "native_core_acceptance", "production_enabled",
    ):
        assert index[flag] is False
    assert {path.name for path in output.iterdir()} == {
        "bridge", "caller", "index.json", "source-observations.json",
    }
    for path in output.rglob("*"):
        assert not path.is_symlink()
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert path.stat().st_nlink == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert not list(tmp_path.glob(".gateway-caller-graph-*"))


@pytest.mark.parametrize("name", ["bridge", "caller"])
@pytest.mark.parametrize("receipt", [False, True])
@pytest.mark.parametrize("mutation", [
    "tamper", "missing", "symlink", "hardlink", "directory", "fifo",
])
def test_unsafe_or_changed_inputs_fail_closed_without_blocking(
    inputs: dict[str, Any], tmp_path: Path,
    name: str, receipt: bool, mutation: str,
) -> None:
    path = inputs[name].with_suffix(".build.json") if receipt else inputs[name]
    if mutation == "tamper":
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "hardlink":
        os.link(path, path.with_name(path.name + ".extra"))
    else:
        retained = path.with_name(path.name + ".retained")
        path.rename(retained)
        if mutation == "symlink":
            path.symlink_to(retained)
        elif mutation == "directory":
            path.mkdir()
        elif mutation == "fifo":
            os.mkfifo(path)
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


@pytest.mark.parametrize("mutation", [
    "missing", "extra", "wrong", "uppercase", "swapped", "not-dict",
])
def test_explicit_pins_must_match_independently_reviewed_identity(
    inputs: dict[str, Any], tmp_path: Path, mutation: str,
) -> None:
    pins = inputs["pins"]
    if mutation == "missing":
        del pins["source_observations_sha256"]
    elif mutation == "extra":
        pins["wac_sha256"] = "0" * 64
    elif mutation == "wrong":
        pins["caller"]["artifact_sha256"] = "0" * 64
    elif mutation == "uppercase":
        pins["caller"]["receipt_sha256"] = pins["caller"]["receipt_sha256"].upper()
    elif mutation == "swapped":
        pins["caller"], pins["bridge"] = pins["bridge"], pins["caller"]
    else:
        inputs["pins"] = []
    with pytest.raises(ValueError, match="explicit pins"):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


@pytest.mark.parametrize("name", ["bridge", "caller"])
@pytest.mark.parametrize("mutation", [
    "format", "language", "world", "imports", "exports", "plan", "bytes-bool",
    "source-hash", "source-bytes", "source-duplicate", "source-missing",
    "source-destination", "duplicate-json",
])
def test_receipt_repin_cannot_bypass_complete_current_source_plan(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    name: str, mutation: str,
) -> None:
    path = inputs[name].with_suffix(".build.json")
    receipt = json.loads(path.read_bytes())
    if mutation in {"format", "language", "world", "imports", "exports"}:
        receipt[mutation] = "wrong"
    elif mutation == "plan":
        receipt["build_plan_sha256"] = "0" * 64
    elif mutation == "bytes-bool":
        receipt["artifact_bytes"] = True
    elif mutation == "source-hash":
        receipt["source_manifest"][0]["sha256"] = "0" * 64
    elif mutation == "source-bytes":
        receipt["source_manifest"][0]["bytes"] += 1
    elif mutation == "source-duplicate":
        receipt["source_manifest"].append(copy.deepcopy(receipt["source_manifest"][0]))
    elif mutation == "source-missing":
        receipt["source_manifest"].pop()
    elif mutation == "source-destination":
        receipt["source_manifest"][0]["destination"] = "elsewhere.py"
    receipt["source_manifest_sha256"] = materializer.digest(
        materializer.canonical(receipt["source_manifest"])
    )
    repin_receipt(inputs, name, receipt, monkeypatch)
    if mutation == "duplicate-json":
        path.write_bytes(b'{"format":"duplicate",' + path.read_bytes()[1:])
        inputs["pins"][name]["receipt_sha256"] = materializer.digest(path.read_bytes())
        monkeypatch.setattr(
            materializer, "REVIEWED_PINS", copy.deepcopy(inputs["pins"]),
        )
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


def test_receipt_claims_do_not_become_materializer_acceptance(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("bridge", "caller"):
        receipt = json.loads(inputs[name].with_suffix(".build.json").read_bytes())
        receipt.update(build_performed=True, wasm_execution_performed=True,
                       native_core_acceptance=True, production_enabled=True)
        repin_receipt(inputs, name, receipt, monkeypatch)
    result = run(inputs, tmp_path / "graph")
    for flag in (
        "build_performed", "wasm_execution_performed",
        "native_core_acceptance", "production_enabled",
    ):
        assert result[flag] is False


@pytest.mark.parametrize("name", ["bridge", "caller"])
def test_reforged_receipt_does_not_turn_arbitrary_bytes_into_a_component(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    binary = b"not a component"
    inputs[name].write_bytes(binary)
    receipt = json.loads(inputs[name].with_suffix(".build.json").read_bytes())
    receipt["artifact_sha256"] = materializer.digest(binary)
    receipt["artifact_bytes"] = len(binary)
    inputs["pins"][name]["artifact_sha256"] = materializer.digest(binary)
    repin_receipt(inputs, name, receipt, monkeypatch)
    with pytest.raises(ValueError, match="not a Wasm component"):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


@pytest.mark.parametrize("mutation", [
    "hash", "bindings", "case-input", "missing-case", "classification",
    "duplicate-json",
])
def test_current_oracle_remains_bound_beyond_an_explicit_hash(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    document = json.loads(inputs["oracle"].read_bytes())
    if mutation == "bindings":
        document["source_bindings"][0]["sha256"] = "0" * 64
    elif mutation == "case-input":
        document["cases"][0]["input_json"] = "{}"
    elif mutation == "missing-case":
        document["cases"].pop()
    elif mutation == "classification":
        document["cases"][0]["classification"] = "provider-invalid"
    raw = materializer.canonical(document)
    if mutation == "duplicate-json":
        raw = b'{"format":"duplicate",' + raw[1:]
    oracle = tmp_path / "oracle.json"
    oracle.write_bytes(raw)
    inputs["oracle"] = oracle
    if mutation != "hash":
        inputs["pins"]["source_observations_sha256"] = materializer.digest(raw)
        monkeypatch.setattr(
            materializer, "REVIEWED_PINS", copy.deepcopy(inputs["pins"]),
        )
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


@pytest.mark.parametrize("name", ["caller", "bridge"])
@pytest.mark.parametrize(
    "mutation", ["suffix", "duplicate", "trim", "world", "package"],
)
def test_caller_closure_accepts_only_exact_reviewed_sources(
    name: str, mutation: str,
) -> None:
    sources = {
        "caller": (materializer.caller_stage.PACK / "wit/pack.wit").read_bytes(),
        "bridge": (
            materializer.bridge_stage.PACK / "wit/pack-v2.wit"
        ).read_bytes(),
    }
    source = sources[name]
    if mutation == "suffix":
        source += b"\n"
    elif mutation == "duplicate":
        source += f"interface {name} {{}}\n".encode()
    elif mutation == "trim":
        source = source.rstrip()
    elif mutation == "world":
        source = source.replace(f"  export {name};".encode(), b"  export other;")
    else:
        source = source.replace(b";", b" {", 1) + b"}\n"
    sources[name] = source
    with pytest.raises(ValueError):
        materializer.package_caller_wit(sources["caller"], sources["bridge"])


@pytest.mark.parametrize("member", [
    "wit/deps/bridge-v2.wit", "wit/shape.wit", "wit/world.wit", "wit/extra.wit",
])
def test_caller_manifest_rejects_extra_or_duplicate_wit_members(member: str) -> None:
    files = {name: b"test" for name in CALLER_MEMBERS}
    files[member] = b"duplicate or unrelated WIT must never enter the Pack"
    with pytest.raises(ValueError, match="exact six-member"):
        materializer._caller_manifest(files)


@pytest.mark.parametrize("member", sorted(CALLER_MEMBERS))
def test_caller_manifest_rejects_missing_closure_members(member: str) -> None:
    files = {name: b"test" for name in CALLER_MEMBERS if name != member}
    with pytest.raises(ValueError, match="exact six-member"):
        materializer._caller_manifest(files)


def test_outer_envelopes_preserve_opaque_inner_strings_and_bound_expansion() -> None:
    case = {
        "operation": "generate-intents", "input_json": "opaque, not parsed JSON",
        "classification": "success", "output_json": '{"n":18446744073709551616}',
    }
    result = materializer.envelope_bounds({"cases": [case]})
    assert result["max_request_bytes"] == len(
        materializer.canonical({"payload": case["input_json"]})
    )
    assert result["max_success_response_bytes"] == len(
        materializer.canonical(case["output_json"])
    )
    limit = materializer.MAX_OUTER_BYTES
    for field, value in (
        ("input_json", '"' * (limit // 2)),
        ("output_json", "x" * (limit - 1)),
        ("input_json", "😀" * (limit // 4)),
    ):
        with pytest.raises(ValueError, match="outer-envelope budget"):
            materializer.envelope_bounds({"cases": [{**case, field: value}]})
    value = "x" * (limit - len(materializer.canonical({"payload": ""})))
    assert materializer.envelope_bounds({"cases": [{**case, "input_json": value}]})[
        "max_request_bytes"
    ] == limit
    assert materializer.envelope_bounds({"cases": [{
        **case, "output_json": "x" * (limit - 2),
    }]})["max_success_response_bytes"] == limit


@pytest.mark.parametrize("field,value", [
    ("operation", "normalize"), ("operation", 1), ("input_json", {}),
    ("output_json", []), ("classification", "excluded"),
])
def test_invalid_outer_surface_is_rejected(field: str, value: Any) -> None:
    case = {
        "operation": "stream-intents", "input_json": "{}",
        "classification": "success", "output_json": "{}",
    }
    with pytest.raises(ValueError):
        materializer.envelope_bounds({"cases": [{**case, field: value}]})


@pytest.mark.parametrize("target", ["bridge", "caller", "oracle", "destination"])
def test_symlinked_ancestors_are_rejected(
    inputs: dict[str, Any], tmp_path: Path, target: str,
) -> None:
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    output = tmp_path / "graph"
    if target == "destination":
        output = link / "graph"
    else:
        if target == "oracle":
            copied = tmp_path / "oracle.json"
            copied.write_bytes(inputs[target].read_bytes())
            inputs[target] = copied
        inputs[target] = link / inputs[target].name
    with pytest.raises(ValueError, match="symlink"):
        run(inputs, output)
    assert_unpublished(tmp_path)


@pytest.mark.parametrize("kind", ["directory", "file", "dangling-symlink"])
def test_existing_destination_is_never_replaced(
    inputs: dict[str, Any], tmp_path: Path, kind: str,
) -> None:
    output = tmp_path / "graph"
    if kind == "directory":
        output.mkdir()
        (output / "keep").write_bytes(b"unchanged")
    elif kind == "file":
        output.write_bytes(b"unchanged")
    else:
        output.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError):
        run(inputs, output)
    if kind == "directory":
        assert (output / "keep").read_bytes() == b"unchanged"
    elif kind == "file":
        assert output.read_bytes() == b"unchanged"
    else:
        assert output.is_symlink()
    assert not list(tmp_path.glob(".gateway-caller-graph-*"))


def test_absent_parent_and_traversal_do_not_create_directories(
    inputs: dict[str, Any], tmp_path: Path,
) -> None:
    for output in (tmp_path / "absent/graph", tmp_path / "../graph"):
        with pytest.raises(ValueError):
            run(inputs, output)
    assert not (tmp_path / "absent").exists()
    assert_unpublished(tmp_path)


def test_internal_scratch_ignores_symlinked_default_temp_directory(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = tmp_path / "default-temp"
    default.mkdir()
    link = tmp_path / "linked-temp"
    link.symlink_to(default, target_is_directory=True)
    monkeypatch.setattr(materializer.tempfile, "tempdir", str(link))
    original = materializer.tempfile.TemporaryDirectory
    captured = []

    def create(*args: Any, **kwargs: Any) -> Any:
        assert kwargs.get("dir") == tmp_path
        directory = original(*args, **kwargs)
        captured.append(Path(directory.name))
        return directory

    monkeypatch.setattr(materializer.tempfile, "TemporaryDirectory", create)
    report = run(inputs, tmp_path / "graph")
    assert report["native_core_acceptance"] is False
    assert len(captured) == 4
    assert all(not path.exists() for path in captured)
    assert not list(default.iterdir())
    assert not list(tmp_path.glob("caller-graph-*"))


def test_catalog_bound_includes_all_published_bytes(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(materializer, "MAX_GRAPH_BYTES", 100_000)
    with pytest.raises(ValueError, match="catalog bound"):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


def test_member_is_bounded_before_full_capture(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(materializer, "MAX_GRAPH_BYTES", 8)
    with pytest.raises(ValueError, match="bounded"):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


def test_source_drift_fails_before_publication(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter([b"before", b"after"])
    monkeypatch.setattr(
        materializer.caller_stage, "reviewed_inputs", lambda: next(values),
    )
    with pytest.raises(ValueError, match="sources changed"):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


def test_racing_destination_is_preserved_and_private_staging_removed(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = materializer.bridge_pack._publish_no_replace

    def race(source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "keep").write_bytes(b"competitor")
        publish(source, destination)

    monkeypatch.setattr(materializer.bridge_pack, "_publish_no_replace", race)
    with pytest.raises(FileExistsError):
        run(inputs, tmp_path / "graph")
    assert (tmp_path / "graph/keep").read_bytes() == b"competitor"
    assert {p.name for p in (tmp_path / "graph").iterdir()} == {"keep"}
    assert not list(tmp_path.glob(".gateway-caller-graph-*"))


def test_unsupported_atomic_publish_fails_closed_and_cleans_up(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported(_source: Path, _destination: Path) -> None:
        raise OSError(errno.ENOTSUP, "unsupported filesystem")

    monkeypatch.setattr(materializer.bridge_pack, "_publish_no_replace", unsupported)
    with pytest.raises(OSError, match="unsupported"):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)


def test_write_failure_never_exposes_a_partial_graph(
    inputs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = materializer.caller_stage._write

    def fail_on_index(descriptor: int, name: str, data: bytes) -> None:
        if name == "index.json":
            raise OSError("injected publication write failure")
        original(descriptor, name, data)

    monkeypatch.setattr(materializer.caller_stage, "_write", fail_on_index)
    with pytest.raises(OSError, match="injected"):
        run(inputs, tmp_path / "graph")
    assert_unpublished(tmp_path)
