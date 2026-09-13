from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"


def _document(filename: str) -> dict:
    return json.loads((DEFAULTSPACK_ROOT / filename).read_text(encoding="utf-8"))


def test_v4_catalog_contains_only_canonical_function_identities() -> None:
    pack = _document("pack.v4.json")
    executable = _document("executables.v4.json")

    function_ids = {item["id"] for item in pack["functions"]}
    variant_ids = {item["function_id"] for item in executable["variants"]}

    assert function_ids == {"defaultspack.conversation"}
    assert variant_ids == function_ids
    assert not any(function_id.startswith("defaults.") for function_id in function_ids)


def test_v4_catalog_pins_the_real_implementation_bytes() -> None:
    executable = _document("executables.v4.json")
    variant = executable["variants"][0]
    implementation = DEFAULTSPACK_ROOT / variant["implementation_path"]
    actual_digest = "sha256:" + hashlib.sha256(implementation.read_bytes()).hexdigest()

    assert variant["implementation_digest"] == actual_digest


def test_retired_alias_and_manifest_inputs_are_not_v4_authority() -> None:
    assert not (DEFAULTSPACK_ROOT / "ecosystem.json").exists()
    assert not (DEFAULTSPACK_ROOT / "compat_aliases.yaml").exists()
    assert not (DEFAULTSPACK_ROOT / "routes.json").exists()
    assert not (DEFAULTSPACK_ROOT / "domain" / "pack_architecture").exists()


def test_v4_pack_and_executable_catalog_share_the_same_source_identity() -> None:
    pack = _document("pack.v4.json")
    contracts = _document("contracts.v4.json")
    index = _document("artifact-index.v4.json")
    executable = _document("executables.v4.json")
    source_identity = pack["integrity"]["source_identity"]

    assert contracts["source_identity"] == source_identity
    assert index["source_identity"] == source_identity
    assert executable["source_identity"] == source_identity
