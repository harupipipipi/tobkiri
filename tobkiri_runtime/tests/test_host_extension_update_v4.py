"""Finite compatibility checks for signed, same-publisher Host Pack updates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core_runtime.host_extension_update_v4 import (
    HostExtensionUpdateBinding,
    HostExtensionUpdateDenied,
    verify_host_extension_successor,
)
from core_runtime.pack_signature import (
    SIGNED_MANIFEST_RELATIVE,
    build_signed_manifest,
    sign_manifest,
    verify_signed_pack,
)
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.errors import InvalidArtifactError
from tobkiri_protocol.canonical import canonical_digest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = RUNTIME_ROOT / "tests" / "fixtures" / "conformance_minimal_echo_pack"
PUBLISHER = "tobkiri.conformance"
HostPair = tuple[Path, Path, str]


def _read(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / name).read_text(encoding="utf-8"))


def _write(root: Path, name: str, value: object) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh(root: Path) -> None:
    manifest = _read(root, "pack.v4.json")
    executable = _read(root, "executables.v4.json")
    implementations = {
        variant["function_id"]: _digest(root / variant["implementation_path"])
        for variant in executable["variants"]
    }
    for function in manifest["functions"]:
        function["implementation_digest"] = implementations[function["id"]]
    for variant in executable["variants"]:
        variant["implementation_digest"] = implementations[variant["function_id"]]
    executable["catalog_digest"] = canonical_digest(
        {key: value for key, value in executable.items() if key != "catalog_digest"}
    )
    _write(root, "executables.v4.json", executable)
    manifest["integrity"]["contract_catalog_digest"] = _digest(
        root / "contracts.v4.json"
    )
    for item in manifest["artifacts"]:
        item["digest"] = _digest(root / item["path"])
    artifact_digest = canonical_digest(manifest["artifacts"])
    manifest["pack"]["artifact_digest"] = artifact_digest
    manifest["integrity"]["artifact_set_digest"] = artifact_digest
    _write(root, "pack.v4.json", manifest)
    index = _read(root, "artifact-index.v4.json")
    for item in index["artifacts"]:
        item["digest"] = _digest(root / item["path"])
    index["artifact_set_digest"] = artifact_digest
    index["integrity_seal"]["signed_digest"] = canonical_digest(
        {key: value for key, value in index.items() if key != "integrity_seal"}
    )
    _write(root, "artifact-index.v4.json", index)


def _sign(root: Path) -> dict[str, Any]:
    manifest = _read(root, "pack.v4.json")
    executable = _read(root, "executables.v4.json")
    identity = manifest["pack"]
    private_key = Ed25519PrivateKey.generate()
    signed = sign_manifest(
        build_signed_manifest(
            root,
            pack_id=identity["id"],
            version=identity["version"],
            publisher_id=identity.get("publisher_id", "tobkiri.repository"),
            core_compatibility=">=0",
            contract_versions={
                operation["contract_id"]: operation["contract_version"]
                for variant in executable["variants"]
                for operation in variant["operations"]
            },
            requested_capabilities=manifest["requirements"]["capabilities"],
        ),
        private_key,
    )
    _write(root, SIGNED_MANIFEST_RELATIVE, signed)
    verified = verify_signed_pack(root, signed, private_key.public_key())
    assert verified["publisher_id"] == identity.get(
        "publisher_id", "tobkiri.repository"
    )
    return signed


@pytest.fixture
def host_successor(tmp_path: Path) -> HostPair:
    """Build real finite Host artifacts and authenticate the successor signature."""

    previous = tmp_path / "previous" / "conformance.minimal.echo"
    shutil.copytree(FIXTURE, previous)
    manifest = _read(previous, "pack.v4.json")
    manifest["pack"]["kind"] = "host_extension"
    manifest["functions"][0]["role"] = "host_capability_provider"
    manifest["functions"][0]["isolation"] = "dedicated_process"
    manifest["requirements"].update(
        execution_boundary="host_brokered",
        workspace_boundary="host_brokered",
        approval_policy="capability_gated",
        capabilities=["capability:echo.execute", "host:brokered-execution"],
    )
    _write(previous, "pack.v4.json", manifest)
    executable = _read(previous, "executables.v4.json")
    executable["variants"][0].update(
        execution_kind="host_extension",
        backend="tobkiri.python-host-v4",
        runtime_abi="python3.13",
        execution_domain_profile="host.extension.default.v1",
        platform="any",
        architecture="any",
    )
    executable["variants"][0]["operations"][0]["effect_class"] = "privileged"
    _write(previous, "executables.v4.json", executable)
    _refresh(previous)
    previous_digest = compile_pack_root(previous).artifact.digest
    successor = tmp_path / "successor" / "conformance.minimal.echo"
    shutil.copytree(previous, successor)
    manifest = _read(successor, "pack.v4.json")
    manifest["pack"]["version"] = "1.1.0"
    _write(successor, "pack.v4.json", manifest)
    runtime = successor / "runtime" / "echo.py"
    runtime.write_text(runtime.read_text(encoding="utf-8") + "\n# signed update\n")
    _refresh(successor)
    _sign(successor)
    return previous, successor, previous_digest


def _verify(pair: HostPair, **kwargs: Any) -> HostExtensionUpdateBinding:
    previous, successor, previous_digest = pair
    return verify_host_extension_successor(
        previous,
        successor,
        expected_predecessor_digest=previous_digest,
        publisher_id=PUBLISHER,
        **kwargs,
    )


def _change_document(
    root: Path, name: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    value = _read(root, name)
    mutate(value)
    _write(root, name, value)
    _refresh(root)
    _sign(root)


def test_signed_runtime_update_preserves_exact_host_authority(
    host_successor: HostPair,
) -> None:
    """A signed byte update retains every existing Host authority declaration."""
    result = _verify(host_successor)

    assert result.predecessor_digest == host_successor[2]
    assert (
        result.artifact_digest == compile_pack_root(host_successor[1]).artifact.digest
    )
    assert result.artifact_digest != result.predecessor_digest
    assert result.publisher_id == PUBLISHER
    assert result.predecessor_version == "1.0.0"
    assert result.version == "1.1.0"
    assert result.compatibility_digest.startswith("sha256:")


def test_host_update_compiler_never_reopens_pack_bytes_with_raw_path_reads(
    host_successor: HostPair, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second compiler pass retains the bounded descriptor reader."""
    original = Path.read_bytes
    roots = host_successor[:2]

    def reject_pack_path_read(path: Path) -> bytes:
        if any(path.is_relative_to(root) for root in roots):
            raise AssertionError("compiler reopened untrusted Pack bytes")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", reject_pack_path_read)

    result = _verify(host_successor)

    assert result.predecessor_digest == host_successor[2]
    assert result.version == "1.1.0"


def test_bundled_gateway_can_receive_code_only_signed_successor(tmp_path: Path) -> None:
    """The real bundled Gateway keeps its Host kind and repository lineage."""

    previous = RUNTIME_ROOT / "ecosystem" / "rumi_ai_gateway_pack"
    successor = tmp_path / "rumi_ai_gateway_pack"
    # A development checkout may contain bytecode from prior imports. Copy
    # only source artifacts; the signer must still reject caches in a Pack.
    shutil.copytree(
        previous, successor,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    manifest = _read(successor, "pack.v4.json")
    manifest["pack"]["version"] = "1.1.0"
    _write(successor, "pack.v4.json", manifest)
    runtime = successor / "runtime" / "gateway.py"
    runtime.write_text(runtime.read_text(encoding="utf-8") + "\n# signed update\n")
    _refresh(successor)
    signed = _sign(successor)
    previous_digest = compile_pack_root(previous).artifact.digest

    result = verify_host_extension_successor(
        previous,
        successor,
        expected_predecessor_digest=previous_digest,
        publisher_id="tobkiri.repository",
        signed_manifest=signed,
    )

    assert result.pack_id == "rumi_ai_gateway_pack"
    assert result.publisher_id == "tobkiri.repository"


@pytest.mark.parametrize(
    ("document", "section", "field", "value"),
    [
        ("pack.v4.json", "requirements", "capabilities", ["host:root"]),
        ("pack.v4.json", "requirements", "pack_dependencies", {"other.pack": ">=1"}),
        (
            "pack.v4.json",
            "requirements",
            "network",
            {"allowed_domains": ["example.test"]},
        ),
        ("pack.v4.json", "requirements", "secrets", ["provider.api-key"]),
        ("pack.v4.json", "requirements", "approval_policy", "none"),
        ("pack.v4.json", "requirements", "workspace_boundary", "none"),
    ],
)
def test_signed_update_cannot_change_requirement_closure(
    host_successor: HostPair, document: str, section: str, field: str, value: Any
) -> None:
    """Updated code cannot add or remove any declared Host requirement."""
    _change_document(
        host_successor[1], document, lambda doc: doc[section].__setitem__(field, value)
    )

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(host_successor)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "other.host.backend"),
        ("runtime_abi", "python3.14"),
        ("execution_domain_profile", "host.extension.expanded.v1"),
        ("materialization_mode", "continuous"),
        ("platform", "linux"),
        ("architecture", "aarch64"),
        ("variant_id", "other.host.variant"),
    ],
)
def test_signed_update_cannot_change_variant_boundary(
    host_successor: HostPair, field: str, value: str
) -> None:
    """A signed implementation retains its exact backend and execution domain."""
    _change_document(
        host_successor[1],
        "executables.v4.json",
        lambda doc: doc["variants"][0].__setitem__(field, value),
    )

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(host_successor)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effect_class", "write"),
        ("idempotency", "replayable"),
        ("timeout_default_ms", 30_001),
        ("timeout_hard_max_ms", 600_001),
        ("contract_version", "2.0.0"),
    ],
)
def test_signed_update_cannot_change_operation_semantics(
    host_successor: HostPair, field: str, value: Any
) -> None:
    """Operation effects, versions, retries, and timeout contracts stay pinned."""
    _change_document(
        host_successor[1],
        "executables.v4.json",
        lambda doc: doc["variants"][0]["operations"][0].__setitem__(field, value),
    )

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(host_successor)


def test_signed_update_cannot_add_required_contract_edge(
    host_successor: HostPair,
) -> None:
    """An added required operation edge cannot inherit an existing Host grant."""
    _change_document(
        host_successor[1],
        "pack.v4.json",
        lambda doc: doc["requirements"]["contract_dependencies"].append(
            {
                "contract_id": "test.host.expansion.v1",
                "version_range": ">=1.0.0 <2.0.0",
                "cardinality": "one",
                "optional": False,
                "operations": ["execute"],
            }
        ),
    )

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(host_successor)


def test_signed_update_cannot_change_exported_effect_ceiling(
    host_successor: HostPair,
) -> None:
    """Exported effects cannot widen even if executable metadata is unchanged."""
    _change_document(
        host_successor[1],
        "pack.v4.json",
        lambda doc: doc["operation_catalog"][0]["effect_ceiling"].append("host:root"),
    )

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(host_successor)


@pytest.mark.parametrize("kind", ["base", "shell", "normal_sandbox", "application"])
def test_successor_cannot_reclassify_existing_host_pack(
    host_successor: HostPair, kind: str
) -> None:
    """No other administrative Pack kind can replace a Host Extension."""
    manifest = _read(host_successor[1], "pack.v4.json")
    manifest["pack"]["kind"] = kind
    _write(host_successor[1], "pack.v4.json", manifest)

    with pytest.raises(HostExtensionUpdateDenied, match="existing Host Extension"):
        _verify(host_successor)


@pytest.mark.parametrize("version", ["0.9.0", "1.0.0"])
def test_changed_host_implementation_requires_newer_version(
    host_successor: HostPair, version: str
) -> None:
    """Changed signed implementation bytes cannot reuse or lower the version."""
    _change_document(
        host_successor[1],
        "pack.v4.json",
        lambda doc: doc["pack"].__setitem__("version", version),
    )

    with pytest.raises(HostExtensionUpdateDenied, match="newer version"):
        _verify(host_successor)


def test_host_predecessor_digest_is_exact(host_successor: HostPair) -> None:
    """A candidate cannot bind to a stale or merely named predecessor."""
    with pytest.raises(HostExtensionUpdateDenied, match="predecessor digest is stale"):
        _verify((host_successor[0], host_successor[1], "sha256:" + "f" * 64))


def test_host_successor_digest_is_exact(host_successor: HostPair) -> None:
    """The caller can fence the selected successor artifact digest."""
    with pytest.raises(HostExtensionUpdateDenied, match="successor digest is stale"):
        _verify(host_successor, expected_successor_digest="sha256:" + "f" * 64)


def test_retained_publisher_cannot_differ_from_compiled_lineage(
    host_successor: HostPair,
) -> None:
    """A retained catalog publisher must agree with both compiled lineages."""
    with pytest.raises(HostExtensionUpdateDenied, match="publisher differs"):
        _verify(host_successor, predecessor_publisher_id="other.publisher")


def test_successor_cannot_claim_other_publisher(host_successor: HostPair) -> None:
    """A different signer lineage cannot inherit an existing Host boundary."""
    _change_document(
        host_successor[1],
        "pack.v4.json",
        lambda doc: doc["pack"].__setitem__("publisher_id", "other.publisher"),
    )

    with pytest.raises(HostExtensionUpdateDenied, match="publisher differs"):
        _verify(host_successor)


def test_successor_cannot_change_signing_lineage(host_successor: HostPair) -> None:
    """The successor cannot replace a pinned signing lineage digest."""
    _change_document(
        host_successor[1],
        "pack.v4.json",
        lambda doc: doc["pack"].__setitem__(
            "signing_lineage_digest", "sha256:" + "f" * 64
        ),
    )

    with pytest.raises(HostExtensionUpdateDenied, match="signing lineage differs"):
        _verify(host_successor)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pack_id", "other.pack"),
        ("publisher_id", "other.publisher"),
        ("version", "9.0.0"),
        ("requested_capabilities", []),
        ("contract_versions", {}),
        ("authority_granted", True),
        ("api_version", "other.signed-pack/v1"),
        ("signature", {}),
    ],
)
def test_signature_identity_must_match_compiled_authority(
    host_successor: HostPair, field: str, value: Any
) -> None:
    """Signed identity and requested authority must match compiled declarations."""
    signed = _read(host_successor[1], SIGNED_MANIFEST_RELATIVE)
    signed[field] = value

    with pytest.raises(
        HostExtensionUpdateDenied, match="signed identity or requirements"
    ):
        _verify(host_successor, signed_manifest=signed)


def test_new_host_contribution_is_rejected(host_successor: HostPair) -> None:
    """A signed new Host contribution cannot expand a predecessor inventory."""
    successor = host_successor[1]
    contribution = {"contract_id": "test.host.expansion.v1", "operation_id": "execute"}
    _write(successor, "host/contribution.json", contribution)
    manifest = _read(successor, "pack.v4.json")
    manifest["artifacts"].append(
        {
            "path": "host/contribution.json",
            "digest": "sha256:" + "0" * 64,
            "kind": "sidecar",
        }
    )
    _write(successor, "pack.v4.json", manifest)
    index = _read(successor, "artifact-index.v4.json")
    index["artifacts"].append(
        {
            "path": "host/contribution.json",
            "digest": "sha256:" + "0" * 64,
            "role": "host_contract_contribution",
        }
    )
    _write(successor, "artifact-index.v4.json", index)
    _refresh(successor)
    _sign(successor)

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(host_successor)


def test_validator_does_not_import_successor_code(host_successor: HostPair) -> None:
    """Compatibility checking never runs candidate module top-level code."""
    successor = host_successor[1]
    (successor / "runtime" / "echo.py").write_text(
        "raise AssertionError('Host code imported during compatibility validation')\n"
    )
    _refresh(successor)
    _sign(successor)

    result = _verify(host_successor)

    assert result.version == "1.1.0"


def test_successor_reference_bytes_must_match_finite_artifact_inventory(
    host_successor: HostPair,
) -> None:
    """Changed executable bytes fail before the compatibility comparison."""
    (host_successor[1] / "runtime" / "echo.py").write_text(
        "# changed after signature\n"
    )

    with pytest.raises(HostExtensionUpdateDenied, match="artifact bytes differ"):
        _verify(host_successor)


def test_host_successor_requires_signed_envelope(host_successor: HostPair) -> None:
    """An unsigned root cannot inherit an existing Host Extension boundary."""

    (host_successor[1] / SIGNED_MANIFEST_RELATIVE).unlink()

    with pytest.raises(
        HostExtensionUpdateDenied, match="signed manifest is unavailable"
    ):
        _verify(host_successor)


def test_signed_update_cannot_rename_exported_function(
    host_successor: HostPair,
) -> None:
    """Even a valid new Function implementation must keep the exact export ID."""

    successor = host_successor[1]
    manifest = _read(successor, "pack.v4.json")
    executable = _read(successor, "executables.v4.json")
    manifest["functions"][0]["id"] = "conformance.echo.replaced"
    executable["variants"][0]["function_id"] = "conformance.echo.replaced"
    _write(successor, "pack.v4.json", manifest)
    _write(successor, "executables.v4.json", executable)
    _refresh(successor)
    _sign(successor)

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(host_successor)


def test_signed_update_cannot_change_contract_schema(host_successor: HostPair) -> None:
    """A fully pinned schema change still changes the Host Contract closure."""

    successor = host_successor[1]
    contracts = _read(successor, "contracts.v4.json")
    contract = contracts["contracts"][0]
    schema = {"type": "object"}
    schema_digest = canonical_digest(schema)
    contract["schema_catalog"][schema_digest] = schema
    contract["operations"][0]["input_schema_digest"] = schema_digest
    revision_digest = canonical_digest(
        {key: value for key, value in contract.items() if key != "revision_digest"}
    )
    contract["revision_digest"] = revision_digest
    _write(successor, "contracts.v4.json", contracts)
    manifest = _read(successor, "pack.v4.json")
    manifest["contracts"][0]["revision_digest"] = revision_digest
    manifest["functions"][0]["contract_revision_digest"] = revision_digest
    _write(successor, "pack.v4.json", manifest)
    executable = _read(successor, "executables.v4.json")
    executable["variants"][0]["operations"][0]["input_schema"] = schema
    executable["variants"][0]["operations"][0]["revision_digest"] = revision_digest
    _write(successor, "executables.v4.json", executable)
    _refresh(successor)
    _sign(successor)

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(host_successor)


def test_signed_update_cannot_change_declared_host_contribution(
    host_successor: HostPair,
) -> None:
    """An existing contribution descriptor stays pinned across code updates."""

    previous, successor, _previous_digest = host_successor
    for root in (previous, successor):
        _write(root, "host/contribution.json", {"effect_ceiling": ["operation.invoke"]})
        manifest = _read(root, "pack.v4.json")
        manifest["artifacts"].append(
            {
                "path": "host/contribution.json",
                "digest": "sha256:" + "0" * 64,
                "kind": "sidecar",
            }
        )
        _write(root, "pack.v4.json", manifest)
        index = _read(root, "artifact-index.v4.json")
        index["artifacts"].append(
            {
                "path": "host/contribution.json",
                "digest": "sha256:" + "0" * 64,
                "role": "host_contract_contribution",
            }
        )
        _write(root, "artifact-index.v4.json", index)
        _refresh(root)
    _sign(successor)
    pair = (previous, successor, compile_pack_root(previous).artifact.digest)
    _verify(pair)
    _write(successor, "host/contribution.json", {"effect_ceiling": ["host:root"]})
    _refresh(successor)
    _sign(successor)

    with pytest.raises(HostExtensionUpdateDenied, match="authority closure"):
        _verify(pair)


def test_signed_host_pack_cannot_switch_to_pack_vm(host_successor: HostPair) -> None:
    """Signed mixed-execution metadata fails the verified compiler boundary."""

    _change_document(
        host_successor[1],
        "executables.v4.json",
        lambda doc: doc["variants"][0].__setitem__("execution_kind", "pack_vm"),
    )

    with pytest.raises(InvalidArtifactError, match="execution kind does not match"):
        _verify(host_successor)


def test_host_successor_does_not_follow_runtime_symlink(
    host_successor: HostPair,
) -> None:
    """Finite artifact reads cannot be redirected outside the selected Pack."""

    successor = host_successor[1]
    runtime = successor / "runtime" / "echo.py"
    outside = successor.parent / "outside.py"
    outside.write_bytes(runtime.read_bytes())
    runtime.unlink()
    runtime.symlink_to(outside)

    with pytest.raises(OSError):
        _verify(host_successor)


def test_identical_signed_host_revision_is_idempotent(host_successor: HostPair) -> None:
    """Reverifying the exact retained revision never mutates its identity."""

    successor = host_successor[1]
    digest = compile_pack_root(successor).artifact.digest

    result = _verify((successor, successor, digest))

    assert result.predecessor_digest == result.artifact_digest == digest
    assert result.predecessor_version == result.version == "1.1.0"


def test_same_host_artifact_cannot_claim_new_version(host_successor: HostPair) -> None:
    """Version-only manifest changes cannot alias an already pinned digest."""

    previous, successor, previous_digest = host_successor
    shutil.copyfile(previous / "runtime" / "echo.py", successor / "runtime" / "echo.py")
    _refresh(successor)
    _sign(successor)
    assert compile_pack_root(successor).artifact.digest == previous_digest

    with pytest.raises(HostExtensionUpdateDenied, match="artifact version differs"):
        _verify(host_successor)


@pytest.mark.parametrize(
    ("previous_version", "successor_version", "allowed"),
    [
        ("1.0.0", "1.0.0-1", False),
        ("1.0.0", "1.0.0+build.2", False),
        ("1.0.0-10", "1.0.0-2", False),
        ("1.0.0-2", "1.0.0-10", True),
        ("1.0.0-1", "1.0.0", True),
        ("1.0.0-1+build.1", "1.0.0-1+build.2", False),
        ("1.0.0-1", "1.0.0-01", False),
        ("1.0.0-01", "1.0.0-1", False),
    ],
)
def test_host_successors_use_semver_precedence(
    host_successor: HostPair,
    previous_version: str,
    successor_version: str,
    allowed: bool,
) -> None:
    """Prereleases are older than stable releases; build tags add no precedence."""

    previous, successor, _previous_digest = host_successor
    for root, version in ((previous, previous_version), (successor, successor_version)):
        manifest = _read(root, "pack.v4.json")
        manifest["pack"]["version"] = version
        _write(root, "pack.v4.json", manifest)
        _refresh(root)
    _sign(successor)
    pair = (previous, successor, compile_pack_root(previous).artifact.digest)

    if allowed:
        assert _verify(pair).version == successor_version
    else:
        with pytest.raises(HostExtensionUpdateDenied, match="newer version"):
            _verify(pair)
