"""Portable capsule, local fencing and captured Host boundary regressions."""

from __future__ import annotations

import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import runpy
import stat
import struct
import sys
from types import SimpleNamespace
from typing import Any
import zipfile
import zlib

import pytest

from ecosystem.tobkiri_cloud_workspace_pack.runtime import capsule
from ecosystem.tobkiri_cloud_workspace_pack.runtime import recipe
from ecosystem.tobkiri_cloud_workspace_pack.runtime import store as store_module
from ecosystem.tobkiri_cloud_workspace_pack.runtime.host import (
    CloudWorkspaceHostFactoryV4,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.recipe import RECIPE_DIGEST
from ecosystem.tobkiri_cloud_workspace_pack.runtime.service import (
    INSPECT,
    WORKSPACE,
    CloudWorkspace,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.store import (
    Conflict,
    WorkspaceStore,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.task import TASK_RESOURCE
from tobkiri_protocol.provenance import sha256_file
from tobkiri_protocol.validation import validate_document

PACK = Path(__file__).resolve().parents[1] / "ecosystem/tobkiri_cloud_workspace_pack"
PLAN = "sha256:" + "a" * 64


def sealed(revision: int = 1, parent: str = "", **files: bytes) -> Any:
    return capsule.make_capsule(
        workspace_id="work-1",
        profile_id="defaults",
        plan_digest=PLAN,
        revision=revision,
        parent_digest=parent,
        recipe_digest=RECIPE_DIGEST,
        files=files or {"README.md": b"actual work\n"},
    )


def test_capsule_export_is_deterministic_and_restores_exact_bytes(
    tmp_path: Path,
) -> None:
    manifest, blobs = sealed(
        **{"src/main.py": b"print('hello')\n", "data.bin": b"\0\xff"}
    )
    archive = capsule.export_archive(manifest, blobs)
    assert archive == capsule.export_archive(manifest, blobs)
    assert capsule.import_archive(archive) == (manifest, blobs)
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setitem(sys.modules, "capsule", capsule)
        monkeypatch.setitem(sys.modules, "recipe", recipe)
        runtime = runpy.run_path(str(PACK / "container/runtime.py"))
        input_file, destination = tmp_path / "work.zip", tmp_path / "work"
        input_file.write_bytes(archive)
        destination.mkdir()
        assert runtime["restore"](input_file, destination) == manifest
        assert (destination / "src/main.py").read_bytes() == b"print('hello')\n"
        assert (destination / "data.bin").read_bytes() == b"\0\xff"
        with pytest.raises(ValueError, match="empty"):
            runtime["restore"](input_file, destination)
        changed = deepcopy(manifest)
        changed["recipe_digest"] = "sha256:" + "b" * 64
        changed["manifest_digest"] = capsule.digest(
            capsule.canonical(
                {
                    key: value
                    for key, value in changed.items()
                    if key != "manifest_digest"
                }
            )
        )
        input_file.write_bytes(capsule.export_archive(changed, blobs))
        empty = tmp_path / "foreign-recipe"
        empty.mkdir()
        with pytest.raises(ValueError, match="recipe"):
            runtime["restore"](input_file, empty)
        assert not list(empty.iterdir())
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "/tmp/x",
        "C:/x",
        "a\\b",
        "a//b",
        "a/./b",
        "a/../b",
        "x ",
        ".env",
        ".env.local",
        ".aws/config",
        ".ssh/id_rsa",
        "Secrets/x",
        ".git/config",
        "credentials.json",
        "approvals/receipt",
        "leases/active",
        "user_data/x",
        "cert.pem",
        "capsule-manifest.json",
        "capsule-manifest.json/x",
        "other/CAPSULE-MANIFEST.JSON/x",
        "あ" * 86,
        "cafe\u0301.txt",
        "NUL.txt",
        "a\x00b",
    ],
)
def test_capsule_rejects_traversal_secret_and_platform_alias_paths(path: str) -> None:
    with pytest.raises((ValueError, PermissionError)):
        sealed(**{path: b"forbidden"})


def test_capsule_rejects_file_directory_and_case_collisions() -> None:
    for files in [{"A.txt": b"a", "a.txt": b"b"}, {"a": b"a", "a/b": b"b"}]:
        with pytest.raises(ValueError, match="colliding"):
            sealed(**files)


def test_manifest_cannot_export_authority_or_unverified_bytes() -> None:
    manifest, blobs = sealed()
    for field in ["approved", "lease", "credentials", "host_path", "security_epoch"]:
        changed = deepcopy(manifest) | {field: "forged"}
        with pytest.raises(ValueError, match="fields"):
            capsule.validate_capsule(changed, blobs)
    altered = deepcopy(manifest)
    altered["source"]["profile_id"] = "other"
    with pytest.raises(ValueError, match="digest"):
        capsule.validate_capsule(altered, blobs)
    with pytest.raises(ValueError, match="digest"):
        capsule.validate_capsule(manifest, {next(iter(blobs)): b"tampered"})


@pytest.mark.parametrize("kind", ["symlink", "duplicate", "traversal", "bomb"])
def test_import_checks_zip_metadata_before_writing(kind: str) -> None:
    manifest, blobs = sealed()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", capsule.canonical(manifest))
        key = next(iter(blobs))
        name = "blobs/" + key[7:]
        info = zipfile.ZipInfo("../escape" if kind == "traversal" else name)
        info.external_attr = (stat.S_IFLNK if kind == "symlink" else stat.S_IFREG) << 16
        if kind == "bomb":
            info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, b"x" * 500_000 if kind == "bomb" else blobs[key])
        if kind == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr(name, blobs[key])
    with pytest.raises(ValueError, match="unsafe"):
        capsule.import_archive(output.getvalue())


def test_manifest_bounds_duplicate_json_and_version_are_strict() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        capsule.parse_json(b'{"version":1,"version":2}')
    with pytest.raises(ValueError, match="non-finite"):
        capsule.parse_json(b'{"x":NaN}')
    with pytest.raises(ValueError, match="size"):
        sealed(**{"large": b"x" * (capsule.MAX_FILE_BYTES + 1)})
    with pytest.raises(ValueError, match="count"):
        sealed(**{f"file-{i}": b"" for i in range(capsule.MAX_FILES + 1)})
    manifest, blobs = sealed()
    manifest["version"] = "future"
    with pytest.raises(ValueError, match="version"):
        capsule.validate_capsule(manifest, blobs)


def test_import_rejects_deflate_stream_with_forged_one_byte_size() -> None:
    """Central size fields cannot bound zipfile's internal deflate allocation."""
    manifest, blobs = sealed(**{"safe.txt": b"x"})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", capsule.canonical(manifest))
        archive.writestr(
            "blobs/" + next(iter(blobs))[7:],
            b"x" * (8 * 1024 * 1024),
            compress_type=zipfile.ZIP_DEFLATED,
        )
    data = bytearray(output.getvalue())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        header = archive.infolist()[1].header_offset
    struct.pack_into("<I", data, header + 14, zlib.crc32(b"x"))
    struct.pack_into("<I", data, header + 22, 1)
    central = data.index(b"PK\x01\x02", header)
    central = data.index(b"PK\x01\x02", central + 4)
    struct.pack_into("<I", data, central + 16, zlib.crc32(b"x"))
    struct.pack_into("<I", data, central + 24, 1)
    with pytest.raises(ValueError, match="unsafe"):
        capsule.import_archive(bytes(data))


def test_checkpoint_and_restore_retention_capacity_fail_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceStore(tmp_path, "defaults")
    manifest, blobs = sealed()
    monkeypatch.setattr(store_module, "MAX_CHECKPOINTS", 0)
    with pytest.raises(ValueError, match="retention"):
        publish(store, manifest, blobs)
    assert store.list() == []
    monkeypatch.setattr(store_module, "MAX_CHECKPOINTS", 256)
    publish(store, manifest, blobs)
    monkeypatch.setattr(store_module, "MAX_RETAINED_BYTES", 0)
    with pytest.raises(ValueError, match="retention"):
        store.restore_local(manifest["manifest_digest"], guard=lambda: None)
    assert not (store.root / "restored").exists()


def publish(store: WorkspaceStore, manifest: Any, blobs: Any, **kwargs: Any) -> Any:
    return store.publish(
        manifest,
        blobs,
        expected_revision=manifest["revision"] - 1,
        expected_writer_epoch=kwargs.get(
            "expected_writer_epoch",
            (store.get(manifest["workspace_id"]) or {}).get("writer_epoch", 0),
        ),
        actor=kwargs.get("actor", "writer-a"),
        request_id=kwargs.get("request_id", "r1"),
        fingerprint=kwargs.get("fingerprint", "one"),
        guard=kwargs.get("guard", lambda: None),
        now_ms=kwargs.get("now_ms", 100),
    )


def test_local_checkpoint_cas_replay_fencing_and_release(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path, "defaults")
    assert store.list() == [] and not store.root.exists()
    manifest, blobs = sealed()
    result = publish(store, manifest, blobs)
    assert publish(store, manifest, blobs) == result
    assert "owner" not in result and "fence" not in result
    with pytest.raises(Conflict, match="rebound"):
        publish(store, manifest, blobs, fingerprint="two")
    with pytest.raises(Conflict, match="stale"):
        publish(store, manifest, blobs, request_id="new")
    next_manifest, next_blobs = sealed(2, manifest["manifest_digest"])
    with pytest.raises(Conflict, match="another"):
        publish(store, next_manifest, next_blobs, actor="writer-b", request_id="r2")
    store.release_writer(
        "work-1",
        expected_revision=1,
        expected_writer_epoch=1,
        actor="writer-a",
        guard=lambda: None,
    )
    with pytest.raises(Conflict, match="epoch"):
        publish(
            store,
            next_manifest,
            next_blobs,
            request_id="stale-writer",
            expected_writer_epoch=1,
        )
    publish(store, next_manifest, next_blobs, actor="writer-b", request_id="r2")
    with pytest.raises(Conflict, match="current"):
        store.release_writer(
            "work-1",
            expected_revision=2,
            expected_writer_epoch=3,
            actor="writer-a",
            guard=lambda: None,
        )
    with store.connection() as connection:
        assert connection.execute("SELECT fence FROM heads").fetchone()[0] == 3
    assert WorkspaceStore(tmp_path, "other").get("work-1") is None


def test_local_writer_expiry_and_guard_failure_roll_back(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path, "defaults")
    manifest, blobs = sealed()
    publish(store, manifest, blobs)
    successor, contents = sealed(2, manifest["manifest_digest"], **{"new.txt": b"new"})
    calls = []

    def guard() -> None:
        calls.append(1)
        if len(calls) == 2:
            raise PermissionError("Profile was revoked")

    with pytest.raises(PermissionError, match="revoked"):
        publish(
            store,
            successor,
            contents,
            actor="writer-b",
            request_id="r2",
            guard=guard,
            now_ms=120_101,
        )
    assert store.get("work-1")["revision"] == 1
    assert store.replay("r2", "one") is None
    assert (
        publish(
            store,
            successor,
            contents,
            actor="writer-b",
            request_id="r2",
            now_ms=120_101,
        )["revision"]
        == 2
    )


def test_restore_never_takes_a_host_path_or_overwrites_existing_copy(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path, "defaults")
    manifest, blobs = sealed()
    publish(store, manifest, blobs)
    result = store.restore_local(manifest["manifest_digest"], guard=lambda: None)
    assert result["container_status"] == "not_started"
    assert "path" not in result
    copy = store.root / "restored" / manifest["manifest_digest"][7:] / "README.md"
    assert copy.read_bytes() == b"actual work\n"
    with pytest.raises(Conflict, match="already"):
        store.restore_local(manifest["manifest_digest"], guard=lambda: None)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (store.root / "restored" / "fake").symlink_to(foreign)
    store.path.unlink()
    store.path.symlink_to(foreign / "state")
    with pytest.raises(PermissionError, match="symbolic"):
        publish(store, manifest, blobs)


class PublicFiles:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.changed = False

    def invoke(self, contract: str, operation: str, payload: Any) -> Any:
        self.calls.append((contract, operation, payload))
        if contract == TASK_RESOURCE:
            raise LookupError("optional task provider is not selected")
        if contract == WORKSPACE:
            count = sum(c[0] == WORKSPACE for c in self.calls)
            return {
                "revision": 2 if self.changed and count == 2 else 1,
                "selected_workspace_id": "host-work",
                "mounts": [
                    {
                        "id": "host-work",
                        "mount_revision": 1,
                        "root_path": "/private/host/root",
                    }
                ],
            }
        assert contract == INSPECT and payload["require_selected"] is True
        return {
            "workspace_id": "host-work",
            "path": payload["path"],
            "size": 8,
            "content": "captured",
            "encoding": "utf-8",
            "start_line": 1,
            "end_line": 1,
            "total_lines": 1,
        }


def test_public_selected_capture_exports_work_without_host_path(tmp_path: Path) -> None:
    client = PublicFiles()
    store = WorkspaceStore(tmp_path, "defaults")
    service = CloudWorkspace(
        store,
        client,
        plan_digest=PLAN,
        actor="actor",
        request_id="capture",
        guard=lambda: None,
    )
    values = {
        "conversation_id": "chat-1",
        "expected_revision": 0,
        "expected_writer_epoch": 0,
        "paths": "src/main.py",
    }
    result = service.invoke("capture", values)
    exported = service.export(result["id"], 1)
    manifest, blobs = capsule.import_archive(
        base64.b64decode(exported["archive_base64"])
    )
    assert list(blobs.values()) == [b"captured"]
    assert b"/private/host/root" not in capsule.canonical(manifest)
    assert service.snapshot("chat-1")["container"]["started"] is False
    assert service.snapshot("chat-1")["cloud"]["connected"] is False


def test_capture_mount_change_or_secret_path_publishes_nothing(tmp_path: Path) -> None:
    client = PublicFiles()
    client.changed = True
    store = WorkspaceStore(tmp_path, "defaults")
    service = CloudWorkspace(
        store,
        client,
        plan_digest=PLAN,
        actor="actor",
        request_id="capture",
        guard=lambda: None,
    )
    values = {
        "conversation_id": "chat-1",
        "expected_revision": 0,
        "expected_writer_epoch": 0,
        "paths": "main.py",
    }
    with pytest.raises(Conflict, match="changed"):
        service.invoke("capture", values)
    assert store.list() == []
    with pytest.raises(PermissionError):
        service.invoke("capture", values | {"paths": ".env"})


def test_import_rebinds_local_profile_and_handoff_never_starts_remote(
    tmp_path: Path,
) -> None:
    manifest, blobs = sealed()
    raw = base64.b64encode(capsule.export_archive(manifest, blobs)).decode()
    store = WorkspaceStore(tmp_path, "another-profile")
    service = CloudWorkspace(
        store,
        None,
        plan_digest="sha256:" + "b" * 64,
        actor="actor",
        request_id="import",
        guard=lambda: None,
    )
    result = service.invoke(
        "import",
        {
            "conversation_id": "chat-1",
            "expected_revision": 0,
            "expected_writer_epoch": 0,
            "archive_base64": raw,
        },
    )
    assert result["source_profile_id"] == "another-profile"
    assert result["source_plan_digest"] != PLAN
    service.request_id = "handoff"
    offer = service.invoke(
        "prepare_handoff",
        {
            "conversation_id": "chat-1",
            "expected_revision": 1,
            "expected_writer_epoch": 1,
            "expected_receiver_head": "",
        },
    )
    assert offer["remote_execution"] == "unavailable"
    assert "lease" not in offer and "actor" not in offer


def test_v4_source_and_ui_bytes_are_current_and_schema_valid() -> None:
    for filename, schema in [
        ("pack.v4.json", "pack_manifest_v4.schema.json"),
        ("contracts.v4.json", "pack_contract_catalog_v4.schema.json"),
        ("executables.v4.json", "executable_catalog_v4.schema.json"),
    ]:
        validate_document(json.loads((PACK / filename).read_text()), schema)
    manifest = json.loads((PACK / "pack.v4.json").read_text())
    for artifact in manifest["artifacts"]:
        assert artifact["digest"] == sha256_file(PACK / artifact["path"])
    assert manifest["requirements"]["network"] == {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    assert manifest["requirements"]["secrets"] == []
    slots = []
    for path in (PACK / "frontend/contributions").glob("*.json"):
        contribution = json.loads(path.read_text())
        validate_document(contribution["view"], "ui_view_v1.schema.json")
        slots.append(contribution["view"]["slot"])
    assert sorted(slots) == ["composer_above", "workspace_tab"]


def test_host_factory_fails_closed_on_foreign_scope_and_authority_fields(
    tmp_path: Path,
) -> None:
    factory = CloudWorkspaceHostFactoryV4("manage")
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=factory.function_id, implementation_digest=PLAN
        ),
        operation=SimpleNamespace(
            contract_id=factory.contract_id,
            operation_id=factory.operation_id,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="provider"),
        artifact=SimpleNamespace(digest=PLAN),
    )
    context = SimpleNamespace(
        user_data_root=tmp_path,
        provider_bindings=(binding,),
        profile_id="defaults",
        plan_digest=PLAN,
        security_epoch=4,
        domain_ids={(factory.contract_id, factory.operation_id, "provider"): "domain"},
    )
    contribution = factory.capture(context).contributions[0]
    captured = SimpleNamespace(
        profile_id="defaults",
        plan_digest=PLAN,
        security_epoch=4,
        request_id="request-1",
        target_domain_id="domain",
    )
    invocation = SimpleNamespace(
        assert_current=lambda: None,
        envelope=SimpleNamespace(
            context=captured, target_principal=SimpleNamespace(value="provider")
        ),
        presentation_owner_principal_id="actor",
        presentation_owner_session_id="session",
        contract_client=lambda **kw: None,
    )
    request = {
        "operation": "initialize",
        "conversation_id": "chat-1",
        "expected_revision": 0,
        "expected_writer_epoch": 0,
    }
    with pytest.raises(PermissionError, match="fields"):
        contribution.invoke(
            factory.operation_id, request | {"approved": True}, invocation
        )
    captured.plan_digest = "sha256:" + "b" * 64
    with pytest.raises(PermissionError, match="scope"):
        contribution.invoke(factory.operation_id, request, invocation)
    assert not (tmp_path / "packs").exists()
    captured.plan_digest = PLAN
    assert (
        contribution.invoke(factory.operation_id, request, invocation)["revision"] == 1
    )
