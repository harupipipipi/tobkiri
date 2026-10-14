from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tobkiri_host.artifact_compiler import compile_pack_root, routes_for_plan
from tobkiri_host.errors import InvalidArtifactError, ResolutionError
from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.secure_persistence import SecureDirectory


ROOT = Path(__file__).resolve().parent.parent


def test_compiler_captured_reader_preserves_artifact_and_route_identities() -> None:
    root = ROOT / "tests" / "fixtures" / "conformance_minimal_echo_pack"
    reader = SecureDirectory(root, create=False)
    expected = compile_pack_root(root)

    captured = compile_pack_root(
        root,
        read_artifact=lambda path: reader.read_bytes_bounded(
            path, max_bytes=128 * 1024 * 1024,
        ),
    )

    assert captured == expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO replacement contract")
def test_compiler_captured_reader_rejects_metadata_fifo_swap_without_blocking(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pack"
    shutil.copytree(
        ROOT / "tests" / "fixtures" / "conformance_minimal_echo_pack", root,
    )
    script = """
import os
from pathlib import Path
import sys
from tobkiri_host.artifact_compiler import compile_pack_root
import tobkiri_protocol.secure_persistence as persistence

root = Path(sys.argv[1])
reader = persistence.SecureDirectory(root, create=False)
target = root / "pack.v4.json"
real_open = persistence.os.open
replaced = False

def swap_then_open(path, flags, *args, **kwargs):
    global replaced
    if path == "pack.v4.json" and kwargs.get("dir_fd") is not None:
        assert not replaced
        replaced = True
        target.unlink()
        os.mkfifo(target, 0o600)
    return real_open(path, flags, *args, **kwargs)

persistence.os.open = swap_then_open
try:
    compile_pack_root(root, read_artifact=lambda name:
        reader.read_bytes_bounded(name, max_bytes=128 * 1024 * 1024))
except persistence.SecurePersistenceError:
    pass
else:
    raise AssertionError("a FIFO cannot supply compiler metadata")
finally:
    persistence.os.open = real_open
assert replaced
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_mcp_gateway_calls_compile_as_external_effects() -> None:
    """Arbitrary remote tool calls must retain ambiguous-outcome handling."""
    compiled = compile_pack_root(ROOT / "ecosystem" / "rumi_mcp_gateway_pack")
    operations = [
        operation
        for function in compiled.artifact.functions
        for operation in function.operations
    ]
    assert len(operations) == 1
    assert operations[0].effect_class.value == "external_effect"


@pytest.mark.parametrize(
    ("pack_id", "function_id", "operation_id"),
    (
        ("defaultspack", "defaultspack.conversation", "complete"),
        (
            "rumi_ai_gateway_pack",
            "rumi_ai_gateway_pack.ai-gateway.generate",
            "rumi_ai_gateway_pack.ai-gateway.generate",
        ),
        (
            "rumi_ai_gateway_pack",
            "rumi_ai_gateway_pack.ai-gateway.stream",
            "rumi_ai_gateway_pack.ai-gateway.stream",
        ),
        (
            "rumi_provider_adapters_pack",
            "rumi_provider_adapters_pack.provider.compatibility.generate",
            "rumi_provider_adapters_pack.provider-generate",
        ),
        (
            "rumi_provider_adapters_pack",
            "rumi_provider_adapters_pack.provider.compatibility.stream",
            "rumi_provider_adapters_pack.provider-stream",
        ),
    ),
)
def test_ai_conversation_chain_outlives_provider_transport_deadline(
    pack_id: str,
    function_id: str,
    operation_id: str,
) -> None:
    """Cold PackVM startup must not consume the provider's response budget."""

    catalog = json.loads(
        (ROOT / "ecosystem" / pack_id / "executables.v4.json").read_text(
            encoding="utf-8"
        )
    )
    variant = next(
        item for item in catalog["variants"] if item["function_id"] == function_id
    )
    operation = next(
        item
        for item in variant["operations"]
        if item["operation_id"] == operation_id
    )

    assert operation["timeout_default_ms"] == 120_000
    assert operation["timeout_hard_max_ms"] == 300_000


@pytest.mark.parametrize(
    ("pack_id", "function_id", "operation_id"),
    (
        (
            "tobkiri_ui_settings_pack",
            "tobkiri.ui.catalog.read",
            "tobkiri_ui_settings_pack.catalog-read",
        ),
        (
            "tobkiri_ui_settings_pack",
            "tobkiri.ui.settings.read",
            "tobkiri_ui_settings_pack.settings-read",
        ),
        (
            "rumi_command_protocol_pack",
            "rumi_command_protocol_pack.catalog.read",
            "command.catalog.read",
        ),
        (
            "defaultspack",
            "defaultspack.application-presentation",
            "defaultspack.presentation.read",
        ),
        *(
            (
                "tobkiri_host_pack_control",
                "tobkiri.host.control-presentation",
                operation_id,
            )
            for operation_id in (
                "profile.catalog.read",
                "profile.read",
                "settings.read",
                "topology.contracts.read",
                "topology.operations.read",
                "topology.packs.read",
                "topology.principals.read",
            )
        ),
    ),
)
def test_native_startup_reads_outlive_cold_packvm_start(
    pack_id: str,
    function_id: str,
    operation_id: str,
) -> None:
    """Startup reads may include one bounded native PackVM cold start."""

    catalog = json.loads(
        (ROOT / "ecosystem" / pack_id / "executables.v4.json").read_text(
            encoding="utf-8"
        )
    )
    variant = next(
        item for item in catalog["variants"] if item["function_id"] == function_id
    )
    operation = next(
        item
        for item in variant["operations"]
        if item["operation_id"] == operation_id
    )

    assert operation["timeout_default_ms"] == 120_000
    assert operation["timeout_hard_max_ms"] == 300_000


@pytest.mark.parametrize(
    ("pack_id", "function_id", "operation_id"),
    (
        (
            "defaultspack",
            "defaultspack.conversation.saved",
            "saved_complete",
        ),
        (
            "rumi_turn_runtime_pack",
            "rumi_turn_runtime_pack.turn-runtime.saved",
            "rumi_turn_runtime_pack.turn-saved",
        ),
    ),
)
def test_saved_turn_exchange_outlives_cold_packvm_start(
    pack_id: str,
    function_id: str,
    operation_id: str,
) -> None:
    """The saved exchange encloses cold start and a bounded generic chain."""

    catalog = json.loads(
        (ROOT / "ecosystem" / pack_id / "executables.v4.json").read_text(
            encoding="utf-8"
        )
    )
    variant = next(
        item for item in catalog["variants"] if item["function_id"] == function_id
    )
    operation = next(
        item
        for item in variant["operations"]
        if item["operation_id"] == operation_id
    )

    assert operation["timeout_default_ms"] == 300_000
    assert operation["timeout_hard_max_ms"] == 300_000


def test_all_canonical_executable_catalogs_compile_without_exclusion() -> None:
    pack_roots = sorted(path.parent for path in (ROOT / "ecosystem").glob("*/pack.v4.json"))
    compiled = [compile_pack_root(path) for path in pack_roots]
    declared = json.loads(
        (ROOT / "schemas/pack_v4_catalog.v1.json").read_text(encoding="utf-8")
    )["pack_ids"]
    assert {item.artifact.pack_id for item in compiled} == set(declared)
    assert {item.artifact.pack_id for item in compiled} == {path.name for path in pack_roots}

    command = next(
        item for item in compiled if item.artifact.pack_id == "rumi_command_protocol_pack"
    )
    assert set(command.routes) == {
        ("tobkiri.action.command.invoke.v1", "command.invoke"),
        ("tobkiri.resource.command.catalog.v1", "command.catalog.read"),
        ("tobkiri.resource.command.datasource.v1", "command.datasource.query"),
        ("tobkiri.resource.command.state.v1", "command.state.query"),
        ("tobkiri.service.command.high-risk.v1", "high_risk_command.manage"),
    }

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
        ("conversation.saved-turn.v1", "saved_complete"),
        ("tobkiri.resource.application.presentation.v1", "defaultspack.presentation.read"),
        (
            "tobkiri.service.python.sandbox.v1",
            "defaultspack.python-sandbox-guest-execute",
        ),
        (
            "tobkiri.service.file.inspect.v1",
            "rumi_file_inspect_pack.file-inspect",
        ),
        (
            "tobkiri.service.file.inspect.v1",
            "rumi_file_inspect_pack.file-inspect.for-media",
        ),
    }
    assert set(conversation.routes) == {
        ("conversation.turn.v1", "complete"),
        ("conversation.saved-turn.v1", "saved_complete"),
        ("tobkiri.resource.application.presentation.v1", "defaultspack.presentation.read"),
        (
            "tobkiri.service.python.sandbox.v1",
            "defaultspack.python-sandbox-guest-execute",
        ),
    }
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


@pytest.mark.parametrize(
    ("pack_id", "function_id", "operation_id"),
    (
        (
            "rumi_default_tools_pack",
            "rumi_default_tools_pack.python-sandbox-execute-tool",
            "rumi_default_tools_pack.python-sandbox-execute-operation",
        ),
        (
            "defaultspack",
            "defaultspack.python-sandbox-guest",
            "defaultspack.python-sandbox-guest-execute",
        ),
    ),
)
def test_python_sandbox_operations_outlive_bounded_guest_execution(
    pack_id: str,
    function_id: str,
    operation_id: str,
) -> None:
    """The 60-second guest budget plus VZ overhead fits the 120-second cap."""
    from scripts.generate_executable_catalogs_v4 import _operation_timeout_ms

    assert _operation_timeout_ms(pack_id, function_id, operation_id) == 120_000
    assert (
        _operation_timeout_ms("defaultspack", "unrelated.function", "read")
        == 30_000
    )


def test_python_sandbox_guest_schema_requires_every_script_field() -> None:
    """The signed guest contract rejects script requests its runner rejects."""
    catalog = json.loads(
        (ROOT / "schemas" / "pack_v4_catalog.v1.json").read_text(encoding="utf-8")
    )
    pack = next(item for item in catalog["packs"] if item["pack_id"] == "defaultspack")
    contract = next(
        item
        for item in pack["provided_contracts"]
        if item["contract_id"] == "tobkiri.service.python.sandbox.v1"
    )
    schema = contract["schemas"]["input"]
    script_branch = next(
        branch
        for branch in schema["oneOf"]
        if branch["properties"].get("mode", {}).get("const") == "script"
    )
    assert set(script_branch["required"]) == {
        "mode",
        "script_path",
        "arguments",
        "stdin",
    }

    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "legacy_executable_sources.v1.json").read_text(
            encoding="utf-8"
        )
    )
    host_entry = next(
        entry
        for entry in fixture["packs"]["rumi_default_tools_pack"]["entries"]
        if entry["function_id"]
        == "rumi_default_tools_pack.python-sandbox-execute-tool"
    )
    guest_entry = next(
        entry
        for entry in fixture["packs"]["defaultspack"]["entries"]
        if entry["function_id"] == "defaultspack.python-sandbox-guest"
    )
    assert host_entry["operation_ids"] == [
        "rumi_default_tools_pack.python-sandbox-execute-operation"
    ]
    assert guest_entry["operation_ids"] == [
        "defaultspack.python-sandbox-guest-execute"
    ]
    assert host_entry["schemas"]["input"]["additionalProperties"] is False
    assert guest_entry["schemas"]["input"]["additionalProperties"] is False
