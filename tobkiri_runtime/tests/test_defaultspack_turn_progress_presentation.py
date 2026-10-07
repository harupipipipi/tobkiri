"""Chat progress reads accept only bounded cursor and captured turn identities."""

import pytest

from ecosystem.defaultspack.defaultspack.turn_progress_presentation import (
    normalize_turn_progress_read,
)


def test_progress_read_converts_cursor_and_preserves_stage() -> None:
    payload = {
        "turn_id": "turn-1", "conversation_id": "conversation-1",
        "cursor": "4096", "progress_id": "a" * 64,
    }
    assert normalize_turn_progress_read(payload) == {**payload, "cursor": 4096}


@pytest.mark.parametrize("extra", [
    {"cursor": "4097"}, {"cursor": "01"}, {"cursor": True},
    {"cursor": -1}, {"progress_id": "foreign"}, {"approved": True},
    {"producer": "tool-broker"}, {"conversation_id": "../other"},
])
def test_progress_read_rejects_claims_or_invalid_scope(extra) -> None:
    with pytest.raises(ValueError):
        normalize_turn_progress_read({
            "turn_id": "turn-1", "conversation_id": "conversation-1",
            "cursor": "0", **extra,
        })
