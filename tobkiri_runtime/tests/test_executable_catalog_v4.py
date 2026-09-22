from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tobkiri_host.artifact_compiler import compile_pack_root, routes_for_plan
from tobkiri_host.errors import InvalidArtifactError, ResolutionError
from tobkiri_protocol.errors import SchemaValidationError


ROOT = Path(__file__).resolve().parent.parent


def test_all_canonical_executable_catalogs_compile_without_exclusion() -> None:
    pack_roots = sorted(path.parent for path in (ROOT / "ecosystem").glob("*/pack.v4.json"))
    compiled = [compile_pack_root(path) for path in pack_roots]
    assert len(compiled) == 143
    assert {item.artifact.pack_id for item in compiled} == {path.name for path in pack_roots}

    conversation = next(item for item in compiled if item.artifact.pack_id == "defaultspack")
    inspect = next(item for item in compiled if item.artifact.pack_id == "rumi_file_inspect_pack")
    selected_operations = {
        (operation.contract_id, operation.operation_id)
        for artifact in (conversation.artifact, inspect.artifact)
        for function in artifact.functions
        for operation in function.operations
    }
    assert selected_operations == {
        ("conversation.turn.v1", "complete"),
        (
            "tobkiri.service.file.inspect.v1",
            "rumi_file_inspect_pack.file-inspect",
        ),
        (
            "tobkiri.service.file.inspect.v1",
            "rumi_file_inspect_pack.file-inspect.for-media",
        ),
    }
    assert set(conversation.routes) == {("conversation.turn.v1", "complete")}
    assert set(inspect.routes) == {
        (
            "tobkiri.service.file.inspect.v1",
            "rumi_file_inspect_pack.file-inspect",
        ),
        (
            "tobkiri.service.file.inspect.v1",
            "rumi_file_inspect_pack.file-inspect.for-media",
        ),
    }


def test_compiler_rejects_tamper_missing_variant_and_source_swap(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "defaultspack"
    shutil.copytree(ROOT / "ecosystem" / "defaultspack", copied)
    runtime = copied / "runtime" / "conversation.py"
    runtime.write_text(runtime.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(InvalidArtifactError, match="digest mismatch"):
        compile_pack_root(copied)

    shutil.rmtree(copied)
    shutil.copytree(ROOT / "ecosystem" / "defaultspack", copied)
    catalog_path = copied / "executables.v4.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["variants"] = []
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises((InvalidArtifactError, SchemaValidationError)):
        compile_pack_root(copied)

    shutil.rmtree(copied)
    shutil.copytree(ROOT / "ecosystem" / "defaultspack", copied)
    catalog_path = copied / "executables.v4.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["source_identity"] = "sha256:" + "0" * 64
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(InvalidArtifactError, match="source identity"):
        compile_pack_root(copied)


def test_compiler_rejects_missing_stale_duplicate_and_unqualified_catalogs(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "pack"
    source = ROOT / "ecosystem" / "defaultspack"
    shutil.copytree(source, copied)
    (copied / "executables.v4.json").unlink()
    with pytest.raises((FileNotFoundError, SchemaValidationError)):
        compile_pack_root(copied)

    shutil.rmtree(copied)
    shutil.copytree(source, copied)
    path = copied / "executables.v4.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["catalog_digest"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(InvalidArtifactError, match="catalog digest"):
        compile_pack_root(copied)

    shutil.rmtree(copied)
    shutil.copytree(source, copied)
    path = copied / "executables.v4.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["variants"].append(catalog["variants"][0])
    unsigned = {key: value for key, value in catalog.items() if key != "catalog_digest"}
    from tobkiri_protocol.canonical import canonical_digest

    catalog["catalog_digest"] = canonical_digest(unsigned)
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(
        (InvalidArtifactError, SchemaValidationError),
        match="duplicate Function|duplicate identity",
    ):
        compile_pack_root(copied)

    shutil.rmtree(copied)
    shutil.copytree(source, copied)
    path = copied / "executables.v4.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["variants"][0]["operations"].append(catalog["variants"][0]["operations"][0])
    unsigned = {key: value for key, value in catalog.items() if key != "catalog_digest"}
    catalog["catalog_digest"] = canonical_digest(unsigned)
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(
        (InvalidArtifactError, SchemaValidationError),
        match="duplicated or unqualified|duplicate identity",
    ):
        compile_pack_root(copied)


def test_compiler_rejects_catalog_swap_after_catalog_self_digest_reseal(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "defaultspack"
    shutil.copytree(ROOT / "ecosystem" / "defaultspack", copied)
    path = copied / "executables.v4.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["variants"][0]["backend"] = "tobkiri.remote-pack-v4"
    from tobkiri_protocol.canonical import canonical_digest

    catalog["catalog_digest"] = canonical_digest(
        {key: value for key, value in catalog.items() if key != "catalog_digest"}
    )
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(InvalidArtifactError, match="artifact digest mismatch"):
        compile_pack_root(copied)


def _single_plan_binding(compiled: Any) -> dict[str, Any]:
    key, metadata = next(iter(compiled.routes.items()))
    contract_id, operation_id = key
    function = compiled.artifact.function(metadata["function_id"])
    operation = next(
        item
        for item in function.operations
        if item.contract_id == contract_id and item.operation_id == operation_id
    )
    principal = {
        "parent_artifact_digest": compiled.artifact.digest,
        "function_implementation_digest": function.implementation_digest,
        "function_id": function.function_id,
        "contract_revision_digest": operation.revision_digest,
        "operation_id": operation.operation_id,
    }
    return {
        "pack_id": compiled.artifact.pack_id,
        "artifact_digest": compiled.artifact.digest,
        "function_principal": principal,
        "contract_id": contract_id,
        "operation_id": operation_id,
        "domain_kind": metadata["domain_kind"],
        "executable_catalog_digest": metadata["catalog_digest"],
        "variant_id": metadata["variant_id"],
        "platform": metadata["platform"],
        "architecture": metadata["architecture"],
        "runtime_abi": metadata["runtime_abi"],
        "backend": metadata["backend"],
        "execution_kind": metadata["execution_kind"],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("executable_catalog_digest", "sha256:" + "0" * 64),
        ("variant_id", "defaultspack.swapped"),
        ("backend", "tobkiri.remote-pack-v4"),
        ("execution_kind", "remote"),
        ("domain_kind", "remote"),
    ),
)
def test_routes_for_plan_rejects_every_exact_variant_pin_swap(
    field: str,
    value: str,
) -> None:
    compiled = compile_pack_root(ROOT / "ecosystem" / "defaultspack")
    binding = _single_plan_binding(compiled)
    binding[field] = value
    with pytest.raises(ResolutionError, match="executable variant pin"):
        routes_for_plan({"bindings": [binding]}, (compiled,))
