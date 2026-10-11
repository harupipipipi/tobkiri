"""Conversation creation keeps identity, revision and Profile authority separate."""

import uuid

import pytest

from ecosystem.defaultspack.defaultspack.conversation_create_presentation import (
    normalize_conversation_create,
)


@pytest.mark.parametrize(
    "extra",
    [
        {"profile_id": "other"},
        {"approved": True},
        {"operation": "delete"},
        {"expected_revision": True},
        {"expected_revision": -1},
        {"expected_revision": "0"},
        {"expected_revision": 0.5},
        {"id": ""},
        {"id": "../escape"},
        {"id": None},
        {"metadata": []},
        {"tags": "tag"},
        {"tags": [1]},
        {"model": {}},
        {"conversation_revision": 10},
        {"messages": []},
    ],
)
def test_create_rejects_invalid_fields(extra: dict[str, object]) -> None:
    """Neither coercion nor extra fields may widen the create request."""
    with pytest.raises(ValueError):
        normalize_conversation_create(
            {"id": str(uuid.uuid4()), "expected_revision": 0, **extra},
            profile_id="defaults",
        )


def test_create_preserves_explicit_identity_and_revision() -> None:
    """Replay normalization never obtains a newer revision or a fresh ID."""
    payload = {"id": str(uuid.uuid4()), "expected_revision": 3, "model": "local"}
    expected = {
        "profile_id": "defaults",
        "operation": "create",
        "expected_revision": 3,
        "conversation": {"id": payload["id"], "model": "local"},
    }
    assert normalize_conversation_create(payload, profile_id="defaults") == expected
    assert normalize_conversation_create(payload, profile_id="defaults") == expected
    with pytest.raises(ValueError):
        normalize_conversation_create(payload, profile_id="")
