"""Offline public producer tests through the actual Pack artifact compiler."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from core_runtime.pack_authoring import (
    PackAuthoringError,
    PythonPackFunction,
    build_python_pack,
)
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.errors import InvalidArtifactError
from tobkiri_host.models import PackageKind
from tobkiri_protocol.canonical import canonical_digest


SOURCE = b"""raise RuntimeError("authoring must never execute this module")

def tobkiri_packvm_invoke(operation_id: str, payload: dict) -> dict:
    return {"message": payload["message"]}
"""


def _contract() -> dict:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"message": {"type": "string", "maxLength": 256}},
        "required": ["message"],
    }
    digest = canonical_digest(schema)
    unsigned = {
        "contract_api_version": "io.tobkiri.contract.v4",
        "contract_id": "example.echo.v1",
        "version": "1.0.0",
        "owner": "example.echo",
        "status": "draft",
        "operations": [
            {
                "operation_id": "echo",
                "input_schema_digest": digest,
                "output_schema_digest": digest,
                "error_schema_digest": digest,
                "effect_ceiling": [],
                "scope_semantics": "declarative",
                "idempotency": {"mode": "replayable"},
            }
        ],
        "schema_catalog": {digest: schema},
        "provider_semantics": {
            "provider_id": "example.echo.provider",
            "cardinality": "one",
            "security": "public",
            "failure": "fail_closed",
            "isolation": "sandbox",
            "required_capabilities": [],
            "lifecycle": {},
        },
    }
    return {
        **unsigned,
        "revision_digest": canonical_digest(unsigned),
        "provenance": {
            "schema": "io.tobkiri.provenance.v1",
            "source_kind": "generated",
            "source_path": "build.py",
            "source_digest": canonical_digest(unsigned),
            "repository_commit": "working-tree",
            "repository_tree": "0" * 64,
            "generator": "example.author",
            "generator_version": "1.0.0",
            "normative": False,
            "evidence": [],
        },
    }


def _function() -> PythonPackFunction:
    return PythonPackFunction(
        "example.echo.function",
        "example.echo.v1",
        ("echo",),
        "runtime/main.py",
        SOURCE,
    )


def _build(
    target: Path,
    *,
    contract: dict | None = None,
    function: PythonPackFunction | None = None,
    **kwargs,
) -> Path:
    return build_python_pack(
        target,
        pack_id="example.echo",
        version="1.0.0",
        display_name="Example",
        contracts=[contract or _contract()],
        functions=[function or _function()],
        **kwargs,
    )


def test_public_producer_compiles_without_importing_pack_code(tmp_path: Path) -> None:
    root = _build(tmp_path / "pack")
    compiled = compile_pack_root(root)
    route = compiled.routes[("example.echo.v1", "echo")]
    assert route["backend"] == "tobkiri.python-pack-v4"
    assert route["runtime_abi"] == "python3.13"
    assert route["execution_kind"] == "pack_vm"
    assert compiled.artifact.package_kind is PackageKind.NORMAL
    manifest = json.loads((root / "pack.v4.json").read_text())
    assert manifest["requirements"]["capabilities"] == []
    assert manifest["requirements"]["execution_boundary"] == "sandbox"
    assert not (root / ".tobkiri").exists()


def test_deterministic_build_and_tampered_source_rejected(tmp_path: Path) -> None:
    first, second = _build(tmp_path / "a"), _build(tmp_path / "b")
    assert {
        path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()
    } == {
        path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()
    }
    (first / "runtime/main.py").write_bytes(SOURCE + b"# mutation\n")
    with pytest.raises(InvalidArtifactError, match="implementation|digest"):
        compile_pack_root(first)


@pytest.mark.parametrize("operation_ids", [("unknown",), ("echo", "echo"), ()])
def test_unknown_duplicate_or_missing_binding_denied(
    tmp_path: Path,
    operation_ids: tuple[str, ...],
) -> None:
    with pytest.raises(PackAuthoringError):
        _build(
            tmp_path / "pack",
            function=replace(
                _function(),
                operation_ids=operation_ids,
            ),
        )
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("mutation", ["revision", "schema", "capability"])
def test_stale_contract_or_authority_expansion_denied(
    tmp_path: Path,
    mutation: str,
) -> None:
    contract = _contract()
    if mutation == "revision":
        contract["status"] = "accepted"
    elif mutation == "schema":
        next(iter(contract["schema_catalog"].values()))["maxProperties"] = 2
    else:
        contract["provider_semantics"]["required_capabilities"] = ["file.write"]
    if mutation != "revision":
        contract["revision_digest"] = canonical_digest(
            {
                key: value
                for key, value in contract.items()
                if key not in {"provenance", "revision_digest"}
            }
        )
    with pytest.raises(PackAuthoringError):
        _build(tmp_path / "pack", contract=contract)
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/absolute",
        "x/../outside",
        "pack.v4.json",
        ".tobkiri/signed-pack.json",
        "runtime/main.py",
        "x\\escape",
        "x\nname",
    ],
)
def test_unsafe_or_reserved_assets_never_escape(tmp_path: Path, name: str) -> None:
    with pytest.raises(PackAuthoringError):
        _build(tmp_path / "pack", assets={name: b"input"})
    assert not (tmp_path / "pack").exists()
    assert not (tmp_path / "outside").exists()


def test_existing_directory_and_symlink_ancestor_preserved(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(PackAuthoringError):
        _build(existing)
    assert list(existing.iterdir()) == []
    link = tmp_path / "link"
    link.symlink_to(existing, target_is_directory=True)
    with pytest.raises(PackAuthoringError):
        _build(link / "pack")
    assert list(existing.iterdir()) == []


def test_missing_real_python_abi_denied(tmp_path: Path) -> None:
    with pytest.raises(PackAuthoringError, match="tobkiri_packvm_invoke"):
        _build(
            tmp_path / "pack",
            function=replace(
                _function(),
                source=b"def echo(payload): return payload\n",
            ),
        )
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("source", [SOURCE + b'\nnew_syntax = t"template"\n', b"\xff"])
def test_host_newer_syntax_or_invalid_utf8_cannot_claim_guest_abi(
    tmp_path: Path,
    source: bytes,
) -> None:
    with pytest.raises(PackAuthoringError, match="UTF-8 Python 3.13"):
        _build(tmp_path / "pack", function=replace(_function(), source=source))
    assert not (tmp_path / "pack").exists()
