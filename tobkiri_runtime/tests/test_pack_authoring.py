"""Tests for the bounded external Pack authoring builder."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from core_runtime.pack_authoring import (
    PythonPackFunction,
    build_python_pack,
    GENERATED_FILENAMES,
    PackAuthoringError,
    build_authored_pack,
)
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_host.errors import InvalidArtifactError
from tobkiri_host.models import PackageKind
from tobkiri_host.artifact_compiler import compile_pack_root

ROOT = Path(__file__).resolve().parent.parent
GLOBAL_CATALOGS = (
    ROOT / "schemas" / "pack_v4_catalog.v1.json",
    ROOT / "schemas" / "executable_sources.v1.json",
)

IMPLEMENTATION = '''"""Minimal PackVM implementation."""


def tobkiri_packvm_invoke(operation_id, payload):
    """Echo the supplied text back to the Host."""
    return {"text": payload.get("text", ""), "operation_id": operation_id}
'''


def _source(pack_id: str, operation_id: str, contract_id: str) -> dict:
    return {
        "source_api_version": "io.tobkiri.pack-source.v1",
        "pack_id": pack_id,
        "version": "1.0.0",
        "display_name": f"Authored {pack_id}",
        "contracts": [
            {
                "contract_id": contract_id,
                "version": "1.0.0",
                "provider_id": f"{pack_id}.provider",
                "operations": [
                    {
                        "operation_id": operation_id,
                        "input_schema": {
                            "type": "object",
                            "required": ["text"],
                            "properties": {"text": {"type": "string"}},
                        },
                        "output_schema": {
                            "type": "object",
                            "required": ["text"],
                            "properties": {"text": {"type": "string"}},
                        },
                        "effect_class": "pure",
                        "scope_semantics": "declarative",
                    }
                ],
            }
        ],
        "functions": [
            {
                "function_id": f"{pack_id}.fn",
                "implementation_path": "runtime/invoke.py",
                "operations": [operation_id],
            }
        ],
    }


def _write_pack(
    base: Path,
    pack_id: str,
    operation_id: str,
    contract_id: str,
    *,
    implementation: str = IMPLEMENTATION,
) -> Path:
    root = base / pack_id
    (root / "runtime").mkdir(parents=True)
    (root / "pack-source.v1.json").write_text(
        json.dumps(_source(pack_id, operation_id, contract_id), indent=2)
        + "\n",
        encoding="utf-8",
    )
    (root / "runtime" / "invoke.py").write_text(implementation, encoding="utf-8")
    (root / "README.md").write_text(f"# {pack_id}\n", encoding="utf-8")
    return root


def _output(base: Path, pack_id: str) -> Path:
    return base / "built" / pack_id


def _tree_snapshot(root: Path) -> dict[str, str]:
    """Hash every regular file under ``root`` for equality checks."""

    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return snapshot


def _catalog_digests() -> dict[Path, str]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in GLOBAL_CATALOGS
    }


def test_two_independent_packs_build_and_compile(tmp_path: Path) -> None:
    catalogs_before = _catalog_digests()
    sources = [
        _write_pack(tmp_path, "alpha.echo", "alpha.echo", "alpha.echo.v1"),
        _write_pack(tmp_path, "beta.math", "beta.math.add", "beta.math.v1"),
    ]
    outputs = [
        _output(tmp_path, "alpha.echo"),
        _output(tmp_path, "beta.math"),
    ]
    results = [
        build_authored_pack(source, output)
        for source, output in zip(sources, outputs)
    ]
    assert results[0]["operations"] == ["alpha.echo.v1/alpha.echo"]
    assert results[1]["operations"] == ["beta.math.v1/beta.math.add"]

    for output in outputs:
        compiled = compile_pack_root(output)
        assert compiled.artifact.package_kind.value == "normal"
        assert all(
            variant.execution_kind.value == "pack_vm"
            for variant in compiled.artifact.variants
        )

    # Global catalogs were not read or modified by the builds.
    assert _catalog_digests() == catalogs_before


def test_build_is_deterministic_across_outputs(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "alpha.det", "alpha.det", "alpha.det.v1")
    first = _output(tmp_path / "a", "alpha.det")
    second = _output(tmp_path / "b", "alpha.det")
    result_a = build_authored_pack(source, first)
    result_b = build_authored_pack(source, second)
    assert result_a["artifacts"] == result_b["artifacts"]
    assert result_a["artifact_digest"] == result_b["artifact_digest"]
    assert _tree_snapshot(first) == _tree_snapshot(second)


def test_build_never_mutates_source(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "gamma.source", "gamma.op", "gamma.v1")
    before = _tree_snapshot(source)
    output = _output(tmp_path, "gamma.source")
    build_authored_pack(source, output)
    assert _tree_snapshot(source) == before
    assert not any(source.glob("*.v4.json"))


def test_failed_build_never_mutates_source(tmp_path: Path) -> None:
    source = _write_pack(
        tmp_path,
        "delta.broken",
        "delta.op",
        "delta.v1",
        implementation="def tobkiri_packvm_invoke(:\n",
    )
    before = _tree_snapshot(source)
    output = _output(tmp_path, "delta.broken")
    with pytest.raises(PackAuthoringError):
        build_authored_pack(source, output)
    assert _tree_snapshot(source) == before
    assert not output.exists()
    # No staging leftovers beside the output either.
    assert list(output.parent.iterdir()) == []


def test_output_collision_is_refused(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "epsilon.collide", "epsilon.op", "epsilon.v1")
    output = _output(tmp_path, "epsilon.collide")

    output.parent.mkdir(parents=True)
    output.write_text("occupied\n", encoding="utf-8")
    with pytest.raises(PackAuthoringError, match="occupied"):
        build_authored_pack(source, output)
    output.unlink()

    output.mkdir()
    (output / "pack.v4.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PackAuthoringError, match="occupied"):
        build_authored_pack(source, output)
    assert json.loads((output / "pack.v4.json").read_text()) == {}
    shutil.rmtree(output)

    real = tmp_path / "real_target"
    real.mkdir()
    output.symlink_to(real, target_is_directory=True)
    with pytest.raises(PackAuthoringError, match="occupied"):
        build_authored_pack(source, output)


def test_output_name_must_equal_pack_id(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "zeta.named", "zeta.op", "zeta.v1")
    with pytest.raises(PackAuthoringError, match="must equal pack_id"):
        build_authored_pack(source, tmp_path / "different.name")


def test_check_mode_verifies_and_never_writes(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "eta.check", "eta.op", "eta.v1")
    output = _output(tmp_path, "eta.check")
    build_authored_pack(source, output)
    source_snapshot = _tree_snapshot(source)
    output_snapshot = _tree_snapshot(output)

    result = build_authored_pack(source, output, check=True)
    assert result["check"] is True
    assert _tree_snapshot(source) == source_snapshot
    assert _tree_snapshot(output) == output_snapshot


def test_check_rejects_absent_output_without_writing(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "theta.absent", "theta.op", "theta.v1")
    output = _output(tmp_path, "theta.absent")
    source_snapshot = _tree_snapshot(source)
    with pytest.raises(PackAuthoringError, match="existing output"):
        build_authored_pack(source, output, check=True)
    assert not output.exists()
    assert _tree_snapshot(source) == source_snapshot


def test_check_detects_stale_output(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "iota.stale", "iota.op", "iota.v1")
    output = _output(tmp_path, "iota.stale")
    build_authored_pack(source, output)
    manifest = output / "pack.v4.json"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '"iota.stale"', '"iota.forged"', 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(PackAuthoringError, match="diverges"):
        build_authored_pack(source, output, check=True)


def test_build_preserves_source_files_and_indexes_them(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "kappa.assets", "kappa.op", "kappa.v1")
    (source / "data").mkdir()
    (source / "data" / "table.json").write_text('{"a": 1}\n', encoding="utf-8")
    output = _output(tmp_path, "kappa.assets")
    build_authored_pack(source, output)
    manifest = json.loads((output / "pack.v4.json").read_text(encoding="utf-8"))
    paths = {entry["path"]: entry["kind"] for entry in manifest["artifacts"]}
    assert paths["pack-source.v1.json"] == "sidecar"
    assert paths["runtime/invoke.py"] == "executable"
    assert paths["data/table.json"] == "asset"
    assert paths["README.md"] == "asset"
    assert (output / "runtime" / "invoke.py").read_text(
        encoding="utf-8"
    ) == IMPLEMENTATION


def test_source_containing_generated_quartet_is_rejected(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "lambda.quartet", "lambda.op", "lambda.v1")
    (source / "pack.v4.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PackAuthoringError, match="generated artifact"):
        build_authored_pack(source, _output(tmp_path, "lambda.quartet"))


def test_requirements_pass_through_verbatim(tmp_path: Path) -> None:
    pack_id = "mu.requirements"
    source = _write_pack(tmp_path, pack_id, "mu.op", "mu.v1")
    document = _source(pack_id, "mu.op", "mu.v1")
    document["capabilities"] = ["connector.adapter.verify", "qr.present"]
    document["secrets"] = ["MU_API_TOKEN"]
    document["network"] = {
        "allowed_domains": ["api.example.invalid"],
        "allowed_ports": [443],
    }
    document["approval_policy"] = "capability_gated"
    document["workspace_boundary"] = "workspace_brokered"
    document["contract_dependencies"] = [
        {
            "contract_id": "dep.contract.v2",
            "version_range": ">=2.0.0,<3.0.0",
            "cardinality": "keyed",
            "optional": True,
            "instance_key": "tenant",
            "operations": ["dep.contract.run"],
        }
    ]
    document["pack_dependencies"] = {"dep.pack": ">=1.0.0,<2.0.0"}
    (source / "pack-source.v1.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )
    output = _output(tmp_path, pack_id)
    build_authored_pack(source, output)
    manifest = json.loads((output / "pack.v4.json").read_text(encoding="utf-8"))
    requirements = manifest["requirements"]
    assert requirements["capabilities"] == [
        "connector.adapter.verify",
        "qr.present",
    ]
    assert requirements["secrets"] == ["MU_API_TOKEN"]
    assert requirements["network"] == {
        "allowed_domains": ["api.example.invalid"],
        "allowed_ports": [443],
    }
    assert requirements["approval_policy"] == "capability_gated"
    assert requirements["workspace_boundary"] == "workspace_brokered"
    assert requirements["contract_dependencies"][0]["contract_id"] == (
        "dep.contract.v2"
    )
    assert requirements["pack_dependencies"] == {"dep.pack": ">=1.0.0,<2.0.0"}
    # The Pack stays a Normal Sandbox Pack: declarations are not elevation.
    assert manifest["pack"]["kind"] == "normal_sandbox"
    assert requirements["execution_boundary"] == "sandbox"


def test_contract_dependency_optional_requires_bool(tmp_path: Path) -> None:
    pack_id = "nu.bool"
    source = _write_pack(tmp_path, pack_id, "nu.op", "nu.v1")
    document = _source(pack_id, "nu.op", "nu.v1")
    document["contract_dependencies"] = [
        {
            "contract_id": "dep.contract.v2",
            "version_range": ">=2.0.0,<3.0.0",
            "cardinality": "one",
            "optional": "false",
        }
    ]
    (source / "pack-source.v1.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )
    with pytest.raises(PackAuthoringError, match="boolean"):
        build_authored_pack(source, _output(tmp_path, pack_id))


def _expect_rejection(
    tmp_path: Path,
    mutate,
    pattern: str,
    *,
    pack_id: str = "bad.pack",
) -> None:
    source = _write_pack(tmp_path, pack_id, "bad.op", "bad.op.v1")
    mutate(source)
    with pytest.raises(PackAuthoringError, match=pattern):
        build_authored_pack(source, _output(tmp_path, pack_id))


def test_rejects_malformed_source_json(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        (root / "pack-source.v1.json").write_text("{ nope", encoding="utf-8")

    _expect_rejection(tmp_path, mutate, "malformed")


def test_rejects_unknown_source_field(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        source = json.loads(
            (root / "pack-source.v1.json").read_text(encoding="utf-8")
        )
        source["grants"] = ["host.exec"]
        (root / "pack-source.v1.json").write_text(
            json.dumps(source), encoding="utf-8"
        )

    _expect_rejection(tmp_path, mutate, "unknown fields")


def test_rejects_wrong_source_api_version(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        source = json.loads(
            (root / "pack-source.v1.json").read_text(encoding="utf-8")
        )
        source["source_api_version"] = "io.tobkiri.pack-source.v0"
        (root / "pack-source.v1.json").write_text(
            json.dumps(source), encoding="utf-8"
        )

    _expect_rejection(tmp_path, mutate, "io.tobkiri.pack-source.v1")


def test_rejects_duplicate_operation_across_contracts(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        source = json.loads(
            (root / "pack-source.v1.json").read_text(encoding="utf-8")
        )
        source["contracts"].append(
            {
                "contract_id": "other.contract.v1",
                "version": "1.0.0",
                "provider_id": "other.provider",
                "operations": [
                    {
                        "operation_id": "bad.op",
                        "input_schema": {"type": "object"},
                        "output_schema": {"type": "object"},
                    }
                ],
            }
        )
        (root / "pack-source.v1.json").write_text(
            json.dumps(source), encoding="utf-8"
        )

    _expect_rejection(tmp_path, mutate, "duplicate operation_id")


def test_rejects_unimplemented_contract_operation(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        source = json.loads(
            (root / "pack-source.v1.json").read_text(encoding="utf-8")
        )
        source["contracts"][0]["operations"].append(
            {
                "operation_id": "bad.unimplemented",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            }
        )
        (root / "pack-source.v1.json").write_text(
            json.dumps(source), encoding="utf-8"
        )

    _expect_rejection(tmp_path, mutate, "lack an implementing function")


def test_rejects_unknown_function_operation(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        source = json.loads(
            (root / "pack-source.v1.json").read_text(encoding="utf-8")
        )
        source["functions"][0]["operations"].append("ghost.operation")
        (root / "pack-source.v1.json").write_text(
            json.dumps(source), encoding="utf-8"
        )

    _expect_rejection(tmp_path, mutate, "unknown operations")


def test_rejects_non_callable_entrypoint_assignment(tmp_path: Path) -> None:
    _expect_rejection(
        tmp_path,
        lambda root: (root / "runtime" / "invoke.py").write_text(
            "tobkiri_packvm_invoke = 42\n", encoding="utf-8"
        ),
        "lambda",
    )


def test_rejects_async_entrypoint(tmp_path: Path) -> None:
    _expect_rejection(
        tmp_path,
        lambda root: (root / "runtime" / "invoke.py").write_text(
            "async def tobkiri_packvm_invoke(operation_id, payload):\n"
            "    return {}\n",
            encoding="utf-8",
        ),
        "synchronous",
    )


@pytest.mark.parametrize(
    "body",
    [
        "def tobkiri_packvm_invoke():\n    return {}\n",
        "def tobkiri_packvm_invoke(operation_id):\n    return {}\n",
        "def tobkiri_packvm_invoke(a, b, c):\n    return {}\n",
        "def tobkiri_packvm_invoke(a, b, *, c):\n    return {}\n",
        "def tobkiri_packvm_invoke(*, operation_id, payload):\n    return {}\n",
    ],
    ids=[
        "zero-args",
        "one-arg",
        "three-required",
        "required-kwonly",
        "kwonly",
    ],
)
def test_rejects_abi_incompatible_signatures(
    tmp_path: Path, body: str
) -> None:
    _expect_rejection(
        tmp_path,
        lambda root: (root / "runtime" / "invoke.py").write_text(
            body, encoding="utf-8"
        ),
        "positional|never passes",
    )


def test_rejects_decorated_entrypoint(tmp_path: Path) -> None:
    _expect_rejection(
        tmp_path,
        lambda root: (root / "runtime" / "invoke.py").write_text(
            "import functools\n\n"
            "@functools.lru_cache\n"
            "def tobkiri_packvm_invoke(operation_id, payload):\n"
            "    return {}\n",
            encoding="utf-8",
        ),
        "decorated",
    )


@pytest.mark.parametrize(
    "body",
    [
        "def tobkiri_packvm_invoke(operation_id, payload):\n    return {}\n",
        "def tobkiri_packvm_invoke(operation_id, payload=None):\n"
        "    return {}\n",
        "def tobkiri_packvm_invoke(operation_id, *args):\n    return {}\n",
        "def tobkiri_packvm_invoke(*args, **kwargs):\n    return {}\n",
        "tobkiri_packvm_invoke = lambda operation_id, payload: {}\n",
        "def tobkiri_packvm_invoke(a, b, *, c=None):\n    return {}\n",
    ],
    ids=[
        "two-positional",
        "defaulted-payload",
        "varargs",
        "args-kwargs",
        "lambda-assignment",
        "optional-kwonly",
    ],
)
def test_accepts_abi_compatible_signatures(
    tmp_path: Path, body: str
) -> None:
    source = _write_pack(tmp_path, "ok.pack", "ok.op", "ok.op.v1")
    (source / "runtime" / "invoke.py").write_text(body, encoding="utf-8")
    build_authored_pack(source, _output(tmp_path, "ok.pack"))


def test_rejects_non_stdlib_import(tmp_path: Path) -> None:
    _expect_rejection(
        tmp_path,
        lambda root: (root / "runtime" / "invoke.py").write_text(
            "import requests\n\n\ndef tobkiri_packvm_invoke(o, p):\n"
            "    return {}\n",
            encoding="utf-8",
        ),
        "non-stdlib",
    )


def test_rejects_relative_import(tmp_path: Path) -> None:
    _expect_rejection(
        tmp_path,
        lambda root: (root / "runtime" / "invoke.py").write_text(
            "from . import helper\n\n\ndef tobkiri_packvm_invoke(o, p):\n"
            "    return {}\n",
            encoding="utf-8",
        ),
        "relative import",
    )


def test_rejects_syntax_error_implementation(tmp_path: Path) -> None:
    _expect_rejection(
        tmp_path,
        lambda root: (root / "runtime" / "invoke.py").write_text(
            "def tobkiri_packvm_invoke(:\n", encoding="utf-8"
        ),
        "syntax error",
    )


def test_rejects_traversal_implementation_path(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        source = json.loads(
            (root / "pack-source.v1.json").read_text(encoding="utf-8")
        )
        source["functions"][0]["implementation_path"] = "../escape.py"
        (root / "pack-source.v1.json").write_text(
            json.dumps(source), encoding="utf-8"
        )

    _expect_rejection(tmp_path, mutate, "safe relative")


def test_rejects_symlinked_file(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "xi.symlink", "xi.op", "xi.v1")
    target = tmp_path / "outside.txt"
    target.write_text("secret\n", encoding="utf-8")
    (source / "README.md").unlink()
    os.symlink(target, source / "README.md")
    with pytest.raises(PackAuthoringError, match="symlink"):
        build_authored_pack(source, _output(tmp_path, "xi.symlink"))


def test_rejects_invalid_contract_id(tmp_path: Path) -> None:
    _expect_rejection(
        tmp_path,
        lambda root: (root / "pack-source.v1.json").write_text(
            json.dumps(
                _source("bad.pack", "bad.op", "contract-without-version")
            ),
            encoding="utf-8",
        ),
        "contract",
    )


def test_rejects_invalid_operation_schema(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        source = json.loads(
            (root / "pack-source.v1.json").read_text(encoding="utf-8")
        )
        source["contracts"][0]["operations"][0]["input_schema"] = {
            "type": "not-a-type"
        }
        (root / "pack-source.v1.json").write_text(
            json.dumps(source), encoding="utf-8"
        )

    _expect_rejection(tmp_path, mutate, "schema")


def test_rejects_symlinked_source_root(tmp_path: Path) -> None:
    source = _write_pack(tmp_path, "pi.root", "pi.op", "pi.v1")
    link = tmp_path / "pi.link"
    os.symlink(source, link, target_is_directory=True)
    with pytest.raises(PackAuthoringError, match="real directory"):
        build_authored_pack(link, _output(tmp_path, "pi.root"))


def test_documented_example_pack_builds(tmp_path: Path) -> None:
    source_root = ROOT / "examples" / "external_pack" / "example.echo"
    source = tmp_path / "source" / "example.echo"
    shutil.copytree(source_root, source)
    output = tmp_path / "dist" / "example.echo"
    result = build_authored_pack(source, output)
    assert result["operations"] == ["example.echo.v1/example.echo"]
    compiled = compile_pack_root(output)
    assert compiled.artifact.pack_id == "example.echo"
    # The checked-in example is source-only; nothing was generated inside it.
    assert not (source_root / "pack.v4.json").exists()


def test_cli_build_subcommand(tmp_path: Path) -> None:
    from scripts.tobkiri_pack import main

    source = _write_pack(tmp_path, "rho.cli", "rho.op", "rho.v1")
    output = _output(tmp_path, "rho.cli")
    assert main(["build", str(source), str(output)]) == 0
    assert main(["build", str(source), str(output), "--check"]) == 0
    for name in GENERATED_FILENAMES:
        assert (output / name).is_file()

@pytest.mark.parametrize('nested', [False, True])
def test_output_inside_source_is_rejected_before_any_write(tmp_path: Path, nested: bool) -> None:
    source = _write_pack(tmp_path, 'inside.pack', 'inside.op', 'inside.v1')
    before = _tree_snapshot(source)
    output = source / 'build' / 'inside.pack' if nested else source
    with pytest.raises(PackAuthoringError, match='outside the source tree'):
        build_authored_pack(source, output)
    assert _tree_snapshot(source) == before
    assert not (source / 'build').exists()

@pytest.mark.parametrize('limit', ['_MAX_FILE_BYTES', '_MAX_TOTAL_BYTES', '_MAX_FILES'])
def test_source_bounds_apply_before_copy(tmp_path: Path, monkeypatch, limit: str) -> None:
    import core_runtime.pack_authoring as authoring
    source = _write_pack(tmp_path, 'bounded.pack', 'bounded.op', 'bounded.v1')
    before = _tree_snapshot(source)
    monkeypatch.setattr(authoring, limit, 1)
    output = _output(tmp_path, 'bounded.pack')
    with pytest.raises(PackAuthoringError, match='limit'):
        build_authored_pack(source, output)
    assert _tree_snapshot(source) == before
    assert not output.exists()
    assert not list(output.parent.glob('.pack-authoring-*'))


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
