"""Regression contract for issue #991 mobile conversation persistence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def _mobile_source() -> str:
    """Return all checked-in mobile Dart source as one inspection surface."""
    mobile_root = REPOSITORY_ROOT / "tobkiri_mobile"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((mobile_root / "lib").rglob("*.dart"))
    )


def _new_owner(tmp_path: Path) -> Any:
    """Return the profile-bound canonical conversation owner."""
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationStore,
    )

    return ConversationStore("default", user_data_root=tmp_path)


def test_mobile_has_no_second_conversation_persistence_authority() -> None:
    """Keep the retired optimistic local ChatStore outside the v4 client."""
    mobile_root = REPOSITORY_ROOT / "tobkiri_mobile"
    source = _mobile_source()

    assert not list((mobile_root / "lib").rglob("chat_store.dart"))
    assert "class ChatStore" not in source
    assert "rumi_chat.conversations.v1" not in source
    assert "rumi_chat.active_id.v1" not in source
    pubspec = (mobile_root / "pubspec.yaml").read_text(encoding="utf-8")
    assert "shared_preferences" not in pubspec


def test_mobile_conversation_routes_are_scoped_host_contracts() -> None:
    """Bind mobile conversation access to authenticated host operations."""
    from ecosystem.defaultspack.domain.mobile.contract import (
        MOBILE_ROUTE_CONTRACTS,
    )

    conversation_routes = {
        (route.method, route.pattern): route
        for route in MOBILE_ROUTE_CONTRACTS
        if "/api/mobile/v1/conversations" in route.pattern
    }

    assert (
        conversation_routes[
            ("GET", "/api/mobile/v1/conversations")
        ].device_scope
        == "chat.read"
    )
    assert (
        conversation_routes[
            ("POST", "/api/mobile/v1/conversations")
        ].device_scope
        == "chat.write"
    )
    assert (
        conversation_routes[
            ("POST", "/api/mobile/v1/conversations/{id}/export")
        ].device_scope
        == "chat.read"
    )
    assert all(
        route.block_module or route.flow_id
        for route in conversation_routes.values()
    )


def test_host_write_failure_is_explicit_and_keeps_prior_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never retain an optimistic mutation after durable publication fails."""
    from ecosystem.rumi_conversation_store_pack.runtime import store as module

    owner = _new_owner(tmp_path)
    created = owner.create(
        {"id": "conversation-1", "title": "Saved"},
        expected_revision=0,
    )
    original = owner.path.read_bytes()

    def fail_write(path: Path, value: object) -> None:
        del path, value
        raise OSError("quota exceeded")

    monkeypatch.setattr(module, "_atomic_json", fail_write)
    with pytest.raises(OSError, match="quota exceeded"):
        owner.update(
            "conversation-1",
            {"title": "Unsaved"},
            expected_conversation_revision=created["conversation"][
                "conversation_revision"
            ],
        )

    assert owner.path.read_bytes() == original
    assert _new_owner(tmp_path).get("conversation-1")["title"] == "Saved"


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        (b"{not-json", json.JSONDecodeError),
        (
            json.dumps(
                {
                    "version": "rumi.conversation-store.v999",
                    "profile_id": "default",
                    "revision": 7,
                    "conversations": {},
                }
            ).encode("utf-8"),
            ValueError,
        ),
    ],
)
def test_corrupt_or_incompatible_load_cannot_be_overwritten(
    tmp_path: Path,
    payload: bytes,
    error_type: type[Exception],
) -> None:
    """Distinguish invalid data from empty and preserve it for recovery."""
    owner = _new_owner(tmp_path)
    owner.path.parent.mkdir(parents=True, exist_ok=True)
    owner.path.write_bytes(payload)

    with pytest.raises(error_type):
        owner.snapshot()
    with pytest.raises(error_type):
        owner.create({"id": "replacement"}, expected_revision=0)

    assert owner.path.read_bytes() == payload


def test_unreadable_load_failure_is_not_treated_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate storage reads that fail before any mutation is attempted."""
    owner = _new_owner(tmp_path)
    owner.create({"id": "conversation-1"}, expected_revision=0)
    original = owner.path.read_bytes()
    original_read_text = Path.read_text

    def fail_owner_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == owner.path:
            raise PermissionError("conversation store unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_owner_read)
    with pytest.raises(PermissionError, match="unreadable"):
        owner.snapshot()
    with pytest.raises(PermissionError, match="unreadable"):
        owner.delete("conversation-1", expected_conversation_revision=1)

    assert owner.path.read_bytes() == original


def test_interrupted_atomic_publish_preserves_saved_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model app termination at publish and retain the previous snapshot."""
    from ecosystem.rumi_conversation_store_pack.runtime import store as module

    owner = _new_owner(tmp_path)
    created = owner.create({"id": "conversation-1"}, expected_revision=0)
    original = owner.path.read_bytes()

    def fail_publish(source: str, destination: Path) -> None:
        del source, destination
        raise OSError("interrupted before replace")

    monkeypatch.setattr(module.os, "replace", fail_publish)
    with pytest.raises(OSError, match="interrupted"):
        owner.append_message(
            "conversation-1",
            {"id": "message-1", "role": "assistant", "content": "partial"},
            expected_conversation_revision=created["conversation"][
                "conversation_revision"
            ],
        )

    assert owner.path.read_bytes() == original
    assert _new_owner(tmp_path).get("conversation-1")["messages"] == []
    assert not list(owner.path.parent.glob(".conversations.json-*.tmp"))


def test_streamed_delta_failure_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not commit an incomplete streamed-message delta after write failure."""
    from ecosystem.rumi_conversation_store_pack.runtime import store as module

    owner = _new_owner(tmp_path)
    created = owner.create({"id": "conversation-1"}, expected_revision=0)
    appended = owner.append_message(
        "conversation-1",
        {
            "id": "message-1",
            "role": "assistant",
            "content": "saved prefix",
            "status": "pending",
        },
        expected_conversation_revision=created["conversation"][
            "conversation_revision"
        ],
    )

    def fail_write(path: Path, value: object) -> None:
        del path, value
        raise OSError("device full")

    monkeypatch.setattr(module, "_atomic_json", fail_write)
    with pytest.raises(OSError, match="device full"):
        owner.mutate_message(
            "conversation-1",
            "message-1",
            expected_conversation_revision=appended["conversation_revision"],
            patch={"content": "unsaved suffix", "status": "complete"},
        )

    restored = _new_owner(tmp_path).get("conversation-1")
    assert restored["messages"][0]["content"] == "saved prefix"
    assert restored["messages"][0]["status"] == "pending"
