"""Fail-closed Host-to-PackVM artifact materialization regressions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner,
)
import ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner as vz_provisioner
import tobkiri_host.artifact_materialization as materialization_module
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.artifact_materialization import (
    MaterializedArtifactFile,
    MaterializedPackArtifact,
    _materialization_digest,
    capture_materialized_artifact,
)
from tobkiri_host.contracts import OperationCatalog, OperationRoute
from tobkiri_host.errors import InvalidArtifactError
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_digest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = RUNTIME_ROOT / "tests" / "fixtures" / "conformance_minimal_echo_pack"
PACK_ID = "conformance.minimal.echo"


@pytest.fixture(autouse=True)
def _ample_guest_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        packvm_guest_runner.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 8 * 1024**3})(),
    )


def _copied_binding(tmp_path: Path):
    root = tmp_path / PACK_ID
    shutil.copytree(FIXTURE, root)
    compiled = compile_pack_root(root)
    (contract_id, operation_id), metadata = next(iter(compiled.routes.items()))
    function = compiled.artifact.functions[0]
    route = OperationRoute(
        contract_id=contract_id,
        operation_id=operation_id,
        artifact_digest=compiled.artifact.digest,
        function_id=function.function_id,
        variant_id=str(metadata["variant_id"]),
        catalog_digest=str(compiled.artifact.catalog_digest),
        platform=str(metadata["platform"]),
        architecture=str(metadata["architecture"]),
        runtime_abi=str(metadata["runtime_abi"]),
        backend=str(metadata["backend"]),
        execution_kind=str(metadata["execution_kind"]),
        domain_kind=str(metadata["domain_kind"]),
        execution_domain_profile=str(metadata["execution_domain_profile"]),
        materialization_mode=str(metadata["materialization_mode"]),
        target_principal_ref=OpaqueAuthorityRef("authority:materialization-test"),
    )
    binding = OperationCatalog((compiled.artifact,), (route,)).resolve(
        contract_id,
        operation_id,
        ">=1,<2",
    )
    return root, binding


def _seed_artifact(source: bytes) -> MaterializedPackArtifact:
    """Create one compact artifact used by direct-VZ seed regressions."""

    implementation_digest = "sha256:" + hashlib.sha256(source).hexdigest()
    artifact_digest = "sha256:" + "a" * 64
    files = (
        MaterializedArtifactFile(
            path="runtime/operation.py",
            digest=implementation_digest,
            executable=False,
            content=source,
        ),
    )
    materialization_digest = _materialization_digest(
        "seed-pack",
        artifact_digest,
        "seed-pack.operation",
        implementation_digest,
        "runtime/operation.py",
        files,
    )
    return MaterializedPackArtifact(
        pack_id="seed-pack",
        artifact_digest=artifact_digest,
        function_id="seed-pack.operation",
        implementation_digest=implementation_digest,
        implementation_path="runtime/operation.py",
        materialization_digest=materialization_digest,
        root_device=1,
        root_inode=1,
        files=files,
    )


def _seed_bindings(artifact: MaterializedPackArtifact) -> dict[str, str]:
    """Return the complete direct-VZ binding map for a seed test."""

    return {
        "domain": "sha256:" + "1" * 64,
        "lease": "sha256:" + "2" * 64,
        "reservation": "sha256:" + "3" * 64,
        "image": "sha256:" + "4" * 64,
        "agent": "sha256:" + "5" * 64,
        "config": "sha256:" + "6" * 64,
        "disk": "sha256:" + "7" * 64,
        "guest_public_key": "sha256:" + "8" * 64,
        "efi_variable_store": "sha256:" + "9" * 64,
        "artifact": artifact.artifact_digest,
        "executable": artifact.implementation_digest,
        "materialization": artifact.materialization_digest,
    }


def test_capture_contains_only_digest_pinned_regular_files(tmp_path: Path) -> None:
    root, binding = _copied_binding(tmp_path)
    captured = capture_materialized_artifact(root, binding)
    assert captured.pack_id == PACK_ID
    assert captured.artifact_digest == binding.artifact.digest
    assert captured.implementation_digest == binding.function.implementation_digest
    assert all(not Path(item.path).is_absolute() for item in captured.files)
    assert {item.path for item in captured.files} >= {
        "artifact-index.v4.json",
        "contracts.v4.json",
        "executables.v4.json",
        "pack.v4.json",
        "runtime/echo.py",
    }
    request = captured.request_payload(nonce="a" * 64)
    assert "host_path" not in request
    assert "pack_root" not in request


def test_capture_rejects_symlink_and_wrong_digest(tmp_path: Path) -> None:
    root, binding = _copied_binding(tmp_path)
    runtime = root / "runtime" / "echo.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(runtime.read_bytes())
    runtime.unlink()
    runtime.symlink_to(outside)
    with pytest.raises(InvalidArtifactError, match="unavailable"):
        capture_materialized_artifact(root, binding)

    runtime.unlink()
    runtime.write_text("tampered = True\n", encoding="utf-8")
    with pytest.raises(InvalidArtifactError, match="digest"):
        capture_materialized_artifact(root, binding)


def test_capture_rejects_pack_root_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, binding = _copied_binding(tmp_path)
    replacement = tmp_path / "replacement"
    shutil.copytree(FIXTURE, replacement)
    original_reader = materialization_module._read_regular_file
    swapped = False

    def swap_after_first_read(descriptor: int, relative: str):
        nonlocal swapped
        result = original_reader(descriptor, relative)
        if not swapped:
            swapped = True
            root.rename(tmp_path / "original")
            replacement.rename(root)
        return result

    monkeypatch.setattr(
        materialization_module,
        "_read_regular_file",
        swap_after_first_read,
    )
    with pytest.raises(InvalidArtifactError, match="root changed"):
        capture_materialized_artifact(root, binding)


def test_capture_rejects_hardlinked_indexed_file(tmp_path: Path) -> None:
    root, binding = _copied_binding(tmp_path)
    runtime = root / "runtime" / "echo.py"
    outside = tmp_path / "outside.py"
    runtime.rename(outside)
    os.link(outside, runtime)

    with pytest.raises(InvalidArtifactError, match="regular file"):
        capture_materialized_artifact(root, binding)


def test_windows_capture_uses_bounded_pinned_reader_without_path_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, binding = _copied_binding(tmp_path)
    reads: list[tuple[str, int | None]] = []

    class FakeSecureDirectory:
        def __init__(self, path: Path, *, create: bool) -> None:
            assert path == root
            assert create is False

        def read_bytes_bounded(
            self,
            relative: str,
            *,
            max_bytes: int | None,
        ) -> bytes:
            reads.append((relative, max_bytes))
            return (root / relative).read_bytes()

    monkeypatch.setattr(
        materialization_module,
        "_requires_windows_secure_reader",
        lambda: True,
    )
    monkeypatch.setattr(
        materialization_module,
        "SecureDirectory",
        FakeSecureDirectory,
    )

    captured = capture_materialized_artifact(root, binding)

    assert {relative for relative, _limit in reads} == {item.path for item in captured.files}
    assert all(
        limit == materialization_module._MAX_MATERIALIZED_FILE_BYTES for _relative, limit in reads
    )


def test_windows_capture_fails_closed_when_pinned_read_detects_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, binding = _copied_binding(tmp_path)

    class ReplacedSecureDirectory:
        def __init__(self, _path: Path, *, create: bool) -> None:
            assert create is False

        def read_bytes_bounded(
            self,
            relative: str,
            *,
            max_bytes: int | None,
        ) -> bytes:
            assert max_bytes == materialization_module._MAX_MATERIALIZED_FILE_BYTES
            if relative == "runtime/echo.py":
                raise materialization_module.SecurePersistenceError(
                    "persistence entry changed during read"
                )
            return (root / relative).read_bytes()

    monkeypatch.setattr(
        materialization_module,
        "_requires_windows_secure_reader",
        lambda: True,
    )
    monkeypatch.setattr(
        materialization_module,
        "SecureDirectory",
        ReplacedSecureDirectory,
    )

    with pytest.raises(InvalidArtifactError, match="file is unavailable"):
        capture_materialized_artifact(root, binding)


def test_guest_stage_is_read_only_replay_safe_and_reverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, binding = _copied_binding(tmp_path)
    captured = capture_materialized_artifact(root, binding)
    guest_root = tmp_path / "guest-artifacts"
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", guest_root)
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)
    request = captured.request_payload(nonce="a" * 64)
    response = packvm_guest_runner._materialize(request)
    assert response["ok"] is True
    identity = str(response["guest_artifact_identity"])
    with pytest.raises(ValueError, match="replay"):
        packvm_guest_runner._materialize(request)
    retry = packvm_guest_runner._materialize(captured.request_payload(nonce="b" * 64))
    assert retry["guest_artifact_identity"] == identity

    invoke = {
        "artifact_digest": captured.artifact_digest,
        "materialization_digest": captured.materialization_digest,
        "guest_artifact_identity": identity,
    }
    assert packvm_guest_runner._verify_invocation_artifact(invoke) == identity
    target = (
        guest_root
        / captured.artifact_digest.removeprefix("sha256:")
        / captured.materialization_digest.removeprefix("sha256:")
    )
    runtime = target / "runtime" / "echo.py"
    target.chmod(0o700)
    runtime.parent.chmod(0o700)
    runtime.chmod(0o600)
    runtime.write_text("tampered = True\n", encoding="utf-8")
    runtime.chmod(0o444)
    runtime.parent.chmod(0o555)
    target.chmod(0o555)
    with pytest.raises(ValueError, match="digest changed"):
        packvm_guest_runner._verify_invocation_artifact(invoke)
    expected_runtime = next(
        item.content for item in captured.files if item.path == "runtime/echo.py"
    )
    target.chmod(0o700)
    runtime.parent.chmod(0o700)
    runtime.chmod(0o600)
    runtime.write_bytes(expected_runtime)
    runtime.chmod(0o444)
    extra = target / "unexpected.py"
    extra.write_text("pass\n", encoding="utf-8")
    extra.chmod(0o444)
    runtime.parent.chmod(0o555)
    target.chmod(0o555)
    with pytest.raises(ValueError, match="inventory changed"):
        packvm_guest_runner._verify_invocation_artifact(invoke)

    target.chmod(0o700)
    runtime.parent.chmod(0o700)
    extra.unlink()
    runtime.unlink()
    outside = tmp_path / "same-bytes-outside-artifact.py"
    outside.write_bytes(expected_runtime)
    outside.chmod(0o444)
    os.link(outside, runtime)
    runtime.parent.chmod(0o555)
    target.chmod(0o555)
    with pytest.raises(ValueError, match="unsafe file"):
        packvm_guest_runner._verify_invocation_artifact(invoke)


def test_direct_vz_seed_materializes_before_the_first_real_invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct VZ launch attests the exact pre-seeded invoke path."""

    source = (
        b"def tobkiri_packvm_invoke(operation_id, payload):\n"
        b"    return {'operation': operation_id, 'message': payload['message']}\n"
    )
    artifact = _seed_artifact(source)
    seed = tmp_path / "artifact-seed.v1.bin"
    seed_binding = vz_provisioner._write_materialized_artifact_seed(seed, artifact)
    guest_root = tmp_path / "guest-artifacts"
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", guest_root)
    monkeypatch.setattr(packvm_guest_runner, "REQUEST_ROOT", tmp_path / "requests")
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)
    identity = packvm_guest_runner.materialize_seed_artifact(
        seed,
        seed_binding,
        _seed_bindings(artifact),
    )
    assert packvm_guest_runner._seeded_artifact_identity(_seed_bindings(artifact)) == identity
    monkeypatch.setattr(
        packvm_guest_runner,
        "_sandbox_argv",
        lambda _target, implementation: (
            sys.executable,
            "-I",
            "-S",
            str(Path(packvm_guest_runner.__file__).resolve()),
            "--execute",
            str(implementation),
        ),
    )
    result = packvm_guest_runner._invoke(
        {
            "operation": "invoke",
            "request_id": "request.seeded",
            "target_domain": "packvm:seeded",
            "artifact_digest": artifact.artifact_digest,
            "materialization_digest": artifact.materialization_digest,
            "guest_artifact_identity": identity,
            "contract_id": "example.contract.v1",
            "contract_version": "1.0.0",
            "operation_id": "seed-pack.inspect",
            "payload": {"message": "seeded before boot"},
            "request_digest": "sha256:" + "b" * 64,
            # Direct-VZ envelopes use the Host's canonical JSON deadline
            # string; first invoke must accept it after artifact seeding.
            "deadline_monotonic": "100",
            "cancel_token": "c" * 64,
        }
    )

    assert result["payload"] == {
        "operation": "seed-pack.inspect",
        "message": "seeded before boot",
    }


def test_direct_vz_iso_seed_round_trip_preserves_framing_and_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CIDATA preserves the Host seed bytes consumed before the first invoke."""

    artifact = _seed_artifact(
        b"def tobkiri_packvm_invoke(*_): return {'seed': 'cidata'}\n"
    )
    host_seed = tmp_path / "host-artifact-seed.v1.bin"
    seed_binding = vz_provisioner._write_materialized_artifact_seed(host_seed, artifact)
    config_seed = tmp_path / "config-seed.iso"
    vz_provisioner._write_iso_seed(
        config_seed,
        "cidata",
        {"artifact-seed.v1.bin": host_seed},
    )

    image = config_seed.read_bytes()
    directory_offset = 20 * 2048
    entry_offset = directory_offset
    artifact_bytes: bytes | None = None
    while entry_offset < directory_offset + 2048:
        record_length = image[entry_offset]
        if record_length == 0:
            break
        identifier_length = image[entry_offset + 32]
        identifier = image[
            entry_offset + 33 : entry_offset + 33 + identifier_length
        ]
        if identifier == b"artifact-seed.v1.bin;1":
            sector = int.from_bytes(image[entry_offset + 2 : entry_offset + 6], "little")
            size = int.from_bytes(image[entry_offset + 10 : entry_offset + 14], "little")
            artifact_bytes = image[sector * 2048 : sector * 2048 + size]
            break
        entry_offset += record_length

    assert artifact_bytes == host_seed.read_bytes()
    guest_seed = tmp_path / "artifact-seed.v1.bin"
    guest_seed.write_bytes(artifact_bytes)
    guest_seed.chmod(0o600)
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", tmp_path / "guest-artifacts")
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)

    identity = packvm_guest_runner.materialize_seed_artifact(
        guest_seed,
        seed_binding,
        _seed_bindings(artifact),
    )

    assert identity == packvm_guest_runner._seeded_artifact_identity(
        _seed_bindings(artifact)
    )


def test_direct_vz_seed_rejects_tampering_and_binding_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed bytes and their launch binding are independently fail-closed."""

    artifact = _seed_artifact(b"def tobkiri_packvm_invoke(*_): return {}\n")
    seed = tmp_path / "artifact-seed.v1.bin"
    seed_binding = vz_provisioner._write_materialized_artifact_seed(seed, artifact)
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", tmp_path / "guest-artifacts")
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)

    mismatched = _seed_bindings(artifact)
    mismatched["artifact"] = "sha256:" + "d" * 64
    with pytest.raises(ValueError, match="binding mismatch"):
        packvm_guest_runner.materialize_seed_artifact(seed, seed_binding, mismatched)

    seed.chmod(0o600)
    seed.write_bytes(seed.read_bytes() + b"tampered")
    seed.chmod(0o600)
    with pytest.raises(ValueError, match="unsafe|changed|trailing"):
        packvm_guest_runner.materialize_seed_artifact(
            seed,
            seed_binding,
            _seed_bindings(artifact),
        )


def test_direct_vz_seed_rejects_traversal_before_writing_guest_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest-valid archive still cannot select a path outside its target."""

    artifact_digest = "sha256:" + "e" * 64
    executable_digest = "sha256:" + "f" * 64
    materialization_digest = "sha256:" + "0" * 64
    manifest = {
        "schema": packvm_guest_runner.ARTIFACT_SEED_SCHEMA,
        "pack_id": "seed-pack",
        "artifact_digest": artifact_digest,
        "function_id": "seed-pack.operation",
        "implementation_digest": executable_digest,
        "implementation_path": "runtime/operation.py",
        "materialization_digest": materialization_digest,
        "files": [
            {
                "path": "../escape.py",
                "digest": executable_digest,
                "executable": False,
                "size": 0,
            }
        ],
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    payload = (
        packvm_guest_runner.ARTIFACT_SEED_MAGIC
        + len(encoded).to_bytes(8, "big")
        + encoded
    )
    seed = tmp_path / "artifact-seed.v1.bin"
    seed.write_bytes(payload)
    seed.chmod(0o600)
    binding = {
        **_seed_bindings(_seed_artifact(b"x")),
        "artifact": artifact_digest,
        "executable": executable_digest,
        "materialization": materialization_digest,
    }
    seed_binding = {
        "format": packvm_guest_runner.ARTIFACT_SEED_SCHEMA,
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    guest_root = tmp_path / "guest-artifacts"
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", guest_root)
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)

    with pytest.raises(ValueError, match="path is unsafe"):
        packvm_guest_runner.materialize_seed_artifact(seed, seed_binding, binding)
    assert not list(guest_root.glob("*/*"))


def test_guest_materialization_rejects_storage_quota_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, binding = _copied_binding(tmp_path)
    captured = capture_materialized_artifact(root, binding)
    guest_root = tmp_path / "guest-artifacts"
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", guest_root)
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(packvm_guest_runner, "MAX_ARTIFACT_STORAGE_BYTES", 1)

    with pytest.raises(ValueError, match="storage quota exceeded"):
        packvm_guest_runner._materialize(captured.request_payload(nonce="a" * 64))


def test_guest_materialization_enforces_cumulative_storage_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, binding = _copied_binding(tmp_path)
    captured = capture_materialized_artifact(root, binding)
    guest_root = tmp_path / "guest-artifacts"
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", guest_root)
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)
    first = captured.request_payload(nonce="a" * 64)
    packvm_guest_runner._materialize(first)

    second = dict(captured.request_payload(nonce="b" * 64))
    second["pack_id"] = "alternate-pack"
    files = second["files"]
    assert isinstance(files, list)
    total = sum(len(base64.b64decode(str(item["content"]))) for item in files)
    second["materialization_digest"] = canonical_digest(
        {
            "pack_id": second["pack_id"],
            "artifact_digest": second["artifact_digest"],
            "function_id": second["function_id"],
            "implementation_digest": second["implementation_digest"],
            "implementation_path": second["implementation_path"],
            "files": [
                {
                    "path": item["path"],
                    "digest": item["digest"],
                    "executable": item["executable"],
                    "size": len(base64.b64decode(str(item["content"]))),
                }
                for item in files
            ],
        }
    )
    stored = packvm_guest_runner._artifact_storage_bytes()
    monkeypatch.setattr(
        packvm_guest_runner,
        "MAX_ARTIFACT_STORAGE_BYTES",
        stored + total + packvm_guest_runner.MAX_ARTIFACT_METADATA_BYTES - 1,
    )

    with pytest.raises(ValueError, match="storage quota exceeded"):
        packvm_guest_runner._materialize(second)


def test_guest_materialization_preserves_free_space_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, binding = _copied_binding(tmp_path)
    captured = capture_materialized_artifact(root, binding)
    guest_root = tmp_path / "guest-artifacts"
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", guest_root)
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        packvm_guest_runner.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )

    with pytest.raises(ValueError, match="guest free space is insufficient"):
        packvm_guest_runner._materialize(captured.request_payload(nonce="a" * 64))


def test_guest_supervisor_materializes_and_invokes_the_exact_python_abi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        b"def tobkiri_packvm_invoke(operation_id, payload):\n"
        b"    return {'operation': operation_id, 'message': payload['message']}\n"
    )
    file_digest = "sha256:" + hashlib.sha256(source).hexdigest()
    artifact_digest = "sha256:" + "1" * 64
    inventory = [
        {
            "path": "runtime/operation.py",
            "digest": file_digest,
            "executable": False,
            "size": len(source),
        }
    ]
    materialization_digest = canonical_digest(
        {
            "pack_id": "example-pack",
            "artifact_digest": artifact_digest,
            "function_id": "example-pack.operation",
            "implementation_digest": file_digest,
            "implementation_path": "runtime/operation.py",
            "files": inventory,
        }
    )
    guest_root = tmp_path / "guest-artifacts"
    monkeypatch.setattr(packvm_guest_runner, "ARTIFACT_ROOT", guest_root)
    monkeypatch.setattr(packvm_guest_runner, "REQUEST_ROOT", tmp_path / "requests")
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        packvm_guest_runner,
        "_sandbox_argv",
        lambda _target, implementation: (
            sys.executable,
            "-I",
            "-S",
            str(Path(packvm_guest_runner.__file__).resolve()),
            "--execute",
            str(implementation),
        ),
    )
    staged = packvm_guest_runner._materialize(
        {
            "operation": "materialize",
            "pack_id": "example-pack",
            "artifact_digest": artifact_digest,
            "function_id": "example-pack.operation",
            "implementation_digest": file_digest,
            "implementation_path": "runtime/operation.py",
            "materialization_digest": materialization_digest,
            "materialization_nonce": "a" * 64,
            "files": [
                {
                    "path": "runtime/operation.py",
                    "digest": file_digest,
                    "executable": False,
                    "content": base64.b64encode(source).decode("ascii"),
                }
            ],
        }
    )
    result = packvm_guest_runner._invoke(
        {
            "operation": "invoke",
            "request_id": "request.test",
            "target_domain": "packvm:test",
            "artifact_digest": artifact_digest,
            "materialization_digest": materialization_digest,
            "guest_artifact_identity": staged["guest_artifact_identity"],
            "contract_id": "example.contract.v1",
            "contract_version": "1.0.0",
            "operation_id": "example-pack.inspect",
            "payload": {"message": "inside guest"},
            "request_digest": "sha256:" + "2" * 64,
            "deadline_monotonic": 100.0,
            "cancel_token": "c" * 64,
        }
    )
    assert result["ok"] is True
    assert result["payload"] == {
        "operation": "example-pack.inspect",
        "message": "inside guest",
    }
