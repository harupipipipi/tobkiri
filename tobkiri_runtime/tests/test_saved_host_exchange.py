"""Host result binding with real owner writes; AI/transport are explicit adapters."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from ecosystem.defaultspack.runtime import saved_conversation as saved
from tests.test_saved_bridge_callbacks import _setup
from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains
from tobkiri_host.continuation_envelope import seal_continuation_intent
from tobkiri_host.saved_host_exchange import SavedHostExchange
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads


class _Exchange:
    def __init__(self, path: Path) -> None:
        self.store, self.outer, self.calls, self.callback = _setup(path)
        self.identity = ChainIdentity("domain", "host-request", "sha256:" + "a" * 64, 60)
        self.host = SavedHostExchange(
            self.identity, request_digest="sha256:" + "c" * 64,
            artifact_identity="artifact", deadline_text="60",
            chains=ContinuationChains(clock=lambda: 0),
        )
        self.previous: str | None = None
        self.intent = saved.start(self.outer.payload["request"])

    def step(self, override: dict[str, Any] | None = None) -> dict[str, Any]:
        intent = self.intent
        hop = intent["hop"]
        frame = seal_continuation_intent(
            canonical_json(intent), identity=self.identity, hop=hop,
            previous_digest=self.previous, target=saved.TARGETS[hop], nonce=str(hop) * 48,
        )
        checked = self.host.accept({
            "kind": "tobkiri.packvm.bridge.host-request.v2",
            "protocol": "io.tobkiri.packvm.bridge.v2", "version": 2,
            "request_id": "host-request", "target_domain": "domain",
            "binding_digest": self.identity.binding_digest,
            "guest_artifact_identity": "artifact", "request_digest": "sha256:" + "c" * 64,
            "deadline_monotonic": "60", "bridge_request": strict_loads(frame.frame),
            "bridge_request_digest": frame.digest,
        })
        outcome = self.callback(self.outer, strict_loads(checked.frame))
        if override is not None:
            outcome = override
        result = self.host.result(outcome)
        self.previous = canonical_digest(result["bridge_result"])
        self.intent = saved.resume(intent["state"], outcome)
        return outcome


@pytest.mark.parametrize("output", ["Hi", [{"type": "text", "text": "Hi"}]])
def test_host_accepts_actual_ai_content_and_owner_ack(tmp_path: Path, output: Any) -> None:
    exchange = _Exchange(tmp_path)
    exchange.step()
    exchange.step()
    exchange.step({"status": "ok", "value": {"status": "ok", "output": output}})
    exchange.step()
    exchange.host.finish(exchange.intent)
    assert exchange.store.get("conversation-1")["messages"][-1]["content"] == output


@pytest.mark.parametrize("change", ["content", "revision", "bool_revision"])
def test_guest_assistant_change_is_rejected_before_owner_write(
    tmp_path: Path, change: str,
) -> None:
    exchange = _Exchange(tmp_path)
    for _ in range(3):
        exchange.step()
    if change == "content":
        exchange.intent["payload"]["message"]["content"] = "not the provider output"
    else:
        exchange.intent["payload"]["expected_conversation_revision"] = (
            True if change == "bool_revision" else 99
        )
    before = exchange.store.path.read_bytes()
    calls = len(exchange.calls)
    with pytest.raises(ValueError, match="assistant differs"):
        exchange.step()
    assert exchange.store.path.read_bytes() == before
    assert len(exchange.calls) == calls


@pytest.mark.parametrize("value", [
    {"status": "error", "output": "Hi"},
    {"status": "ok", "output": "Hi", "tool_intents": [{"name": "tool"}]},
    {"status": "ok", "output": ""},
    {"status": "ok", "output": {"text": "Hi"}},
])
def test_guest_cannot_turn_noncompletion_into_assistant(
    tmp_path: Path, value: dict[str, Any],
) -> None:
    exchange = _Exchange(tmp_path)
    exchange.step()
    exchange.step()
    state = deepcopy(exchange.intent["state"])
    exchange.step({"status": "ok", "value": value})
    # Simulate a malicious sandbox ignoring the actual Host result.
    exchange.intent = saved.resume(state, {
        "status": "ok", "value": {"status": "ok", "output": "Hi"},
    })
    before = exchange.store.path.read_bytes()
    with pytest.raises(ValueError, match="assistant differs"):
        exchange.step()
    assert exchange.store.path.read_bytes() == before


@pytest.mark.parametrize("change", ["content", "revision", "turn_id"])
def test_guest_terminal_ack_must_match_owner_result(tmp_path: Path, change: str) -> None:
    exchange = _Exchange(tmp_path)
    for _ in range(4):
        exchange.step()
    if change == "content":
        exchange.intent["message"]["content"] = "forged terminal content"
    elif change == "revision":
        exchange.intent["conversation_revision"] += 1
    else:
        exchange.intent["turn_id"] = "other-turn"
    with pytest.raises(ValueError, match="terminal result differs"):
        exchange.host.finish(exchange.intent)


def test_host_fingerprints_do_not_follow_later_result_mutation(tmp_path: Path) -> None:
    exchange = _Exchange(tmp_path)
    exchange.step()
    exchange.step()
    outcome = exchange.step()
    outcome["value"]["output"] = "mutated after host result"
    exchange.intent["payload"]["message"]["content"] = outcome["value"]["output"]
    with pytest.raises(ValueError, match="assistant differs"):
        exchange.step()
