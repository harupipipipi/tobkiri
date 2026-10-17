"""JobBroker imports under the actual digest-verified synthetic Host loader."""

from pathlib import Path
import hashlib
import json
import shutil
import pytest
import core_runtime
from core_runtime.host_provider_hooks_v4 import load_host_provider_factory
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.contracts import OperationCatalog, OperationRoute
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.errors import InvalidArtifactError
from tobkiri_protocol.canonical import canonical_digest

PACK = "rumi_job_action_broker_pack"
RUNTIME = Path(core_runtime.__file__).resolve().parents[1]
SOURCE = Path(__file__).resolve().parents[1] / "ecosystem" / PACK / "runtime/broker.py"


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def sealed_binding(tmp_path):
    root = tmp_path / PACK
    shutil.copytree(RUNTIME / "ecosystem" / PACK, root)
    shutil.copyfile(SOURCE, root / "runtime/broker.py")
    executable = json.loads((root / "executables.v4.json").read_text())
    implementation = digest(root / "runtime/broker.py")
    for variant in executable["variants"]:
        variant["implementation_digest"] = implementation
    executable["catalog_digest"] = canonical_digest(
        {key: value for key, value in executable.items() if key != "catalog_digest"}
    )
    write(root / "executables.v4.json", executable)
    manifest = json.loads((root / "pack.v4.json").read_text())
    for function in manifest["functions"]:
        function["implementation_digest"] = implementation
    artifacts = {item["path"]: item for item in manifest["artifacts"]}
    artifacts["runtime/dispatch_envelope.py"] = {
        "path": "runtime/dispatch_envelope.py",
        "kind": "executable",
        "digest": digest(root / "runtime/dispatch_envelope.py"),
    }
    for item in artifacts.values():
        item["digest"] = digest(root / item["path"])
    manifest["artifacts"] = sorted(artifacts.values(), key=lambda item: item["path"])
    artifact_digest = canonical_digest(manifest["artifacts"])
    manifest["pack"]["artifact_digest"] = artifact_digest
    manifest["integrity"]["artifact_set_digest"] = artifact_digest
    write(root / "pack.v4.json", manifest)
    index = json.loads((root / "artifact-index.v4.json").read_text())
    entries = {item["path"]: item for item in index["artifacts"]}
    entries["runtime/dispatch_envelope.py"] = {
        "path": "runtime/dispatch_envelope.py",
        "role": "runtime",
        "digest": digest(root / "runtime/dispatch_envelope.py"),
    }
    for item in entries.values():
        item["digest"] = digest(root / item["path"])
    index["artifacts"] = sorted(entries.values(), key=lambda item: item["path"])
    index["artifact_set_digest"] = artifact_digest
    index["integrity_seal"]["signed_digest"] = canonical_digest(
        {key: value for key, value in index.items() if key != "integrity_seal"}
    )
    write(root / "artifact-index.v4.json", index)
    compiled = compile_pack_root(root)
    (contract, operation), metadata = next(iter(compiled.routes.items()))
    function = compiled.artifact.functions[0]
    route = OperationRoute(
        contract_id=contract,
        operation_id=operation,
        artifact_digest=compiled.artifact.digest,
        function_id=function.function_id,
        variant_id=metadata["variant_id"],
        catalog_digest=compiled.artifact.catalog_digest,
        platform=metadata["platform"],
        architecture=metadata["architecture"],
        runtime_abi=metadata["runtime_abi"],
        backend=metadata["backend"],
        execution_kind=metadata["execution_kind"],
        domain_kind=metadata["domain_kind"],
        execution_domain_profile=metadata["execution_domain_profile"],
        materialization_mode=metadata["materialization_mode"],
        target_principal_ref=OpaqueAuthorityRef("authority:job-import-test"),
    )
    return root, OperationCatalog((compiled.artifact,), (route,)).resolve(
        contract, operation, ">=1,<2"
    )


def test_actual_verified_host_loader_imports_absolute_dispatch_helper(tmp_path):
    root, binding = sealed_binding(tmp_path)
    factory = load_host_provider_factory(root, binding)
    assert factory.function_id == "rumi_job_action_broker_pack.job-action.broker"
    assert callable(factory.capture)


def test_imported_dispatch_helper_is_in_verified_artifact_set(tmp_path):
    root, binding = sealed_binding(tmp_path)
    path = root / "runtime/dispatch_envelope.py"
    path.write_text(path.read_text() + "\n# tampered helper\n")
    with pytest.raises(InvalidArtifactError, match="digest"):
        load_host_provider_factory(root, binding)
