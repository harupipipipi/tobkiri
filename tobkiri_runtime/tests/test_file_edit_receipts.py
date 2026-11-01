"""Real line deltas and strict, content-free confirmed edit metadata."""

import json

import pytest

from core_runtime.file_edit_receipts import (
    MAX_DIFF_BYTES,
    committed_file_edit_receipt,
    file_edit_receipt_from_tool_result,
    line_diff_stats,
    validate_file_edit_receipt,
)


def receipt(**changes: object) -> dict[str, object]:
    """Build metadata from exact approved-owner bytes for schema tests."""
    value = committed_file_edit_receipt(
        mutation_id="mutation-1",
        operation="create",
        profile_id="profile",
        workspace_id="workspace",
        root_identity=("/private/workspace", 1, 2),
        frame_identity="actual-frame",
        path="src/demo.py",
        before=None,
        after=b"one\ntwo\n",
    )
    return {**value, **changes}


@pytest.mark.parametrize(
    "before,after,added,deleted",
    [
        (None, b"one\ntwo\n", 2, 0),
        (b"one\ntwo\n", None, 0, 2),
        (b"one\n", b"one\ntwo\n", 1, 0),
        (b"one\ntwo\n", b"one\nupdated\nnew\n", 2, 1),
        (b"one\n", b"one\n", 0, 0),
        (b"one", b"one\n", 1, 1),
        (b"one\r\n", b"one\n", 1, 1),
        (b"", b"", 0, 0),
        (b"a\na\na\n", b"a\nb\na\n", 1, 1),
    ],
)
def test_exact_create_delete_append_update_and_newline_deltas(
    before: bytes | None, after: bytes | None, added: int, deleted: int
) -> None:
    assert line_diff_stats(before, after) == {
        "status": "available",
        "lines_added": added,
        "lines_deleted": deleted,
    }


@pytest.mark.parametrize(
    "before,after,options,reason",
    [
        (None, b"text", {"preimage_known": False}, "unknown"),
        (b"\x00binary", b"text", {}, "binary"),
        (b"\xff", b"text", {}, "binary"),
        (None, b"x" * (MAX_DIFF_BYTES + 1), {}, "too_large"),
        (b"x\n" * 1001, b"y\n" * 1001, {}, "too_large"),
        (None, b"text", {"encoding": "latin-1"}, "unsupported_encoding"),
        (None, b"public", {"sensitive": True}, "sensitive"),
        (None, b"password=example-test-secret\n", {}, "sensitive"),
        (None, b"-----BEGIN PRIVATE KEY-----\nexample\n", {}, "sensitive"),
    ],
)
def test_unavailable_is_explicit_without_content(
    before: bytes | None,
    after: bytes | None,
    options: dict[str, object],
    reason: str,
) -> None:
    assert line_diff_stats(before, after, **options) == {
        "status": "unavailable",
        "reason": reason,
    }


def test_scope_ids_hide_root_and_keep_file_identity_between_saves() -> None:
    first = receipt()
    second = committed_file_edit_receipt(
        mutation_id="mutation-2",
        operation="write",
        profile_id="profile",
        workspace_id="workspace",
        root_identity=("/private/workspace", 1, 2),
        frame_identity="actual-frame",
        path="src/demo.py",
        before=b"one\ntwo\n",
        after=b"one\nthree\n",
    )
    assert first["file_id"] == second["file_id"]
    assert first["receipt_id"] != second["receipt_id"]
    assert first["sequence"] < second["sequence"]
    assert first["frame_id"] == second["frame_id"]
    assert "/private/workspace" not in json.dumps(first)
    assert "one" not in json.dumps(first)


def test_rename_keeps_origin_identity_and_separate_workspace_does_not() -> None:
    original = receipt()
    moved = committed_file_edit_receipt(
        mutation_id="move",
        operation="move",
        profile_id="profile",
        workspace_id="workspace",
        root_identity=("/private/workspace", 1, 2),
        path="src/renamed.py",
        previous_path="src/demo.py",
        before=b"one\ntwo\n",
        after=b"one\ntwo\n",
    )
    other = committed_file_edit_receipt(
        mutation_id="other",
        operation="create",
        profile_id="profile",
        workspace_id="other-workspace",
        root_identity=("/private/workspace", 1, 2),
        path="src/demo.py",
        before=None,
        after=b"one\ntwo\n",
    )
    assert moved["file_id"] == original["file_id"]
    assert other["file_id"] != original["file_id"]
    assert moved["stats"] == {
        "status": "available",
        "lines_added": 0,
        "lines_deleted": 0,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "private"},
        {"authority_receipt": "never-show"},
        {"schema_version": True},
        {"status": "pending"},
        {"status": "failed"},
        {"operation": []},
        {"operation": "read"},
        {"path": "../escape"},
        {"path": "/absolute"},
        {"path": "C:/absolute"},
        {"path": "a//b"},
        {"sequence": -1},
        {"sequence": True},
        {"sequence": 2**53},
        {"occurred_at_ms": 1.5},
        {"frame_id": ""},
        {"stats": {"status": "available", "lines_added": None, "lines_deleted": 0}},
        {"stats": {"status": "available", "lines_added": True, "lines_deleted": 0}},
        {"stats": {"status": "unavailable", "reason": []}},
        {"stats": {"status": "unavailable", "reason": "binary", "content": "private"}},
        {"previous_path": "another.py"},
    ],
)
def test_malformed_receipt_fails_closed(changes: dict[str, object]) -> None:
    assert validate_file_edit_receipt(receipt(**changes)) is None


def test_tool_result_requires_explicit_matching_success_receipt() -> None:
    value = receipt()
    payload = {
        "created": True,
        "path": value["path"],
        "workspace_id": "workspace",
        "file_edit_receipt": value,
    }
    wrapped = {
        "status": "ok",
        "data": {
            "is_error": False,
            "result": json.dumps(payload),
            "widget": None,
        },
    }
    assert file_edit_receipt_from_tool_result("coding_file_create", wrapped) == value
    for name in ("list_modules", "coding_file_read", "coding_file_patch", "unknown"):
        assert file_edit_receipt_from_tool_result(name, wrapped) is None
    for options in (
        {"is_error": True},
        {"cancelled": True},
        {"status": "denied"},
        {"approval_required": True},
        {"status": "pending_approval"},
    ):
        assert (
            file_edit_receipt_from_tool_result("coding_file_create", {**wrapped, **options}) is None
        )
    assert (
        file_edit_receipt_from_tool_result("coding_file_create", {**payload, "path": "wrong.py"})
        is None
    )
    assert (
        file_edit_receipt_from_tool_result(
            "coding_file_create",
            {"created": True, "path": "Modules.json", "diff": "+100"},
        )
        is None
    )
