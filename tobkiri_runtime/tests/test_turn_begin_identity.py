"""Idempotent begin cannot rebind a retained request to different owner state."""

import pytest

from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict, TurnRuntime

PAYLOAD = {
    "profile_id": "defaults",
    "request_id": "request",
    "turn_id": "turn",
    "conversation_id": "conversation",
    "conversation_revision": 1,
}


@pytest.mark.parametrize(
    "patch",
    [
        {"profile_id": "other"},
        {"conversation_id": "other"},
        {"conversation_revision": 2},
        {"turn_id": "other"},
    ],
)
def test_request_identity_cannot_change(patch: dict[str, object]) -> None:
    runtime = TurnRuntime()
    before = runtime.begin(PAYLOAD)
    assert runtime.begin(PAYLOAD) == before
    with pytest.raises(TurnConflict):
        runtime.begin({**PAYLOAD, **patch})
    assert runtime.list() == [before]


@pytest.mark.parametrize("revision", [None, True, 0, -1, "1", 1.0])
def test_revision_is_never_coerced(revision: object) -> None:
    runtime = TurnRuntime()
    with pytest.raises(ValueError):
        runtime.begin({**PAYLOAD, "conversation_revision": revision})
    assert runtime.list() == []
    turn = runtime.begin(PAYLOAD)
    with pytest.raises(TurnConflict):
        runtime.transition("turn", "running", expected_revision=revision)
    assert runtime.get("turn") == turn


def test_repeat_returns_current_snapshot_without_another_event() -> None:
    runtime = TurnRuntime()
    runtime.begin(PAYLOAD)
    running = runtime.transition("turn", "running", expected_revision=1)
    assert runtime.begin(PAYLOAD) == running
    assert len(running["events"]) == 2
