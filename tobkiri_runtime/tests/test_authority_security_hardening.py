from __future__ import annotations

import json
import hashlib
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.authority.principal import (
    UNATTRIBUTED_PRINCIPAL_ID,
    build_principal_id,
    principal_scope_candidates,
)
from core_runtime.pack_artifact_integrity import (
    verify_declared_artifacts,
    write_host_install_record,
)
from core_runtime.resolved_profile import _manifest_contract_metadata
from ecosystem.rumi_agent_runtime_service_pack.runtime import runtime
from ecosystem.rumi_agent_state_store_pack.runtime.store import (
    AgentStateStore,
)
from ecosystem.rumi_git_publish_pack.runtime.publish import (
    _arguments as publish_arguments,
)
from ecosystem.rumi_git_publish_pack.runtime.publish import GitPublishService
from ecosystem.rumi_git_publish_pack.runtime import publish as git_publish
from ecosystem.rumi_git_read_pack.runtime.read import GitReadService
from ecosystem.rumi_git_read_pack.runtime import read as git_read
from ecosystem.rumi_git_write_pack.runtime.write import _arguments as write_arguments
from ecosystem.rumi_git_write_pack.runtime.write import GitWriteService
from ecosystem.rumi_git_write_pack.runtime import write as git_write
from ecosystem.rumi_host_authority_bridge_pack.runtime import bridge
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import OpaqueInvocationLease


def test_missing_legacy_principal_is_neutral_instead_of_defaultspack() -> None:
    principal_id = build_principal_id()

    assert principal_id == UNATTRIBUTED_PRINCIPAL_ID
    assert "defaultspack" not in principal_scope_candidates("")


def _authenticated_host_context() -> SimpleNamespace:
    """Build the Host-only envelope used by the receipt boundary test."""

    return SimpleNamespace(
        envelope=RequestEnvelope(
            context=RequestContext(
                request_id="authority-test-request",
                trace_id="authority-test-trace",
                caller_principal=OpaqueAuthorityRef("host-caller"),
                profile_id="host-profile",
                activation_id="host-activation",
                activation_digest="sha256:" + "a" * 64,
                plan_digest="sha256:" + "b" * 64,
                security_epoch=1,
                caller_session_id="host-session",
                caller_domain_id="host-caller-domain",
                caller_boot_epoch=1,
                target_domain_id="host-target-domain",
                target_boot_epoch=1,
                target_backend_digest="sha256:" + "c" * 64,
                profile_authority_digest="sha256:" + "d" * 64,
                fencing_token=1,
                handle_namespace="host-handles",
            ),
            target_principal=OpaqueAuthorityRef("host-target"),
            target_domain=OpaqueAuthorityRef("host-target-domain"),
            contract_id="host.authority.v1",
            contract_version="1.0.0",
            operation_id="authorize",
            payload={},
            request_digest="sha256:" + "e" * 64,
            deadline_monotonic=time.monotonic() + 30,
            lease=OpaqueInvocationLease(b"host-lease"),
            idempotency_key=None,
        ),
        caller_pack_id="host-caller-pack",
        caller_function_id="host-caller-function",
        profile_revision="host-profile-revision",
        workspace_id="host-workspace",
    )


def test_unsigned_nonbuiltin_pack_requires_host_install_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUMI_PACK_PUBLISHER_TRUST_STORE", raising=False)
    ok, diagnostics = verify_declared_artifacts(
        tmp_path,
        {"id": "third_party", "version": "1.0.0"},
    )
    assert ok is False
    assert "Host install record" in diagnostics[0]


def test_unsigned_nonbuiltin_requires_explicit_host_developer_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_root = tmp_path / "pack"
    pack_root.mkdir()
    trust_store = tmp_path / "trust.json"
    write_host_install_record(
        trust_store,
        pack_id="third_party",
        install_path=pack_root,
        record={
            "signature_required": False,
            "developer_mode": True,
            "publisher_id": "",
            "key_id": "",
            "installed_version": "1.0.0",
            "signed_manifest_path": "",
            "contract_versions": {},
            "requested_capabilities": [],
        },
    )
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(trust_store))
    monkeypatch.setenv("RUMI_PACK_DEVELOPER_MODE", "1")
    ok, diagnostics = verify_declared_artifacts(
        pack_root,
        {"id": "third_party", "version": "1.0.0"},
    )
    assert ok is True
    assert diagnostics == ()


def test_provider_trust_uses_only_host_attestation() -> None:
    manifest = {
        "version": "1.0.0",
        "_v3_manifest": {
            "provenance": {
                "content_hash": "sha256:" + "a" * 64,
                "build_identity": "fixture",
                "trust_class": "system",
            },
            "contracts": {
                "provides": [
                    {
                        "id": "rumi.service.fixture.v1",
                        "version": "1.0.0",
                        "provider_instance_id": "fixture.provider",
                        "cardinality": "one",
                        "security": "internal",
                        "failure": "fail_closed",
                        "lifecycle": {
                            "introduced": "1.0.0",
                            "deprecated": False,
                        },
                        "schemas": {},
                        "isolation": "process",
                    }
                ]
            },
        },
    }
    providers, _, diagnostics = _manifest_contract_metadata(
        ("third-party",),
        {"third-party": manifest},
        verified_pack_trust={"third-party": "verified"},
    )
    assert not diagnostics
    assert providers[0].trust_class == "verified"


def test_agent_effect_commit_barrier_is_atomic(tmp_path: Path) -> None:
    store = AgentStateStore("default", root=tmp_path)
    begun = store.apply(
        "run.begin",
        {
            "expected_revision": 0,
            "run_id": "run-1",
            "idempotency_key": "key-1",
            "agent_profile_id": "default",
            "conversation_id": "conversation-1",
            "turn_id": "turn-1",
            "parent_run_id": "",
        },
    )
    store.apply(
        "run.transition",
        {
            "expected_revision": begun["revision"],
            "run_id": "run-1",
            "status": "running",
            "step": 0,
            "details": {},
        },
    )
    effect = store.apply(
        "run.effect.begin",
        {
            "expected_revision": 2,
            "run_id": "run-1",
            "executor_token": "executor-secret",
            "effect_receipt": "sha256:effect",
        },
    )
    cancelled = store.apply(
        "run.cancel",
        {
            "expected_revision": effect["revision"],
            "run_id": "run-1",
            "reason": "stop",
        },
    )
    assert cancelled["too_late"] is True
    assert cancelled["run"]["cancel_requested"] is True
    assert cancelled["run"]["status"] == "running"


def test_raw_tool_payload_is_ephemeral_and_one_shot() -> None:
    receipt = runtime._stash_ephemeral_tool_payload(
        {"pending_tool_intents": [{"arguments": {"secret": "value"}}]}
    )
    assert "secret" not in receipt
    assert runtime._load_ephemeral_tool_payload(receipt)["pending_tool_intents"]
    with pytest.raises(RuntimeError):
        runtime._load_ephemeral_tool_payload(receipt)


def test_git_mutations_require_pinned_snapshot() -> None:
    with pytest.raises(ValueError):
        write_arguments("stage", {"paths": ["a.txt"]})
    with pytest.raises(ValueError):
        publish_arguments({"branch": "main"}, dry_run=False)


def test_git_commit_uses_isolated_index_and_ref_cas(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    target = tmp_path / "file.txt"
    target.write_text("before\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "initial"],
        check=True,
    )
    target.write_text("after\n")

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args],
            text=True,
        )

    class Client:
        def invoke(self, contract: str, name: str, payload: object) -> dict[str, object]:
            if contract.endswith("workspace.v1"):
                return {
                    "root_path": str(tmp_path),
                    "mount_revision": 1,
                }
            if contract.endswith("git.read.v1"):
                return {"repository_root": "."}
            return {"authorized": True}

    snapshot = GitReadService(Client()).invoke(
        "snapshot",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            "capture_commit": True,
        },
    )
    result = GitWriteService(Client()).invoke(
        "commit",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            "message": "isolated commit",
            "expected_mount_revision": 1,
            **snapshot,
        },
    )
    assert result["commit_hash"] == git("rev-parse", "HEAD").strip()


class _GitServiceClient:
    """Minimal Host client with an optional deterministic authorization race."""

    def __init__(self, root: Path, *, after_redeem=None) -> None:
        self.root = root
        self.after_redeem = after_redeem

    def invoke(self, contract: str, name: str, payload: object) -> dict[str, object]:
        if contract.endswith("workspace.v1"):
            return {"root_path": str(self.root), "mount_revision": 1}
        if contract.endswith("git.read.v1"):
            return {"repository_root": "."}
        if contract.endswith("authorize.v1") and name == "redeem":
            if self.after_redeem is not None:
                self.after_redeem()
            return {"authorized": True}
        return {"authorized": True}


def _git_snapshot(
    root: Path,
    *,
    paths: list[str] | None = None,
    capture_commit: bool = False,
    all_tracked: bool = False,
    branch: str | None = None,
    source: str | None = None,
) -> dict[str, object]:
    client = _GitServiceClient(root)
    request: dict[str, object] = {"profile_id": "default", "workspace_id": "workspace"}
    if paths is not None:
        request["paths"] = paths
    if capture_commit:
        request["capture_commit"] = True
        request["all_tracked"] = all_tracked
    if branch is not None:
        request["branch"] = branch
    if source is not None:
        request["source"] = source
    snapshot = GitReadService(client).invoke("snapshot", request)
    return {**snapshot, "expected_mount_revision": 1}


def _init_git_repo_with_commit(
    root: Path,
    content: str = "before\n",
    *,
    object_format: str | None = None,
) -> None:
    init_args = ["git", "init", "-q", "-b", "main"]
    if object_format is not None:
        init_args.append(f"--object-format={object_format}")
    init_args.append(str(root))
    initialized = subprocess.run(
        init_args,
        capture_output=object_format is not None,
        text=True,
        check=False,
    )
    if initialized.returncode != 0:
        if object_format == "sha256":
            pytest.skip("installed Git does not support SHA-256 repositories")
        initialized.check_returncode()
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        check=True,
    )
    (root / "file.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "initial"],
        check=True,
    )


def test_git_stage_rejects_precondition_race_before_index_mutation(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    target = tmp_path / "file.txt"
    target.write_text("approved\n", encoding="utf-8")
    snapshot = _git_snapshot(tmp_path, paths=["file.txt"])
    client = _GitServiceClient(
        tmp_path,
        after_redeem=lambda: target.write_text("raced\n", encoding="utf-8"),
    )

    with pytest.raises(PermissionError, match="snapshot changed"):
        GitWriteService(client).invoke(
            "stage",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "paths": ["file.txt"],
                **snapshot,
            },
        )

    staged = subprocess.check_output(["git", "-C", str(tmp_path), "show", ":file.txt"], text=True)
    assert staged == "before\n"


def test_git_commit_rejects_rewritten_path_before_writing_an_object(
    tmp_path: Path,
) -> None:
    """A receipt OID is checked against one effect-boundary raw capture."""

    _init_git_repo_with_commit(tmp_path)
    target = tmp_path / "file.txt"
    target.write_text("approved\n", encoding="utf-8")
    snapshot = _git_snapshot(
        tmp_path,
        paths=["file.txt"],
        capture_commit=True,
    )
    client = _GitServiceClient(
        tmp_path,
        after_redeem=lambda: target.write_text("raced\n", encoding="utf-8"),
    )

    with pytest.raises(PermissionError, match="commit path changed"):
        GitWriteService(client).invoke(
            "commit",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "paths": ["file.txt"],
                "message": "must not commit raced bytes",
                **snapshot,
            },
        )

    assert (
        subprocess.check_output(["git", "-C", str(tmp_path), "show", "HEAD:file.txt"], text=True)
        == "before\n"
    )


def test_git_commit_expands_all_tracked_before_receipt_redemption(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    second = tmp_path / "second.txt"
    second.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "second.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "second"], check=True)
    (tmp_path / "file.txt").write_text("approved one\n", encoding="utf-8")
    second.write_text("approved two\n", encoding="utf-8")
    snapshot = _git_snapshot(tmp_path, capture_commit=True, all_tracked=True)
    client = _GitServiceClient(
        tmp_path,
        after_redeem=lambda: (tmp_path / "file.txt").write_text("raced one\n", encoding="utf-8"),
    )

    with pytest.raises(PermissionError, match="commit path changed"):
        GitWriteService(client).invoke(
            "commit",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "all_tracked": True,
                "message": "all tracked receipt",
                **snapshot,
            },
        )

    assert snapshot["expected_commit_entries"]
    assert (
        subprocess.check_output(["git", "-C", str(tmp_path), "show", "HEAD:file.txt"], text=True)
        == "before\n"
    )


def test_git_commit_rejects_symbolic_head_change_after_receipt(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "branch", "other"], check=True)
    (tmp_path / "file.txt").write_text("approved\n", encoding="utf-8")
    snapshot = _git_snapshot(
        tmp_path,
        paths=["file.txt"],
        capture_commit=True,
    )
    client = _GitServiceClient(
        tmp_path,
        after_redeem=lambda: subprocess.run(
            ["git", "-C", str(tmp_path), "symbolic-ref", "HEAD", "refs/heads/other"],
            check=True,
        ),
    )

    with pytest.raises(PermissionError, match="symbolic HEAD changed"):
        GitWriteService(client).invoke(
            "commit",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "paths": ["file.txt"],
                "message": "bound branch",
                **snapshot,
            },
        )


def test_git_stage_uses_approved_blob_when_path_changes_at_effect_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    target = tmp_path / "file.txt"
    target.write_text("approved\n", encoding="utf-8")
    snapshot = _git_snapshot(tmp_path, paths=["file.txt"])
    real_git_bytes = git_write._git_bytes
    blob_written = False

    def racing_git_bytes(
        repository: Path,
        args: list[str],
        *,
        input_bytes: bytes,
        **kwargs: object,
    ) -> bytes:
        nonlocal blob_written
        if args[:2] == ["hash-object", "-w"]:
            result = real_git_bytes(
                repository,
                args,
                input_bytes=input_bytes,
                **kwargs,
            )
            target.write_text("raced\n", encoding="utf-8")
            blob_written = True
            return result
        return real_git_bytes(
            repository,
            args,
            input_bytes=input_bytes,
            **kwargs,
        )

    monkeypatch.setattr(git_write, "_git_bytes", racing_git_bytes)
    result = GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "stage",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            **snapshot,
        },
    )

    assert result["staged"] == ["file.txt"]
    staged = subprocess.check_output(["git", "-C", str(tmp_path), "show", ":file.txt"], text=True)
    assert staged == "approved\n"
    assert target.read_text(encoding="utf-8") == "raced\n"
    raced_oid = subprocess.check_output(
        ["git", "-C", str(tmp_path), "hash-object", "--no-filters", "file.txt"],
        text=True,
    ).strip()
    assert blob_written is True
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "cat-file", "-e", f"{raced_oid}^{{blob}}"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_git_stage_holds_index_lock_through_compare_and_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    other = tmp_path / "other.txt"
    other.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "other.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "other initial"],
        check=True,
    )
    target = tmp_path / "file.txt"
    target.write_text("approved\n", encoding="utf-8")
    other.write_text("unselected race\n", encoding="utf-8")
    snapshot = _git_snapshot(tmp_path, paths=["file.txt"])
    real_identity = git_write._index_identity
    calls = 0
    writer_result: list[subprocess.CompletedProcess[str]] = []

    def race_after_lock(index_path: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            writer_result.append(
                subprocess.run(
                    ["git", "-C", str(tmp_path), "add", "other.txt"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            )
        return real_identity(index_path)

    monkeypatch.setattr(git_write, "_index_identity", race_after_lock)
    GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "stage",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            **snapshot,
        },
    )

    assert len(writer_result) == 1
    assert writer_result[0].returncode != 0
    assert (
        subprocess.check_output(["git", "-C", str(tmp_path), "show", ":file.txt"], text=True)
        == "approved\n"
    )
    assert (
        subprocess.check_output(["git", "-C", str(tmp_path), "show", ":other.txt"], text=True)
        == "before\n"
    )


def test_git_stage_captures_a_dangling_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    os.symlink("not-present-target", tmp_path / "link.txt")
    snapshot = _git_snapshot(tmp_path, paths=["link.txt"])

    result = GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "stage",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["link.txt"],
            **snapshot,
        },
    )

    assert result["staged"] == ["link.txt"]
    index_entry = subprocess.check_output(
        ["git", "-C", str(tmp_path), "ls-files", "-s", "--", "link.txt"],
        text=True,
    )
    assert index_entry.startswith("120000 ")
    assert (
        subprocess.check_output(["git", "-C", str(tmp_path), "show", ":link.txt"], text=True)
        == "not-present-target"
    )


def test_git_snapshot_and_write_reject_parent_symlinks_to_outside_workspace(
    tmp_path: Path,
) -> None:
    """Neither snapshot nor effect capture may traverse a parent symlink."""

    _init_git_repo_with_commit(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "payload.txt").write_text("outside bytes\n", encoding="utf-8")
    os.symlink(outside, tmp_path / "linked-parent")

    with pytest.raises(PermissionError, match="ancestor"):
        _git_snapshot(tmp_path, paths=["linked-parent/payload.txt"])
    with pytest.raises(PermissionError, match="ancestor"):
        git_write._paths(
            tmp_path,
            ["linked-parent/payload.txt"],
            allow_missing=True,
        )
    with pytest.raises(PermissionError, match="ancestor"):
        git_write._materialize_captured_entries(
            tmp_path,
            [
                {
                    "path": "linked-parent/payload.txt",
                    "blob_oid": "0" * 40,
                    "mode": "100644",
                }
            ],
        )

    outside_oid = git_write._raw_blob_oid(
        b"outside bytes\n",
        object_format="sha1",
    )
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "cat-file", "-e", f"{outside_oid}^{{blob}}"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.parametrize(
    ("module", "capture"),
    [
        (git_read, "_capture_raw_path"),
        (git_write, "_capture_stage_bytes"),
    ],
)
def test_git_capture_rejects_verified_ancestor_rename_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    capture: str,
) -> None:
    """A swapped ancestor cannot redirect an in-flight capture outside root."""

    _init_git_repo_with_commit(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    target = nested / "payload.txt"
    target.write_text("approved bytes\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "payload.txt").write_text("outside bytes\n", encoding="utf-8")
    moved = tmp_path / "nested-moved"
    original_open = module._open_nofollow
    swapped = False

    def swap_ancestor_before_final_open(
        path: str | Path,
        flags: int,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "payload.txt" and dir_fd is not None and not swapped:
            nested.rename(moved)
            os.symlink(outside, nested)
            swapped = True
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(module, "_open_nofollow", swap_ancestor_before_final_open)
    with pytest.raises(PermissionError, match="ancestor changed"):
        getattr(module, capture)(tmp_path, "nested/payload.txt")

    assert swapped is True
    assert (nested / "payload.txt").read_text(encoding="utf-8") == "outside bytes\n"
    outside_oid = git_write._raw_blob_oid(
        b"outside bytes\n",
        object_format="sha1",
    )
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "cat-file", "-e", f"{outside_oid}^{{blob}}"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_git_stage_and_commit_capture_root_parent_for_deletions(
    tmp_path: Path,
) -> None:
    """Deletion receipts remain usable only when their parent chain is safe."""

    _init_git_repo_with_commit(tmp_path)
    (tmp_path / "file.txt").unlink()
    stage_snapshot = _git_snapshot(tmp_path, paths=["file.txt"])

    staged = GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "stage",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            **stage_snapshot,
        },
    )

    assert staged["staged"] == ["file.txt"]
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "ls-files", "--error-unmatch", "file.txt"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )

    commit_snapshot = _git_snapshot(
        tmp_path,
        paths=["file.txt"],
        capture_commit=True,
    )
    committed = GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "commit",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            "message": "remove approved path",
            **commit_snapshot,
        },
    )

    assert committed["commit_hash"]
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "cat-file", "-e", "HEAD:file.txt"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_git_sha256_stage_and_commit_deletion_use_64_digit_zero_oid(
    tmp_path: Path,
) -> None:
    """SHA-256 index-info removals must not use SHA-1's zero sentinel."""

    _init_git_repo_with_commit(tmp_path, object_format="sha256")
    (tmp_path / "file.txt").unlink()
    stage_snapshot = _git_snapshot(tmp_path, paths=["file.txt"])

    GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "stage",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            **stage_snapshot,
        },
    )
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "ls-files", "--error-unmatch", "file.txt"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )

    commit_snapshot = _git_snapshot(
        tmp_path,
        paths=["file.txt"],
        capture_commit=True,
    )
    GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "commit",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            "message": "remove SHA-256 path",
            **commit_snapshot,
        },
    )
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "cat-file", "-e", "HEAD:file.txt"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_git_sha256_absent_refs_use_64_digit_zero_oid_through_receipt_and_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same absent-ref sentinel survives snapshot, receipt, and dry-run."""

    _init_git_repo_with_commit(tmp_path, object_format="sha256")
    remote_url = "https://example.test/org/sha256.git"
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", remote_url],
        check=True,
    )
    client = _GitServiceClient(tmp_path)
    branch_snapshot = GitReadService(client).invoke(
        "snapshot",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "branch": "new-branch",
            "expect_branch_absent": True,
        },
    )
    assert branch_snapshot["expected_branch_oid"] == "0" * 64
    branch_receipt = write_arguments(
        "branch_create",
        {
            "branch": "new-branch",
            "expected_mount_revision": 1,
            **branch_snapshot,
        },
    )
    assert branch_receipt["expected_branch_oid"] == "0" * 64

    publish_snapshot = GitReadService(client).invoke(
        "publish_snapshot",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "remote": "origin",
            "branch": "main",
        },
    )
    assert publish_snapshot["expected_remote_oid"] == "0" * 64
    captured: list[list[str]] = []
    real_git = git_publish._git

    def no_network_push(repository: Path, args: list[str], **kwargs: object) -> str:
        if args[:1] == ["push"]:
            captured.append(args)
            return ""
        return real_git(repository, args, **kwargs)

    monkeypatch.setattr(git_publish, "_git", no_network_push)
    result = GitPublishService(client).invoke(
        "dry_run",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "expected_mount_revision": 1,
            **publish_snapshot,
        },
    )

    assert result["dry_run"] is True
    assert "--force-with-lease=refs/heads/main:" + "0" * 64 in captured[0]

    with pytest.raises(PermissionError, match="expected_source_oid"):
        GitPublishService(client).invoke(
            "dry_run",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "expected_mount_revision": 1,
                **publish_snapshot,
                "expected_source_oid": "a" * 40,
            },
        )
    with pytest.raises(PermissionError, match="expected_remote_oid"):
        GitPublishService(client).invoke(
            "dry_run",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "expected_mount_revision": 1,
                **publish_snapshot,
                "expected_remote_oid": "0" * 40,
            },
        )


def test_git_snapshot_and_stage_never_run_repository_clean_filters(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    marker = tmp_path / "filter-ran"
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "config",
            "filter.hostile.clean",
            f"sh -c 'touch {marker}; cat'",
        ],
        check=True,
    )
    (tmp_path / ".gitattributes").write_text("file.txt filter=hostile\n")
    (tmp_path / "file.txt").write_text("raw approved\n", encoding="utf-8")
    snapshot = _git_snapshot(tmp_path, paths=["file.txt"])

    result = GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "stage",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            **snapshot,
        },
    )

    assert result["staged"] == ["file.txt"]
    assert marker.exists() is False
    assert (
        subprocess.check_output(["git", "-C", str(tmp_path), "show", ":file.txt"], text=True)
        == "raw approved\n"
    )


def test_git_all_tracked_snapshot_never_runs_repository_clean_filters(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    marker = tmp_path / "filter-ran"
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "config",
            "filter.hostile.clean",
            f"sh -c 'touch {marker}; cat'",
        ],
        check=True,
    )
    (tmp_path / ".gitattributes").write_text("file.txt filter=hostile\n")
    (tmp_path / "file.txt").write_text("raw all-tracked\n", encoding="utf-8")

    snapshot = _git_snapshot(tmp_path, capture_commit=True, all_tracked=True)

    assert [entry["path"] for entry in snapshot["expected_commit_entries"]] == ["file.txt"]
    assert marker.exists() is False


def test_git_worktree_hash_framing_distinguishes_adjacent_field_values() -> None:
    first = hashlib.sha256()
    second = hashlib.sha256()
    git_write._update_entry_digest(first, "ab", "100644", "c")
    git_write._update_entry_digest(second, "a", "100644", "bc")
    assert first.digest() != second.digest()


def test_git_snapshot_and_write_reject_index_info_delimiter_paths(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    unsafe_path = "file\tother.txt"
    (tmp_path / unsafe_path).write_text("unsafe\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="unsafe index delimiter"):
        _git_snapshot(tmp_path, paths=[unsafe_path])
    with pytest.raises(PermissionError, match="unsafe index delimiter"):
        write_arguments(
            "commit",
            {
                "paths": [unsafe_path],
                "message": "unsafe",
                "expected_head": "a" * 40,
                "expected_tree": "b" * 40,
                "expected_index_tree": "c" * 40,
                "expected_status_hash": "d" * 64,
                "expected_worktree_hash": "e" * 64,
                "expected_mount_revision": 1,
            },
        )


def test_git_snapshot_hashes_large_diff_without_ui_truncation(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    (tmp_path / "file.txt").write_bytes(b"x" * (600 * 1024))
    snapshot = _git_snapshot(tmp_path, paths=["file.txt"])

    result = GitWriteService(_GitServiceClient(tmp_path)).invoke(
        "stage",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "paths": ["file.txt"],
            **snapshot,
        },
    )

    assert result["staged"] == ["file.txt"]


def test_git_restore_fails_closed_without_a_host_workspace_lease(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path, "approved source\n")
    subprocess.run(["git", "-C", str(tmp_path), "branch", "source"], check=True)
    (tmp_path / "file.txt").write_text("replacement\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "replacement"],
        check=True,
    )
    (tmp_path / "file.txt").write_text("to restore\n", encoding="utf-8")
    snapshot = _git_snapshot(tmp_path, paths=["file.txt"], source="source")
    with pytest.raises(PermissionError, match="exclusive workspace mutation lease"):
        GitWriteService(_GitServiceClient(tmp_path)).invoke(
            "restore",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "paths": ["file.txt"],
                "source": "source",
                **snapshot,
            },
        )
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "to restore\n"


def test_git_branch_switch_fails_closed_without_a_host_workspace_lease(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "branch", "feature"], check=True)
    (tmp_path / "file.txt").write_text("replacement\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "replacement"],
        check=True,
    )
    replacement = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    expected = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "feature"], text=True
    ).strip()
    snapshot = _git_snapshot(tmp_path, branch="feature")
    with pytest.raises(PermissionError, match="exclusive workspace mutation lease"):
        GitWriteService(_GitServiceClient(tmp_path)).invoke(
            "branch_switch",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "branch": "feature",
                **snapshot,
            },
        )
    assert (
        subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
        ).strip()
        == replacement
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "feature"], text=True
        ).strip()
        == expected
    )


def test_git_publish_uses_exact_oid_refspec_and_force_with_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://example.test/org/repo.git",
        ],
        check=True,
    )
    source_oid = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "main"], text=True
    ).strip()
    captured: list[list[str]] = []
    real_git = git_publish._git

    def no_network_push(repository: Path, args: list[str], **kwargs: object) -> str:
        if args[:1] == ["push"]:
            captured.append(args)
            return ""
        return real_git(repository, args, **kwargs)

    monkeypatch.setattr(git_publish, "_git", no_network_push)
    result = GitPublishService(_GitServiceClient(tmp_path)).invoke(
        "push",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "expected_mount_revision": 1,
            "remote": "origin",
            "branch": "main",
            "expected_source_oid": source_oid,
            "expected_remote_oid": "0" * 40,
            "expected_remote_url": "https://example.test/org/repo.git",
            "expected_remote_url_hash": hashlib.sha256(
                b"https://example.test/org/repo.git"
            ).hexdigest(),
            "force_with_lease": True,
        },
    )

    assert result["source_oid"] == source_oid
    assert captured == [
        [
            "push",
            "--force-with-lease=refs/heads/main:" + "0" * 40,
            "--",
            "https://example.test/org/repo.git",
            f"{source_oid}:refs/heads/main",
        ]
    ]


def test_git_publish_normal_flow_uses_exact_remote_lease_and_push_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    remote_url = "https://example.test/org/repo.git"
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", remote_url],
        check=True,
    )
    source_oid = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "main"], text=True
    ).strip()
    captured: list[list[str]] = []
    real_git = git_publish._git

    def no_network_push(repository: Path, args: list[str], **kwargs: object) -> str:
        if args[:1] == ["push"]:
            captured.append(args)
            return ""
        return real_git(repository, args, **kwargs)

    monkeypatch.setattr(git_publish, "_git", no_network_push)
    GitPublishService(_GitServiceClient(tmp_path)).invoke(
        "push",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "expected_mount_revision": 1,
            "remote": "origin",
            "branch": "main",
            "expected_source_oid": source_oid,
            "expected_remote_oid": "0" * 40,
            "expected_remote_url": remote_url,
            "expected_remote_url_hash": hashlib.sha256(remote_url.encode()).hexdigest(),
        },
    )

    assert "--force" not in captured[0]
    assert "--force-with-lease=refs/heads/main:" + "0" * 40 in captured[0]
    assert captured[0][-2] == remote_url


def test_git_publish_uses_captured_url_when_config_changes_at_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    approved_url = "https://example.test/org/repo.git"
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", approved_url],
        check=True,
    )
    source_oid = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "main"], text=True
    ).strip()
    captured: list[list[str]] = []
    real_git = git_publish._git

    def race_config_at_push(repository: Path, args: list[str], **kwargs: object) -> str:
        if args[:1] == ["push"]:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "remote",
                    "set-url",
                    "origin",
                    "https://attacker.test/repo.git",
                ],
                check=True,
            )
            captured.append(args)
            return ""
        return real_git(repository, args, **kwargs)

    monkeypatch.setattr(git_publish, "_git", race_config_at_push)
    GitPublishService(_GitServiceClient(tmp_path)).invoke(
        "push",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "expected_mount_revision": 1,
            "remote": "origin",
            "branch": "main",
            "expected_source_oid": source_oid,
            "expected_remote_oid": "0" * 40,
            "expected_remote_url": approved_url,
            "expected_remote_url_hash": hashlib.sha256(approved_url.encode()).hexdigest(),
        },
    )

    assert captured[0][-2] == approved_url


def test_git_publish_rejects_non_fast_forward_without_force_authority(
    tmp_path: Path,
) -> None:
    _init_git_repo_with_commit(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "--orphan", "other"], check=True)
    (tmp_path / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "unrelated.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "unrelated"], check=True)
    unrelated_oid = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "main"], check=True)
    source_oid = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "main"], text=True
    ).strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://example.test/org/repo.git",
        ],
        check=True,
    )
    with pytest.raises(PermissionError, match="not a fast-forward"):
        GitPublishService(_GitServiceClient(tmp_path)).invoke(
            "push",
            {
                "profile_id": "default",
                "workspace_id": "workspace",
                "expected_mount_revision": 1,
                "remote": "origin",
                "branch": "main",
                "expected_source_oid": source_oid,
                "expected_remote_oid": unrelated_oid,
                "expected_remote_url": "https://example.test/org/repo.git",
                "expected_remote_url_hash": hashlib.sha256(
                    b"https://example.test/org/repo.git"
                ).hexdigest(),
            },
        )


def test_authority_receipt_is_durable_and_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_RECEIPT_ROOT", tmp_path / "receipts")
    scope = {
        "service_pack_id": "service-pack",
        "operation": "effect.write",
        "authority": "effect.write",
        "caller_id": "caller",
        "caller_pack_id": "caller-pack",
        "caller_function_id": "function",
        "profile_id": "default",
        "workspace_id": "workspace",
        "session_id": "session",
        "arguments": {"path": "safe.txt"},
        "approval_required": False,
    }
    host_context = _authenticated_host_context()
    issued = bridge._authorize(scope, host_context=host_context)
    stored = list((tmp_path / "receipts").glob("*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text())["status"] == "issued"
    redeemed = bridge._redeem(
        {**scope, "receipt": issued["receipt"]},
        host_context=host_context,
    )
    assert redeemed["authorized"] is True
    replay = bridge._redeem(
        {**scope, "receipt": issued["receipt"]},
        host_context=host_context,
    )
    assert replay["authorized"] is False
