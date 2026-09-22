"""Fail-closed Host-to-PackVM artifact materialization regressions."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import shutil
import sys

import pytest

from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner,
)
import tobkiri_host.artifact_materialization as materialization_module
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.artifact_materialization import capture_materialized_artifact
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
