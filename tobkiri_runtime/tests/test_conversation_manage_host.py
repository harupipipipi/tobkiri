"""Conversation action hooks preserve captured scope and owner revisions."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_conversation_store_pack.runtime.manage_host import (
    CONTRACT_ID,
    FUNCTION_ID,
    OPERATION_ID,
    ConversationManageHostFactoryV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import (
    ConversationConflict,
    ConversationStore,
)


def context(root: Path) -> Any:
    """Construct one exact action binding without runtime registration."""
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
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


def create_payload() -> dict[str, Any]:
    """Return a mutation targeting the empty store's explicit revision."""
    return {
        "profile_id": "defaults",
        "operation": "create",
        "conversation": {"id": "one", "title": "First"},
        "expected_revision": 0,
    }


def test_action_round_trip_and_stale_replay(tmp_path: Path) -> None:
    invoke = ConversationManageHostFactoryV4().capture(context(tmp_path)).contributions[0].invoke
    assert list(tmp_path.iterdir()) == []
    assert invoke(OPERATION_ID, create_payload(), None)["action"] == "created"
    store = ConversationStore("defaults", user_data_root=tmp_path)
    before = store.snapshot()
    with pytest.raises(ConversationConflict):
        invoke(OPERATION_ID, create_payload(), None)
    assert store.snapshot() == before
    result = invoke(
        OPERATION_ID,
        {
            "profile_id": "defaults",
            "operation": "update",
            "conversation_id": "one",
            "patch": {"title": "Changed"},
            "expected_conversation_revision": 1,
        },
        None,
    )
    assert result["conversation"]["title"] == "Changed"
    assert ConversationStore("other", user_data_root=tmp_path).snapshot()["conversations"] == []
    invoke(
        OPERATION_ID,
        {
            "profile_id": "defaults",
            "operation": "delete",
            "conversation_id": "one",
            "expected_conversation_revision": 2,
        },
        None,
    )
    assert store.snapshot()["conversations"] == []


@pytest.mark.parametrize(
    "patch",
    [
        {"profile_id": "other"},
        {"approved": True},
        {"root": "/tmp"},
        {"operation": "migration.apply"},
        {"expected_revision": True},
        {"expected_revision": "0"},
        {"expected_revision": -1},
        {"conversation": []},
    ],
)
def test_invalid_actions_do_not_create_state(tmp_path: Path, patch: dict[str, Any]) -> None:
    invoke = ConversationManageHostFactoryV4().capture(context(tmp_path)).contributions[0].invoke
    with pytest.raises((ValueError, PermissionError)):
        invoke(OPERATION_ID, create_payload() | patch, None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "field", ["domain_ids", "profile_id", "user_data_root", "provider_bindings"]
)
def test_capture_rejects_incomplete_scope(tmp_path: Path, field: str) -> None:
    captured = context(tmp_path)
    setattr(
        captured,
        field,
        {} if field == "domain_ids" else () if field == "provider_bindings" else None,
    )
    with pytest.raises(PermissionError):
        ConversationManageHostFactoryV4().capture(captured)
    assert list(tmp_path.iterdir()) == []
