"""Saved-turn DeepThink readiness: Host-only gate across the PackVM boundary.

The guest ABI carries only ``deepthink_enabled``; model selection stays with the
owned conversation ``model_reference`` and the readiness gate runs on the Host
inside ``SavedBridgeCallbacks`` — before the first user append and again at the
AI stage — never inside the sandbox.
"""

from copy import deepcopy
from pathlib import Path
import sys
from typing import Any

import pytest

_DEFAULTSPACK = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
if str(_DEFAULTSPACK) not in sys.path:
    sys.path.insert(0, str(_DEFAULTSPACK))

from core_runtime.bootstrap.saved_bridge import (  # noqa: E402
    READINESS,
    SavedBridgeCallbacks,
    SavedDeepThinkUnavailableError,
)
from domain.chat.deepthink_preflight import (  # noqa: E402
    DeepThinkPreflightError,
    DeepThinkReadinessError,
)
from ecosystem.defaultspack.runtime import saved_conversation as saved  # noqa: E402
from tests.test_saved_bridge_callbacks import _setup as _bridge_setup  # noqa: E402
from tests.test_saved_bridge_callbacks import _frame  # noqa: E402
from tests.test_saved_conversation_steps import _setup as _steps_setup  # noqa: E402
from tests.test_saved_turn_coordinator import _Session, _run  # noqa: E402
from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains  # noqa: E402
from tobkiri_host.continuation_envelope import seal_continuation_intent  # noqa: E402
from tobkiri_host.errors import BackendUnavailableError, ProviderExecutionError  # noqa: E402
from tobkiri_host.saved_host_exchange import SavedHostExchange  # noqa: E402
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads  # noqa: E402
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input  # noqa: E402

_REPORT = {
    "ok": True,
    "chain_id": "modelpack/rumi",
    "chain_source": "model_pack",
    "member_models": ["openai/gpt-a", "anthropic/claude-b"],
    "budget": {"max_review_rounds": 2},
    "problems": [],
}


def _deepthink_outer(store_setup):
    _store, outer, calls, callbacks = store_setup
    outer.payload["request"]["deepthink_enabled"] = True
    return outer, calls, callbacks


def _gate(report: dict[str, Any] = _REPORT, calls: list | None = None):
    def run(model: str, messages: list, tools: list) -> dict[str, Any]:
        if calls is not None:
            calls.append((model, deepcopy(messages), deepcopy(tools)))
        return dict(report)

    return run


def test_input_contract_accepts_flag_and_rejects_non_bool() -> None:
    request = {
        "turn_id": "turn-1",
        "conversation_id": "conversation-1",
        "conversation_revision": 1,
        "content": "Hello",
    }
    for value in (True, False):
        initial = validate_saved_conversation_input(
            {"request": {**request, "deepthink_enabled": value}}
        )
        assert initial["request"]["deepthink_enabled"] is value
    assert "deepthink_enabled" not in validate_saved_conversation_input(
        {"request": request}
    )["request"]
    for value in (1, 0, "true", "false", None):
        with pytest.raises(ValueError, match="deepthink flag is invalid"):
            validate_saved_conversation_input(
                {"request": {**request, "deepthink_enabled": value}}
            )


def test_preflight_gate_uses_owned_model_before_any_append(tmp_path: Path) -> None:
    store_setup = _bridge_setup(tmp_path)
    store = store_setup[0]
    outer, calls, callbacks = _deepthink_outer(store_setup)
    gate_calls: list = []
    callbacks = SavedBridgeCallbacks(
        callbacks._dispatch, callbacks._require_targets, deepthink_gate=_gate(calls=gate_calls)
    )
    before = store.path.read_bytes()
    callbacks.preflight(outer)
    assert store.path.read_bytes() == before
    # The AI-route readiness probe still runs first and is marked DeepThink.
    readiness = [payload for target, payload in calls if target == READINESS]
    assert readiness == [
        {
            "model_profile_id": "model-profile-1",
            "messages": [{"role": "user", "content": "Hello"}],
            "deepthink": True,
        }
    ]
    # The gate receives the owned model_reference, never a guest-supplied model.
    assert len(gate_calls) == 1
    model, messages, tools = gate_calls[0]
    assert model == "model-profile-1"
    assert messages[-1] == {"role": "user", "content": "Hello"}
    assert tools == []


def test_preflight_without_gate_fails_closed_structured(tmp_path: Path) -> None:
    store_setup = _bridge_setup(tmp_path)
    store = store_setup[0]
    outer, calls, callbacks = _deepthink_outer(store_setup)
    before = store.path.read_bytes()
    with pytest.raises(SavedDeepThinkUnavailableError) as raised:
        callbacks.preflight(outer)
    assert raised.value.code == "DEEPTHINK_GATE_UNAVAILABLE"
    payload = raised.value.to_dict()
    assert payload["code"] == "DEEPTHINK_GATE_UNAVAILABLE"
    assert "fix" in payload and "cause" in payload
    assert store.path.read_bytes() == before


def test_preflight_gate_rejection_keeps_error_chain_payload(tmp_path: Path) -> None:
    store_setup = _bridge_setup(tmp_path)
    store = store_setup[0]
    outer, _calls, callbacks = _deepthink_outer(store_setup)

    def fail(model: str, messages: list, tools: list) -> dict[str, Any]:
        raise DeepThinkReadinessError(
            model=model,
            code="DEEPTHINK_MEMBER_PROVIDER_UNCONFIGURED",
            cause="member provider has no configured credential",
            fix="set an API key or turn DeepThink off",
            member_models=["openai/gpt-a"],
            details={"chain_id": "modelpack/rumi"},
        )

    callbacks = SavedBridgeCallbacks(
        callbacks._dispatch, callbacks._require_targets, deepthink_gate=fail
    )
    before = store.path.read_bytes()
    with pytest.raises(DeepThinkReadinessError) as raised:
        callbacks.preflight(outer)
    payload = raised.value.to_dict()
    assert payload["code"] == "DEEPTHINK_MEMBER_PROVIDER_UNCONFIGURED"
    assert payload["model"] == "model-profile-1"
    assert getattr(raised.value, "saved_user_persistence", None) is None
    assert store.path.read_bytes() == before


class _DeepThinkExchange:
    """SavedHostExchange bound to a saved request with the given flag."""

    def __init__(self, path: Path, gate=None, enabled: bool = True) -> None:
        self.store, self.outer, self.calls, callback = _bridge_setup(path)
        if enabled:
            self.outer.payload["request"]["deepthink_enabled"] = True
        self.callback = SavedBridgeCallbacks(
            callback._dispatch,
            callback._require_targets,
            deepthink_gate=gate if gate is not None else _gate(),
        )
        self.identity = ChainIdentity("domain", "host-request", "sha256:" + "a" * 64, 60)
        self.host = SavedHostExchange(
            self.identity, request_digest="sha256:" + "c" * 64,
            artifact_identity="artifact", deadline_text="60",
            chains=ContinuationChains(clock=lambda: 0),
            request=self.outer.payload["request"],
        )
        self.previous: str | None = None
        self.intent = saved.start(self.outer.payload["request"])

    def step(self, override: dict[str, Any] | None = None) -> dict[str, Any]:
        intent = self.intent
        hop = intent["hop"]
        frame = seal_continuation_intent(
            canonical_json(intent), identity=self.identity, hop=hop,
            previous_digest=self.previous, target=saved.TARGETS[hop],
            nonce=str(hop) * 48,
        )
        checked = self.host.accept({
            "kind": "tobkiri.packvm.bridge.host-request.v2",
            "protocol": "io.tobkiri.packvm.bridge.v2", "version": 2,
            "request_id": "host-request", "target_domain": "domain",
            "binding_digest": self.identity.binding_digest,
            "guest_artifact_identity": "artifact",
            "request_digest": "sha256:" + "c" * 64,
            "deadline_monotonic": "60",
            "bridge_request": strict_loads(frame.frame),
            "bridge_request_digest": frame.digest,
        })
        outcome = self.callback(self.outer, strict_loads(checked.frame))
        if override is not None:
            outcome = override
        result = self.host.result(outcome)
        self.previous = canonical_digest(result["bridge_result"])
        self.intent = saved.resume(intent["state"], outcome)
        return outcome


def test_full_exchange_binds_report_into_terminal_result(tmp_path: Path) -> None:
    exchange = _DeepThinkExchange(tmp_path)
    exchange.step()
    exchange.step()
    ai_intent = exchange.intent
    assert ai_intent["payload"]["requirements"] == {
        "request_surface": "conversation.saved",
        "deepthink": True,
    }
    outcome = exchange.step()
    assert outcome["value"]["deepthink"] == _REPORT
    exchange.step()
    assert exchange.intent["deepthink"] == _REPORT
    exchange.host.finish(exchange.intent)
    messages = exchange.store.get("conversation-1")["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]


def test_ai_stage_gate_recheck_marks_user_already_saved(tmp_path: Path) -> None:
    store_setup = _bridge_setup(tmp_path)
    store, outer, calls, callback = store_setup
    outer.payload["request"]["deepthink_enabled"] = True

    def fail(model: str, messages: list, tools: list) -> dict[str, Any]:
        raise DeepThinkReadinessError(
            model=model,
            code="DEEPTHINK_MEMBER_PROVIDER_UNCONFIGURED",
            cause="member provider lost its credential between checks",
            fix="restore the credential or turn DeepThink off",
        )

    callbacks = SavedBridgeCallbacks(
        callback._dispatch, callback._require_targets, deepthink_gate=fail
    )
    intent = saved.start(outer.payload["request"])
    intent = saved.resume(
        intent["state"],
        callbacks(outer, _frame(intent)),
    )
    before = store.path.read_bytes()
    intent = saved.resume(
        intent["state"],
        callbacks(outer, _frame(intent)),
    )
    assert intent["state"]["stage"] == "ai"
    with pytest.raises(DeepThinkReadinessError) as raised:
        callbacks(outer, _frame(intent))
    # The user append committed before the AI-stage gate ran.
    assert raised.value.saved_user_persistence == "saved"
    assert store.path.read_bytes() != before
    messages = store.get("conversation-1")["messages"]
    assert [message["role"] for message in messages] == ["user"]


def test_exchange_requires_report_only_when_enabled(tmp_path: Path) -> None:
    exchange = _DeepThinkExchange(tmp_path)
    exchange.step()
    exchange.step()
    with pytest.raises(ValueError, match="readiness report is required"):
        exchange.step({"status": "ok", "value": {"status": "ok", "output": "Hi"}})

    plain = _steps_setup(tmp_path / "plain")
    exchange2 = _DeepThinkExchange.__new__(_DeepThinkExchange)
    exchange2.__dict__["store"], exchange2.__dict__["outer"], exchange2.__dict__["calls"], callback = (
        _bridge_setup(tmp_path / "plain2")
    )
    exchange2.callback = SavedBridgeCallbacks(
        callback._dispatch, callback._require_targets, deepthink_gate=_gate()
    )
    exchange2.identity = ChainIdentity(
        "domain", "host-request", "sha256:" + "a" * 64, 60
    )
    exchange2.host = SavedHostExchange(
        exchange2.identity, request_digest="sha256:" + "c" * 64,
        artifact_identity="artifact", deadline_text="60",
        chains=ContinuationChains(clock=lambda: 0),
        request=exchange2.outer.payload["request"],
    )
    exchange2.previous = None
    exchange2.intent = saved.start(exchange2.outer.payload["request"])
    exchange2.step()
    exchange2.step()
    with pytest.raises(ValueError, match="readiness report is invalid"):
        exchange2.step(
            {
                "status": "ok",
                "value": {"status": "ok", "output": "Hi", "deepthink": dict(_REPORT)},
            }
        )


def test_forged_terminal_report_is_rejected(tmp_path: Path) -> None:
    exchange = _DeepThinkExchange(tmp_path)
    for _ in range(4):
        exchange.step()
    exchange.intent["deepthink"] = {"ok": True, "member_models": ["evil/model"]}
    with pytest.raises(ValueError, match="terminal result differs"):
        exchange.host.finish(exchange.intent)


def test_guest_requires_report_shape_matching_flag(tmp_path: Path) -> None:
    store, request = _steps_setup(tmp_path)
    enabled = saved.start({**request, "deepthink_enabled": True})
    intent = saved.resume(
        enabled["state"],
        {"status": "ok", "value": {"conversation": store.get("conversation-1")}},
    )
    intent = saved.resume(
        intent["state"],
        {
            "status": "ok",
            "value": store.append_message(
                "conversation-1",
                intent["payload"]["message"],
                expected_conversation_revision=intent["payload"]["expected_conversation_revision"],
                saved_input={"request": {**request, "deepthink_enabled": True}},
            ),
        },
    )
    assert intent["state"]["stage"] == "ai"
    # An owner response missing the report is an invalid acknowledgement;
    # the guest settles it as a failure result rather than an exception.
    missing = saved.resume(
        intent["state"],
        {"status": "ok", "value": {"status": "ok", "output": "Hi"}},
    )
    assert missing["status"] == "error"
    assert missing["error"]["code"] == "OWNER_RESPONSE_INVALID"
    invalid = saved.resume(
        intent["state"],
        {
            "status": "ok",
            "value": {"status": "ok", "output": "Hi", "deepthink": {}},
        },
    )
    assert invalid["status"] == "error"
    assert invalid["error"]["code"] == "OWNER_RESPONSE_INVALID"

    store2, request2 = _steps_setup(tmp_path / "other")
    disabled = saved.start(request2)
    intent = saved.resume(
        disabled["state"],
        {"status": "ok", "value": {"conversation": store2.get("conversation-1")}},
    )
    intent = saved.resume(
        intent["state"],
        {
            "status": "ok",
            "value": store2.append_message(
                "conversation-1",
                intent["payload"]["message"],
                expected_conversation_revision=intent["payload"]["expected_conversation_revision"],
                saved_input={"request": request2},
            ),
        },
    )
    assert intent["state"]["stage"] == "ai"
    forged = saved.resume(
        intent["state"],
        {
            "status": "ok",
            "value": {
                "status": "ok",
                "output": "Hi",
                "deepthink": dict(_REPORT),
            },
        },
    )
    assert forged["status"] == "error"
    assert forged["error"]["code"] == "OWNER_RESPONSE_INVALID"


def _deepthink_session(path: Path) -> "_Session":
    session = _Session(path)
    session.initial["request"]["deepthink_enabled"] = True
    return session


def test_execute_saved_turn_persists_readiness_reference(tmp_path: Path) -> None:
    session = _deepthink_session(tmp_path)
    session.ai_outcome = {
        "status": "ok",
        "value": {"status": "ok", "output": "Hi", "deepthink": dict(_REPORT)},
    }
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    result = _run(store, session)
    assert result["status"] == "completed"
    reference = result["turn"]["result_reference"]
    assert reference["deepthink"] == _REPORT
    assert set(reference) == {
        "conversation_id",
        "conversation_revision",
        "user_message_id",
        "assistant_message_id",
        "outcome_digest",
        "deepthink",
    }
    # The receipt path validates only the content-free reference identity.
    assert _run(store, session) == {"status": "existing", "turn": result["turn"]}


def test_execute_saved_turn_requires_report_when_enabled(tmp_path: Path) -> None:
    session = _deepthink_session(tmp_path)
    session.ai_outcome = {
        "status": "ok",
        "value": {"status": "ok", "output": "Hi"},
    }
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    result = _run(store, session)
    # Missing report makes the acknowledgement unverifiable: the guest rejects
    # it as an invalid owner response and the turn fails finitely — it never
    # completes and nothing uncertain is left to reconcile.
    assert result["status"] == "reconciliation_required"
    assert result["turn"]["status"] == "failed"
    assert result["turn"]["events"][-1]["details"] == {
        "phase": "saved_execution_failed",
        "error_code": "OWNER_RESPONSE_INVALID",
        "user_persistence": "saved",
        "assistant_persistence": "not_written",
    }
    assert result["turn"]["result_reference"] is None


def test_execute_saved_turn_settles_structured_rejection(tmp_path: Path) -> None:
    session = _deepthink_session(tmp_path)
    inner = DeepThinkReadinessError(
        model="model-profile-1",
        code="DEEPTHINK_MEMBER_PROVIDER_UNCONFIGURED",
        cause="member provider has no configured credential",
        fix="set an API key or turn DeepThink off",
        member_models=["openai/gpt-a"],
        details={"chain_id": "modelpack/rumi"},
    )
    inner.saved_user_persistence = "saved"

    def fail(value: dict) -> dict:
        transport = BackendUnavailableError(
            "macOS VZ saved bridge rejected exchange"
        )
        transport.__cause__ = inner
        raise ProviderExecutionError("provider execution failed") from transport

    session.transform = fail
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    result = _run(store, session)
    assert result["status"] == "reconciliation_required"
    turn = result["turn"]
    assert turn["status"] == "failed"
    assert turn["error"] == inner.to_dict()
    assert turn["events"][-1]["details"] == {
        "phase": "saved_execution_failed",
        "error_code": "DEEPTHINK_MEMBER_PROVIDER_UNCONFIGURED",
        "error": inner.to_dict(),
        "user_persistence": "saved",
        "assistant_persistence": "not_written",
    }
    # A finite failed turn is terminal; it never retries or reconciles forward.
    assert _run(store, session) == {"status": "existing", "turn": turn}


def test_execute_saved_turn_gate_unavailable_is_structured(tmp_path: Path) -> None:
    session = _deepthink_session(tmp_path)
    inner = SavedDeepThinkUnavailableError(
        "saved bridge DeepThink readiness gate is unavailable"
    )

    def fail(value: dict) -> dict:
        transport = BackendUnavailableError(
            "macOS VZ saved preflight rejected request"
        )
        transport.__cause__ = inner
        raise ProviderExecutionError("provider execution failed") from transport

    session.transform = fail
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    result = _run(store, session)
    assert result["status"] == "reconciliation_required"
    turn = result["turn"]
    assert turn["status"] == "failed"
    assert turn["error"]["code"] == "DEEPTHINK_GATE_UNAVAILABLE"
    assert turn["events"][-1]["details"]["user_persistence"] == "not_written"


def test_unstructured_failure_still_waits_for_reconciliation(tmp_path: Path) -> None:
    session = _deepthink_session(tmp_path)

    def lose(value: dict) -> dict:
        raise ProviderExecutionError("provider execution failed") from (
            BackendUnavailableError("transport lost")
        )

    session.transform = lose
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    result = _run(store, session)
    assert result["turn"]["status"] == "waiting"
    assert result["turn"]["error"] is None


def test_stream_error_envelope_preserves_to_dict_payload(tmp_path: Path, monkeypatch) -> None:
    """Structured domain errors keep their machine-readable fields on the wire."""
    monkeypatch.setenv(
        "RUMI_CHAT_IDEMPOTENCY_DB", str(tmp_path / "chat_idempotency.sqlite3")
    )
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CHAT_STORE_PATH",
        str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"),
    )
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module

    ChatStore._instance = None
    engine = ChatRunEngine(
        store=ChatStore(), settings_owner=None
    )
    error = DeepThinkPreflightError(
        model="model-profile-1",
        cause="selected model is not chain-capable",
        fix="select a review_chain model pack or turn DeepThink off",
    )

    def explode(self, input_data, context, *, stream_mode=True):
        if False:
            yield {}
        raise error

    monkeypatch.setattr(engine_module.ChatRunEngine, "_stream_once", explode)
    events = list(
        engine.stream(
            {
                "conversation_id": "conversation-1",
                "idempotency_key": "deepthink-test-1",
                "message": {"role": "user", "content": "Hello"},
            },
            {},
        )
    )
    assert events[-1]["type"] == "error"
    assert events[-1]["data"]["error"] == error.to_dict()
    assert events[-1]["data"]["terminal"] is True
    ChatStore._instance = None


def test_stream_error_envelope_falls_back_for_plain_errors(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "RUMI_CHAT_IDEMPOTENCY_DB", str(tmp_path / "chat_idempotency.sqlite3")
    )
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CHAT_STORE_PATH",
        str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"),
    )
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    import domain.chat.stream_engine as engine_module

    ChatStore._instance = None
    engine = ChatRunEngine(store=ChatStore(), settings_owner=None)

    def explode(self, input_data, context, *, stream_mode=True):
        if False:
            yield {}
        raise RuntimeError("plain failure")

    monkeypatch.setattr(engine_module.ChatRunEngine, "_stream_once", explode)
    events = list(
        engine.stream(
            {
                "conversation_id": "conversation-1",
                "idempotency_key": "deepthink-test-2",
                "message": {"role": "user", "content": "Hello"},
            },
            {},
        )
    )
    assert events[-1]["data"]["error"] == {
        "code": "CHAT_RUN_FAILED",
        "message": "plain failure",
    }
    ChatStore._instance = None
