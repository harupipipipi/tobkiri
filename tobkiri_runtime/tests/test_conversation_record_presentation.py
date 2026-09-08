"""Record routes cannot manufacture a Profile, revision or writable identity."""

import pytest

from ecosystem.defaultspack.defaultspack.conversation_record_presentation import (
    normalize_conversation_record,
    present_conversation_deleted,
)


@pytest.mark.parametrize(
    "patch",
    [
        {"approved": True},
        {"profile_id": "other"},
        {"conversation_id": None},
        {"expected_conversation_revision": True},
        {"expected_conversation_revision": 0},
        {"expected_conversation_revision": "1"},
        {"expected_conversation_revision": 1.5},
        {"updates": {}},
        {"updates": {"id": "other"}},
        {"updates": {"messages": []}},
        {"updates": {"tags": [1]}},
        {"updates": {"metadata": []}},
        {"updates": {"title": None}},
        {"updates": {"is_starred": 1}},
    ],
)
def test_record_update_rejects_invalid_payload(patch: dict[str, object]) -> None:
    """All malformed updates fail before owner dispatch."""
    with pytest.raises(ValueError):
        normalize_conversation_record(
            "update",
            {
                "conversation_id": "history-1",
                "expected_conversation_revision": 1,
                "updates": {"title": "Valid"},
                **patch,
            },
            profile_id="defaults",
        )


def test_record_mapping_preserves_revision_and_owner_model_field() -> None:
    """The UI model alias never becomes a silently ignored owner patch."""
    assert normalize_conversation_record(
        "update",
        {
            "conversation_id": "history-1",
            "expected_conversation_revision": 3,
            "updates": {"model": "selected"},
        },
        profile_id="defaults",
    ) == {
        "profile_id": "defaults",
        "operation": "update",
        "conversation_id": "history-1",
        "expected_conversation_revision": 3,
        "patch": {"model_reference": "selected"},
    }
    with pytest.raises(ValueError):
        normalize_conversation_record(
            "get", {"conversation_id": "history-1", "approved": True}, profile_id="defaults"
        )
    with pytest.raises(ValueError):
        normalize_conversation_record(
            "delete", {"conversation_id": "history-1"}, profile_id="defaults"
        )
    with pytest.raises(ValueError):
        present_conversation_deleted({"action": "updated"})
