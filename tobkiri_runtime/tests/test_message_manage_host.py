"""Captured owner message actions reject replay and identity widening."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_conversation_store_pack.runtime.message_host import (
    CONTRACT_ID,
    FUNCTION_ID,
    OPERATION_ID,
    MessageManageHostFactoryV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import (
    ConversationConflict,
    ConversationStore,
)


def _context(root: Path) -> Any:
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        catalog_bindings=(),
        provider_bindings=(
            SimpleNamespace(
                function=SimpleNamespace(function_id=FUNCTION_ID, implementation_digest="impl"),
                operation=SimpleNamespace(
                    contract_id=CONTRACT_ID, operation_id=OPERATION_ID, contract_version="1.0.0"
                ),
                principal_ref=SimpleNamespace(value="principal"),
                artifact=SimpleNamespace(digest="artifact"),
            ),
        ),
        domain_ids={(CONTRACT_ID, OPERATION_ID, "principal"): "domain"},
    )


def _payload(**extra: object) -> dict[str, object]:
    return {
        "profile_id": "defaults",
        "operation": "append",
        "conversation_id": "one",
        "expected_conversation_revision": 1,
        "message": {"id": "user-1", "role": "user", "content": "Hello"},
        **extra,
    }


@pytest.mark.parametrize("caller", ["saved-principal", "ordinary-principal"])
def test_saved_append_receipt_requires_captured_saved_caller(tmp_path: Path, caller: str) -> None:
    from ecosystem.defaultspack.runtime import saved_conversation
    from tests.test_saved_conversation_steps import _setup

    store, request = _setup(tmp_path)
    context = _context(tmp_path)
    context.catalog_bindings = (SimpleNamespace(
        function=SimpleNamespace(function_id="defaultspack.conversation.saved"),
        principal_ref=SimpleNamespace(value="saved-principal"),
    ),)
    invoke = MessageManageHostFactoryV4().capture(context).contributions[0].invoke
    intent = saved_conversation.start(request)
    intent = saved_conversation.resume(intent["state"], {
        "status": "ok", "value": {"conversation": store.get("conversation-1")},
    })
    payload = {
        **intent["payload"], "profile_id": "defaults", "operation": "append_saved",
        "saved_input": {"request": request},
    }
    invocation = SimpleNamespace(envelope=SimpleNamespace(
        context=SimpleNamespace(caller_principal=SimpleNamespace(value=caller)),
    ))
    if caller == "ordinary-principal":
        before = store.path.read_bytes()
        with pytest.raises(PermissionError, match="captured saved caller"):
            invoke(OPERATION_ID, payload, invocation)
        assert store.path.read_bytes() == before
        assert store.saved_receipt("turn-1") is None
    else:
        assert invoke(OPERATION_ID, payload, invocation)["conversation_revision"] == 2
        assert store.saved_receipt("turn-1")["user_revision"] == 2


def test_message_actions_round_trip_and_replay(tmp_path: Path) -> None:
    """All actions use the real owner lock/revision and stable message IDs."""
    captured = MessageManageHostFactoryV4().capture(_context(tmp_path))
    assert list(tmp_path.iterdir()) == []
    invoke = captured.contributions[0].invoke
    store = ConversationStore("defaults", user_data_root=tmp_path)
    store.create({"id": "one"}, expected_revision=0)
    assert invoke(OPERATION_ID, _payload(), None)["conversation_revision"] == 2
    before = store.path.read_bytes()
    for payload in (_payload(), _payload(expected_conversation_revision=2)):
        with pytest.raises(ConversationConflict):
            invoke(OPERATION_ID, payload, None)
        assert store.path.read_bytes() == before
    base = {"profile_id": "defaults", "conversation_id": "one"}
    result = invoke(
        OPERATION_ID,
        {
            **base,
            "operation": "update",
            "message_id": "user-1",
            "patch": {"content": "Edited"},
            "expected_conversation_revision": 2,
        },
        None,
    )
    assert result["message"]["content"] == "Edited"
    invoke(
        OPERATION_ID,
        {
            **base,
            "operation": "replace",
            "messages": [{"id": "assistant-1", "role": "assistant", "content": "Response"}],
            "expected_conversation_revision": 3,
        },
        None,
    )
    invoke(
        OPERATION_ID,
        {
            **base,
            "operation": "delete",
            "message_id": "assistant-1",
            "expected_conversation_revision": 4,
        },
        None,
    )
    assert store.get("one")["messages"] == []
    assert store.get("one")["conversation_revision"] == 5
    assert not ConversationStore("other", user_data_root=tmp_path).path.exists()
    captured.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"profile_id": "other"},
        {"approved": True},
        {"root": "/tmp/other"},
        {"operation": "migrate"},
        {"expected_conversation_revision": True},
        {"expected_conversation_revision": "1"},
        {"expected_conversation_revision": 1.5},
        {"expected_conversation_revision": 0},
        {"message": {}},
        {"message": {"id": None}},
        {"message": {"id": 1}},
        {"message": []},
        {"conversation_id": None},
    ],
)
def test_invalid_message_actions_do_not_write(tmp_path: Path, extra: dict[str, object]) -> None:
    invoke = MessageManageHostFactoryV4().capture(_context(tmp_path)).contributions[0].invoke
    store = ConversationStore("defaults", user_data_root=tmp_path)
    store.create({"id": "one"}, expected_revision=0)
    before = store.path.read_bytes()
    with pytest.raises((ValueError, PermissionError)):
        invoke(OPERATION_ID, _payload(**extra), None)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "field", ["profile_id", "user_data_root", "provider_bindings", "domain_ids"]
)
def test_message_capture_requires_exact_binding(tmp_path: Path, field: str) -> None:
    context = _context(tmp_path)
    setattr(
        context,
        field,
        {"profile_id": "", "user_data_root": None, "provider_bindings": (), "domain_ids": {}}[
            field
        ],
    )
    with pytest.raises(PermissionError):
        MessageManageHostFactoryV4().capture(context)
    assert list(tmp_path.iterdir()) == []


def test_invalid_replace_and_patch_leave_existing_messages_unchanged(tmp_path: Path) -> None:
    """Malformed replacement rows must not be silently dropped into a write."""
    invoke = MessageManageHostFactoryV4().capture(_context(tmp_path)).contributions[0].invoke
    store = ConversationStore("defaults", user_data_root=tmp_path)
    store.create({"id": "one"}, expected_revision=0)
    invoke(OPERATION_ID, _payload(), None)
    before = store.path.read_bytes()
    base = {"profile_id": "defaults", "conversation_id": "one", "expected_conversation_revision": 2}
    for fields in (
        {"operation": "replace", "messages": [None]},
        {"operation": "replace", "messages": [{"content": "Missing ID"}]},
        {"operation": "update", "message_id": "user-1", "patch": {"id": "other"}},
        {"operation": "update", "message_id": "user-1", "patch": {}},
        {"operation": "delete", "message_id": "user-1", "approved": True},
    ):
        with pytest.raises((ValueError, PermissionError)):
            invoke(OPERATION_ID, {**base, **fields}, None)
        assert store.path.read_bytes() == before
    with pytest.raises(PermissionError):
        invoke("other", _payload(), None)
