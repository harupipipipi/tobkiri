from __future__ import annotations

import base64
import json
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


PNG = b"\x89PNG\r\n\x1a\nframe"


class _FakeManager:
    def validate_desktop_access(self, seat_id, access_key, owner_id=None):
        del access_key
        if owner_id != "local-user":
            return {
                "ok": False,
                "code": "DESKTOP_ACCESS_DENIED",
                "error": "denied",
                "status_code": 403,
            }
        return {"ok": True, "seat_id": seat_id, "owner_id": owner_id}

    def screenshot(self, seat_id):
        assert seat_id == "seat-1"
        return {
            "ok": True,
            "data": PNG,
            "content_type": "image/png",
            "width": 640,
            "height": 480,
            "source": "test",
        }


def _context(**changes):
    value = {
        "owner_pack": "defaultspack",
        "run_id": "run-1",
        "conversation_id": "conversation-1",
        "workspace_id": "workspace-1",
    }
    value.update(changes)
    return value


def _service(
    tmp_path,
    *,
    now=1_750_000_000.0,
    max_count=50,
    max_bytes=50 * 1024 * 1024,
):
    from ecosystem.defaultspack.backend.sandbox.frame_cache import FrameCache
    from ecosystem.defaultspack.backend.sandbox.frame_evidence_store import (
        FrameEvidenceStore,
    )

    class Service:
        manager = _FakeManager()
        frame_cache = FrameCache(
            min_capture_interval_seconds=0,
            time_fn=lambda: now,
        )
        frame_evidence_store = FrameEvidenceStore(
            tmp_path,
            time_fn=lambda: now,
            max_count_per_run=max_count,
            max_bytes_per_run=max_bytes,
        )

    return Service()


def _capture(service):
    from ecosystem.defaultspack.blocks.sandbox import api

    return api._desktop_frame(service, {"seat_id": "seat-1"}, _context())


def _persist(service, **changes):
    from ecosystem.defaultspack.blocks.sandbox import api

    payload = {
        "seat_id": "seat-1",
        "action": "persist",
        "frame_seq": 1,
        "purpose": "visual_qa",
    }
    payload.update(changes)
    return api._desktop_frame_evidence(service, payload, _context())


def test_desktop_frame_read_is_side_effect_free(tmp_path):
    service = _service(tmp_path)

    result = _capture(service)

    assert result["_binary"] is True
    assert result["body"] == PNG
    assert result["headers"]["Cache-Control"] == "no-store"
    assert "artifacts" not in result
    assert "artifact_paths" not in result
    assert "X-Rumi-Artifact-Path" not in result["headers"]
    assert not (tmp_path / "user_data" / "artifacts").exists()


def test_explicit_evidence_commit_returns_opaque_private_artifact(tmp_path):
    service = _service(tmp_path)
    _capture(service)

    result = _persist(service)

    assert result["status"] == "ok"
    data = result["data"]
    artifact = data["artifacts"][0]
    assert data["artifact_ref"].startswith("frame_evidence_")
    assert data["artifact_refs"] == [data["artifact_ref"]]
    assert artifact["sha256"]
    assert artifact["retention"]["ttl_seconds"] == 604800
    assert artifact["privacy"] == {
        "scope": "local_private",
        "encrypted_at_rest": False,
    }
    assert not ({"path", "content_ref", "blob_name", "binding"} & artifact.keys())

    root = tmp_path / "user_data" / "artifacts" / "desktop_frame_evidence"
    record_path = next((root / "records").iterdir())
    blob_path = next((root / "blobs").iterdir())
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["binding"] == {
        "run_id": "run-1",
        "conversation_id": "conversation-1",
        "workspace_id": "workspace-1",
        "seat_id": "seat-1",
        "principal_id": "local-user",
    }
    assert blob_path.read_bytes() == PNG
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(blob_path.stat().st_mode) == 0o600


def test_evidence_commit_rejects_stale_revision_without_writing(tmp_path):
    service = _service(tmp_path)
    _capture(service)
    service.frame_cache.put_frame(
        "seat-1",
        PNG + b"new",
        content_type="image/png",
        width=640,
        height=480,
    )

    result = _persist(service, frame_seq=1)

    assert result["status"] == "error"
    assert result["error"]["code"] == "DESKTOP_FRAME_EVIDENCE_STALE_REVISION"
    assert not (tmp_path / "user_data" / "artifacts").exists()


def test_unauthorized_evidence_commit_precedes_storage(tmp_path):
    from ecosystem.defaultspack.blocks.sandbox import api

    service = _service(tmp_path)
    _capture(service)

    result = api._desktop_frame_evidence(
        service,
        {
            "seat_id": "seat-1",
            "action": "persist",
            "frame_seq": 1,
            "purpose": "visual_qa",
        },
        {
            "run_id": "run-1",
            "conversation_id": "conversation-1",
            "workspace_id": "workspace-1",
        },
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "DESKTOP_ACCESS_DENIED"
    assert not (tmp_path / "user_data" / "artifacts").exists()


def test_evidence_context_is_required_and_not_accepted_from_arguments(tmp_path):
    from ecosystem.defaultspack.blocks.sandbox import api

    service = _service(tmp_path)
    _capture(service)

    result = api._desktop_frame_evidence(
        service,
        {
            "seat_id": "seat-1",
            "action": "persist",
            "frame_seq": 1,
            "purpose": "visual_qa",
            "run_id": "client-run",
            "conversation_id": "client-conversation",
            "workspace_id": "client-workspace",
        },
        {"owner_pack": "defaultspack"},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "DESKTOP_FRAME_EVIDENCE_CONTEXT_REQUIRED"
    assert not (tmp_path / "user_data" / "artifacts").exists()


def test_evidence_deduplicates_concurrent_exact_commits(tmp_path):
    service = _service(tmp_path)
    _capture(service)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: _persist(service), range(16)))

    refs = {result["data"]["artifact_ref"] for result in results}
    assert len(refs) == 1
    root = tmp_path / "user_data" / "artifacts" / "desktop_frame_evidence"
    assert len(list((root / "records").iterdir())) == 1
    assert len(list((root / "blobs").iterdir())) == 1
    deduplicated = sum(
        result["data"]["artifacts"][0]["deduplicated"] for result in results
    )
    assert deduplicated == 15


def test_evidence_quota_fails_closed(tmp_path):
    service = _service(tmp_path, max_count=1)
    _capture(service)
    first = _persist(service)
    service.frame_cache.put_frame(
        "seat-1",
        PNG + b"second",
        content_type="image/png",
        width=640,
        height=480,
    )

    second = _persist(service, frame_seq=2)

    assert first["status"] == "ok"
    assert second["status"] == "error"
    assert second["error"]["code"] == "DESKTOP_FRAME_EVIDENCE_COUNT_QUOTA"


def test_evidence_size_quota_fails_before_writing(tmp_path):
    service = _service(tmp_path, max_bytes=len(PNG) - 1)
    _capture(service)

    result = _persist(service)

    assert result["status"] == "error"
    assert result["error"]["code"] == "DESKTOP_FRAME_EVIDENCE_SIZE_QUOTA"
    root = tmp_path / "user_data" / "artifacts" / "desktop_frame_evidence"
    assert not list((root / "records").iterdir())
    assert not list((root / "blobs").iterdir())


def test_evidence_rejects_unsupported_image_type(tmp_path):
    service = _service(tmp_path)
    service.frame_cache.put_frame(
        "seat-1",
        b"GIF89a-frame",
        content_type="image/gif",
        width=640,
        height=480,
    )

    result = _persist(service)

    assert result["status"] == "error"
    assert result["error"]["code"] == "DESKTOP_FRAME_EVIDENCE_MIME_UNSUPPORTED"


def test_evidence_run_cleanup_removes_exact_bound_artifacts(tmp_path):
    from ecosystem.defaultspack.blocks.sandbox import api

    service = _service(tmp_path)
    _capture(service)
    persisted = _persist(service)

    cleaned = api._desktop_frame_evidence(
        service,
        {"seat_id": "seat-1", "action": "cleanup_run"},
        _context(),
    )

    assert persisted["status"] == "ok"
    assert cleaned["data"]["removed"] == 1
    root = tmp_path / "user_data" / "artifacts" / "desktop_frame_evidence"
    assert not list((root / "records").iterdir())
    assert not list((root / "blobs").iterdir())


def test_expired_evidence_is_deleted_and_cannot_be_exported(tmp_path):
    from ecosystem.defaultspack.backend.sandbox.frame_cache import DesktopFrame
    from ecosystem.defaultspack.backend.sandbox.frame_evidence_store import (
        FrameEvidenceBinding,
        FrameEvidenceError,
        FrameEvidenceStore,
    )

    clock = [1_750_000_000.0]
    store = FrameEvidenceStore(tmp_path, time_fn=lambda: clock[0])
    binding = FrameEvidenceBinding(
        run_id="run-1",
        conversation_id="conversation-1",
        workspace_id="workspace-1",
        seat_id="seat-1",
        principal_id="local-user",
    )
    artifact = store.persist(
        DesktopFrame(
            seat_id="seat-1",
            frame_seq=1,
            data=PNG,
            content_type="image/png",
            width=640,
            height=480,
            captured_at=clock[0],
        ),
        binding=binding,
        purpose="visual_qa",
        ttl_seconds=60,
    )
    clock[0] += 61

    try:
        store.export(artifact["artifact_ref"], binding=binding)
    except FrameEvidenceError as exc:
        assert exc.code == "DESKTOP_FRAME_EVIDENCE_EXPIRED"
    else:
        raise AssertionError("expired desktop frame evidence was exported")
    root = tmp_path / "user_data" / "artifacts" / "desktop_frame_evidence"
    assert not list((root / "records").iterdir())
    assert not list((root / "blobs").iterdir())


def test_evidence_export_rejects_tampered_blob_path(tmp_path):
    from ecosystem.defaultspack.backend.sandbox.frame_evidence_store import (
        FrameEvidenceError,
    )
    from ecosystem.defaultspack.blocks.sandbox import api

    service = _service(tmp_path)
    _capture(service)
    persisted = _persist(service)
    artifact_ref = persisted["data"]["artifact_ref"]
    root = tmp_path / "user_data" / "artifacts" / "desktop_frame_evidence"
    record_path = root / "records" / f"{artifact_ref}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["blob_name"] = "../outside.png"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    binding = api._desktop_frame_evidence_binding(_context(), seat_id="seat-1")

    try:
        service.frame_evidence_store.export(artifact_ref, binding=binding)
    except FrameEvidenceError as exc:
        assert exc.code == "DESKTOP_FRAME_EVIDENCE_STORAGE_UNSAFE"
    else:
        raise AssertionError("tampered desktop frame evidence path was accepted")


def test_evidence_delete_rejects_tampered_blob_path(tmp_path):
    from ecosystem.defaultspack.backend.sandbox.frame_evidence_store import (
        FrameEvidenceError,
    )
    from ecosystem.defaultspack.blocks.sandbox import api

    service = _service(tmp_path)
    _capture(service)
    persisted = _persist(service)
    artifact_ref = persisted["data"]["artifact_ref"]
    root = tmp_path / "user_data" / "artifacts" / "desktop_frame_evidence"
    record_path = root / "records" / f"{artifact_ref}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    outside_path = root / "outside.png"
    outside_path.write_bytes(b"must remain")
    record["blob_name"] = "../outside.png"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    binding = api._desktop_frame_evidence_binding(_context(), seat_id="seat-1")

    try:
        service.frame_evidence_store.delete(artifact_ref, binding=binding)
    except FrameEvidenceError as exc:
        assert exc.code == "DESKTOP_FRAME_EVIDENCE_STORAGE_UNSAFE"
    else:
        raise AssertionError("tampered desktop frame evidence path was deleted")
    assert outside_path.read_bytes() == b"must remain"
    assert record_path.is_file()


def test_evidence_export_delete_and_binding_checks(tmp_path):
    from ecosystem.defaultspack.blocks.sandbox import api

    service = _service(tmp_path)
    _capture(service)
    persisted = _persist(service)
    artifact_ref = persisted["data"]["artifact_ref"]

    forbidden = api._desktop_frame_evidence(
        service,
        {"seat_id": "seat-1", "action": "export", "artifact_ref": artifact_ref},
        _context(conversation_id="other-conversation"),
    )
    exported = api._desktop_frame_evidence(
        service,
        {"seat_id": "seat-1", "action": "export", "artifact_ref": artifact_ref},
        _context(),
    )
    deleted = api._desktop_frame_evidence(
        service,
        {"seat_id": "seat-1", "action": "delete", "artifact_ref": artifact_ref},
        _context(),
    )

    assert forbidden["error"]["code"] == "DESKTOP_FRAME_EVIDENCE_FORBIDDEN"
    assert base64.b64decode(exported["data"]["data_base64"]) == PNG
    assert deleted["data"] == {
        "artifact_ref": artifact_ref,
        "deleted": True,
        "summary": "Deleted desktop visual QA evidence.",
    }


def test_evidence_tool_requires_internal_approval_and_preserves_reference(
    tmp_path,
    monkeypatch,
):
    del tmp_path
    from domain.tool import desktop_tools
    from domain.tool_policy.internal_context import (
        mark_tool_server_approval_context,
    )

    class FakeApi:
        @staticmethod
        def run(payload, context):
            assert payload["_handler"] == "desktop_frame_evidence"
            assert context["principal_id"] == "local-user"
            return {
                "status": "ok",
                "data": {
                    "artifact_ref": "frame_evidence_0123456789abcdef0123456789abcdef",
                    "artifacts": [
                        {
                            "artifact_ref": (
                                "frame_evidence_0123456789abcdef0123456789abcdef"
                            ),
                            "kind": "image",
                            "mime_type": "image/png",
                        }
                    ],
                },
            }

    monkeypatch.setattr(desktop_tools, "_sandbox_api", lambda: FakeApi)
    arguments = {
        "seat_id": "seat-1",
        "action": "persist",
        "frame_seq": 7,
        "purpose": "visual_qa",
    }
    denied = desktop_tools.desktop_frame_evidence(arguments, _context())
    approved_context = mark_tool_server_approval_context(_context())
    allowed = desktop_tools.desktop_frame_evidence(arguments, approved_context)

    assert denied["is_error"] is True
    assert denied["widget"]["error"]["code"] == "SANDBOX_APPROVAL_REQUIRED"
    assert allowed["data"]["artifact_ref"].startswith("frame_evidence_")


def test_tool_result_ir_keeps_opaque_image_evidence_without_path():
    from domain.tool.result_codec import encode_tool_result_to_ir_blocks

    artifact = {
        "artifact_ref": "frame_evidence_0123456789abcdef0123456789abcdef",
        "kind": "image",
        "mime_type": "image/png",
    }
    blocks = encode_tool_result_to_ir_blocks(
        {"status": "ok", "data": {"artifacts": [artifact]}},
        tool_call_id="call-1",
        name="desktop_frame_evidence",
    )

    assert blocks[0].tool_result is not None
    assert blocks[0].tool_result.artifacts == [artifact]
    assert blocks[1].type == "image"
    assert blocks[1].data == {"artifact": artifact}
