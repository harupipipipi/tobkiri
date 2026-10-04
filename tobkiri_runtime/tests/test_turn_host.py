"""Captured turn contracts share persistence without ambient Profile authority."""

from contextlib import nullcontext
from pathlib import Path
import copy
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_turn_runtime_pack.runtime.host import TurnHostFactoryV4
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
from tobkiri_protocol.canonical import canonical_digest


def _cancellation() -> Any:
    return SimpleNamespace(track=lambda _: nullcontext())


def _context(root: Path, factory: TurnHostFactoryV4) -> Any:
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(
            SimpleNamespace(
                function=SimpleNamespace(
                    function_id=factory.function_id, implementation_digest="impl"
                ),
                operation=SimpleNamespace(
                    contract_id=factory.contract_id,
                    operation_id=factory.operation_id,
                    contract_version="1.0.0",
                ),
                principal_ref=SimpleNamespace(value="principal"),
                artifact=SimpleNamespace(digest="artifact"),
            ),
        ),
        domain_ids={(factory.contract_id, factory.operation_id, "principal"): "domain"},
    )


def _invoke(root: Path, kind: str, **values: Any) -> Any:
    factory = TurnHostFactoryV4(kind)
    captured = factory.capture(_context(root, factory))
    try:
        return captured.contributions[0].invoke(
            factory.operation_id, {"profile_id": "defaults", **values}, None
        )
    finally:
        captured.close()


BEGIN = {
    "operation": "begin",
    "turn_id": "turn",
    "request_id": "request",
    "conversation_id": "conversation",
    "conversation_revision": 1,
}

SAVED_INPUT = {"request": {
    "turn_id": "saved-turn", "conversation_id": "conversation",
    "conversation_revision": 1, "content": "original private text",
}}


def _seed_running_saved_turn(root: Path) -> None:
    result = _invoke(root, "lifecycle", operation="claim_saved", **SAVED_INPUT)
    assert result["claimed"] is True
    assert result["turn"]["status"] == "running"


@pytest.mark.parametrize("scope_exited", [False, True])
def test_stop_factory_does_not_treat_host_scope_exit_as_nested_termination(
    tmp_path: Path,
    scope_exited: bool,
) -> None:
    _seed_running_saved_turn(tmp_path)
    completed = threading.Event()
    if scope_exited:
        completed.set()
    requests: list[str] = []
    fences: list[str] = []

    def request(turn_id: str) -> Any:
        requests.append(turn_id)
        return SimpleNamespace(
            completed=completed,
            wait_for_verified_drain=lambda _deadline: False,
        )

    invocation = SimpleNamespace(
        cancellation=SimpleNamespace(
            active_for=lambda turn_id: turn_id == "saved-turn",
            can_request=lambda turn_id: turn_id == "saved-turn",
            request=request,
        ),
        assert_current=lambda: fences.append("checked"),
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 1),
    )
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke

    result = invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)

    assert result == {
        "status": "cancellation_requested",
        "turn_id": "saved-turn",
        "stopped": False,
    }
    assert requests == ["saved-turn"]
    # The stop Host checks capture freshness before durable resolution and
    # again after requesting the scoped cancellation handle.
    assert fences == ["checked", "checked", "checked"]
    observed = _invoke(tmp_path, "resource", operation="get", turn_id="saved-turn")
    assert observed["status"] == "running"
    assert observed["events"][-1] == {
        "sequence": 2,
        "name": "turn.cancellation_requested",
        "at": observed["events"][-1]["at"],
        "details": {"phase": "nested_cancellation_requested"},
    }


def test_stop_factory_returns_confirmed_only_for_private_verified_drain(
    tmp_path: Path,
) -> None:
    """The turn contract projects only a bounded Host drain confirmation."""

    _seed_running_saved_turn(tmp_path)

    requests: list[str] = []
    fences: list[str] = []

    def request(turn_id: str) -> Any:
        requests.append(turn_id)
        return SimpleNamespace(
            completed=threading.Event(),
            wait_for_verified_drain=lambda _deadline: True,
        )

    invocation = SimpleNamespace(
        cancellation=SimpleNamespace(
            active_for=lambda turn_id: turn_id == "saved-turn",
            can_request=lambda turn_id: turn_id == "saved-turn",
            request=request,
        ),
        assert_current=lambda: fences.append("checked"),
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 1),
    )
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke

    result = invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)

    assert result == {
        "status": "stopped_confirmed",
        "turn_id": "saved-turn",
        "stopped": True,
    }
    assert requests == ["saved-turn"]
    # Confirmation adds a final freshness check before committing cancelled.
    assert fences == ["checked"] * 4
    observed = _invoke(tmp_path, "resource", operation="get", turn_id="saved-turn")
    assert observed["status"] == "cancelled"
    assert [event["name"] for event in observed["events"][-2:]] == [
        "turn.cancellation_requested",
        "turn.cancelled",
    ]
    assert observed["events"][-1]["details"] == {
        "phase": "nested_cancellation_confirmed",
    }


def test_queued_guidance_stop_rechecks_capture_after_durable_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale capture cannot report success after cancelling queued guidance."""

    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

    begun = _invoke(tmp_path, "lifecycle", **BEGIN)
    running = _invoke(
        tmp_path,
        "lifecycle",
        operation="transition",
        turn_id="turn",
        expected_revision=begun["revision"],
        status="running",
    )
    guided = _invoke(
        tmp_path,
        "lifecycle",
        operation="steer",
        turn_id="turn",
        expected_revision=running["revision"],
        guidance={
            "prompt": "Continue.",
            "target_type": "conversation",
            "target_id": "conversation",
            "conversation_id": "conversation",
            "visible": True,
            "auto_send": True,
            "metadata": {},
        },
    )
    _invoke(
        tmp_path,
        "lifecycle",
        operation="transition",
        turn_id="turn",
        expected_revision=guided["revision"],
        status="completed",
        details={"result_reference": {"conversation_revision": 3}},
    )
    stale = False
    checks = 0
    original = DurableTurnRuntime.prepare_guidance_stop

    def prepare(
        self: DurableTurnRuntime,
        turn_id: str,
        *,
        expected_active_turn_id: str | None,
    ) -> dict[str, Any] | None:
        nonlocal stale
        result = original(
            self,
            turn_id,
            expected_active_turn_id=expected_active_turn_id,
        )
        stale = True
        return result

    def assert_current() -> None:
        nonlocal checks
        checks += 1
        if stale:
            raise TimeoutError("captured stop invocation expired")

    monkeypatch.setattr(DurableTurnRuntime, "prepare_guidance_stop", prepare)
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke
    invocation = SimpleNamespace(
        assert_current=assert_current,
        cancellation=SimpleNamespace(),
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 1),
    )

    with pytest.raises(TimeoutError, match="expired"):
        invoke(factory.operation_id, {"turn_id": "turn"}, invocation)

    assert checks == 3
    observed = _invoke(tmp_path, "resource", operation="get", turn_id="turn")
    assert observed["guidance"][0]["status"] == "failed"


def test_stop_waits_for_exact_saved_registration_and_live_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop arriving before its saved turn targets that same later owner."""

    from ecosystem.rumi_turn_runtime_pack.runtime import host as host_module

    registered = False
    handle_active = False
    requests: list[str] = []
    fences: list[str] = []

    def sleep(_seconds: float) -> None:
        nonlocal handle_active, registered
        if not registered:
            _seed_running_saved_turn(tmp_path)
            registered = True
        elif not handle_active:
            handle_active = True

    def request(turn_id: str) -> Any:
        requests.append(turn_id)
        return SimpleNamespace(
            wait_for_verified_drain=lambda _deadline: False,
        )

    monkeypatch.setattr(host_module.time, "sleep", sleep)
    invocation = SimpleNamespace(
        cancellation=SimpleNamespace(
            active_for=lambda turn_id: handle_active and turn_id == "saved-turn",
            can_request=lambda turn_id: handle_active and turn_id == "saved-turn",
            request=request,
        ),
        assert_current=lambda: fences.append("checked"),
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 1),
    )
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke

    result = invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)

    assert result == {
        "status": "cancellation_requested",
        "turn_id": "saved-turn",
        "stopped": False,
    }
    assert requests == ["saved-turn"]
    assert fences == ["checked"] * 5
    observed = _invoke(tmp_path, "resource", operation="get", turn_id="saved-turn")
    assert observed["status"] == "running"
    assert [event["name"] for event in observed["events"]][-1] == (
        "turn.cancellation_requested"
    )


def test_stop_rejects_unknown_or_queued_turn_without_durable_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded registration miss never fabricates a cancellation intent."""

    from ecosystem.rumi_turn_runtime_pack.runtime import host as host_module

    monkeypatch.setattr(host_module, "_STOP_REGISTRATION_WAIT_SECONDS", 0.0)
    requests: list[str] = []
    invocation = SimpleNamespace(
        cancellation=SimpleNamespace(
            active_for=lambda _turn_id: False,
            can_request=lambda _turn_id: False,
            request=lambda turn_id: requests.append(turn_id),
        ),
        assert_current=lambda: None,
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 1),
    )
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke

    with pytest.raises(KeyError, match="turn is unknown"):
        invoke(factory.operation_id, {"turn_id": "missing-turn"}, invocation)

    queued = _invoke(tmp_path, "lifecycle", operation="begin_saved", **SAVED_INPUT)
    with pytest.raises(TurnConflict, match="not execution-ready"):
        invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)

    assert requests == []
    observed = _invoke(tmp_path, "resource", operation="get", turn_id="saved-turn")
    assert observed == queued
    assert observed["status"] == "queued"
    assert all(
        event["name"] != "turn.cancellation_requested"
        for event in observed["events"]
    )


def test_stop_registration_wait_rejects_stale_invocation_without_mutation(
    tmp_path: Path,
) -> None:
    """An expired capture cannot wait, resolve, or mutate a saved turn."""

    checks = 0

    def assert_current() -> None:
        nonlocal checks
        checks += 1
        raise TimeoutError("captured stop invocation expired")

    invocation = SimpleNamespace(
        cancellation=SimpleNamespace(
            active_for=lambda _turn_id: False,
            can_request=lambda _turn_id: False,
            request=lambda _turn_id: pytest.fail("request must not run"),
        ),
        assert_current=assert_current,
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 1),
    )
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke

    with pytest.raises(TimeoutError, match="expired"):
        invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)

    assert checks == 1
    assert not (
        tmp_path
        / "packs"
        / "rumi_turn_runtime_pack"
        / "profiles"
        / "defaults"
        / "turns.sqlite3"
    ).exists()


def test_stop_timeout_without_live_handle_records_one_unconfirmed_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered owner without a handle remains unconfirmed and idempotent."""

    from ecosystem.rumi_turn_runtime_pack.runtime import host as host_module

    _seed_running_saved_turn(tmp_path)
    monkeypatch.setattr(host_module, "_STOP_REGISTRATION_WAIT_SECONDS", 0.0)
    requests: list[str] = []

    def request(turn_id: str) -> Any:
        requests.append(turn_id)
        raise PermissionError("operation cancellation handle is unavailable")

    invocation = SimpleNamespace(
        cancellation=SimpleNamespace(
            active_for=lambda _turn_id: False,
            can_request=lambda _turn_id: False,
            request=request,
        ),
        assert_current=lambda: None,
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 1),
    )
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke

    first = invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)
    after_first = _invoke(
        tmp_path, "resource", operation="get", turn_id="saved-turn"
    )
    second = invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)
    after_second = _invoke(
        tmp_path, "resource", operation="get", turn_id="saved-turn"
    )

    assert first == second == {
        "status": "cancellation_requested",
        "turn_id": "saved-turn",
        "stopped": False,
    }
    assert requests == ["saved-turn", "saved-turn"]
    assert after_second == after_first
    assert after_second["status"] == "running"
    assert [
        event["name"] for event in after_second["events"]
    ].count("turn.cancellation_requested") == 1


def test_stop_rejects_foreign_live_owner_without_durable_intent(
    tmp_path: Path,
) -> None:
    """A live handle outside the exact captured scope cannot mutate the turn."""

    _seed_running_saved_turn(tmp_path)
    before = _invoke(tmp_path, "resource", operation="get", turn_id="saved-turn")
    invocation = SimpleNamespace(
        cancellation=SimpleNamespace(
            active_for=lambda turn_id: turn_id == "saved-turn",
            can_request=lambda _turn_id: False,
            request=lambda _turn_id: pytest.fail("foreign request must not run"),
        ),
        assert_current=lambda: None,
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 1),
    )
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke

    with pytest.raises(PermissionError, match="handle is unavailable"):
        invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)

    assert _invoke(
        tmp_path, "resource", operation="get", turn_id="saved-turn"
    ) == before


def test_stop_registration_wait_honours_envelope_deadline_and_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling stops at the envelope bound and rechecks capture freshness."""

    from ecosystem.rumi_turn_runtime_pack.runtime import host as host_module

    stale = False
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal stale
        sleeps.append(seconds)
        stale = True

    def assert_current() -> None:
        if stale:
            raise TimeoutError("captured stop invocation expired")

    monkeypatch.setattr(host_module.time, "sleep", sleep)
    invocation = SimpleNamespace(
        cancellation=SimpleNamespace(
            active_for=lambda _turn_id: False,
            can_request=lambda _turn_id: False,
            request=lambda _turn_id: pytest.fail("request must not run"),
        ),
        assert_current=assert_current,
        envelope=SimpleNamespace(deadline_monotonic=time.monotonic() + 0.01),
    )
    factory = TurnHostFactoryV4("stop")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke

    with pytest.raises(TimeoutError, match="expired"):
        invoke(factory.operation_id, {"turn_id": "saved-turn"}, invocation)

    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 0.01
    assert not (
        tmp_path
        / "packs"
        / "rumi_turn_runtime_pack"
        / "profiles"
        / "defaults"
        / "turns.sqlite3"
    ).exists()


def test_recaptured_actions_resources_events_share_real_store(tmp_path: Path) -> None:
    before = _invoke(tmp_path, "lifecycle", **BEGIN)
    running = _invoke(
        tmp_path,
        "lifecycle",
        operation="transition",
        turn_id="turn",
        expected_revision=1,
        status="running",
    )
    assert running["revision"] == 2
    assert _invoke(tmp_path, "lifecycle", **BEGIN) == running
    for kind in ("resource", "events"):
        read = {"operation": "get", "turn_id": "turn"}
        if kind == "events":
            read["conversation_id"] = "conversation"
        assert _invoke(tmp_path, kind, **read) == running
        assert _invoke(tmp_path, kind, operation="list", conversation_id="conversation") == {
            "turns": [running]
        }
        assert _invoke(tmp_path, kind, operation="list", conversation_id="other") == {"turns": []}
        with pytest.raises(PermissionError):
            _invoke(tmp_path, kind, **BEGIN)
    with pytest.raises(TurnConflict):
        _invoke(
            tmp_path,
            "lifecycle",
            operation="transition",
            turn_id="turn",
            expected_revision=before["revision"],
            status="cancelled",
        )


def test_reconcile_factory_cannot_create_or_execute_a_turn(tmp_path: Path) -> None:
    from core_runtime.global_contract_dispatch import GlobalContractClient
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import RECEIPT_CONTRACT
    from tests.test_saved_turn_coordinator import _Session, _run

    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    clients = []

    def client(**kwargs: Any) -> GlobalContractClient:
        clients.append(dict(kwargs))
        assert kwargs.pop("include_credentials") is False
        assert kwargs["allowed_contract_ids"] == frozenset({RECEIPT_CONTRACT})
        return GlobalContractClient(session=session, **kwargs)

    invocation = SimpleNamespace(contract_client=client, assert_current=lambda: None)
    factory = TurnHostFactoryV4("reconcile")
    invoke = factory.capture(_context(tmp_path, factory)).contributions[0].invoke
    for payload in ({"turn_id": "turn-1", "approved": True}, session.initial,
                    {"turn_id": "turn-1", "result_reference": {}},
                    {"turn_id": "turn-1", "profile_id": "other"}):
        with pytest.raises(PermissionError):
            invoke(factory.operation_id, payload, invocation)
    assert not clients
    with pytest.raises(KeyError):
        invoke(factory.operation_id, {"turn_id": "turn-1"}, invocation)
    assert not store.path.exists() and session.calls == 0
    session.transform = lambda _: (_ for _ in ()).throw(TimeoutError())
    assert _run(store, session)["status"] == "reconciliation_required"
    result = invoke(factory.operation_id, {"turn_id": "turn-1"}, invocation)
    assert result["status"] == "completed"
    assert result["turn"]["result_reference"]["conversation_revision"] == 3
    assert session.calls == 1


def test_saved_factory_uses_restricted_invocation_and_reuses_durable_result(
    tmp_path: Path,
) -> None:
    from core_runtime.global_contract_dispatch import GlobalContractClient
    from tests.test_saved_turn_coordinator import _Session
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import SAVED_CONTRACTS

    session = _Session(tmp_path)
    calls = []
    guards = []

    def client(**kwargs: Any) -> GlobalContractClient:
        calls.append(kwargs)
        assert kwargs.pop("include_credentials") is False
        return GlobalContractClient(session=session, **kwargs)

    invocation = SimpleNamespace(
        contract_client=client, assert_current=lambda: guards.append("checked"),
        cancellation=_cancellation(),
    )
    factory = TurnHostFactoryV4("saved")
    captured = factory.capture(_context(tmp_path, factory))
    invoke = captured.contributions[0].invoke
    result = invoke(factory.operation_id, {**session.initial, "_session_id": "host"}, invocation)
    assert result["status"] == "completed"
    assert calls == [{
        "allowed_contract_ids": SAVED_CONTRACTS,
        "consumer_pack_id": "rumi_turn_runtime_pack",
    }]
    # Receipt settlement is guarded after the captured model dispatch returns.
    assert len(guards) == 9
    assert invoke(factory.operation_id, session.initial, invocation) == {
        "status": "existing", "turn": result["turn"],
    }
    assert session.calls == 1


@pytest.mark.parametrize("extra", ["profile_id", "state", "outcome", "approved"])
def test_saved_factory_rejects_external_authority_and_resume_fields(
    tmp_path: Path, extra: str,
) -> None:
    from core_runtime.global_contract_dispatch import GlobalContractClient
    from tests.test_saved_turn_coordinator import _Session

    session = _Session(tmp_path)

    def client(**kwargs: Any) -> GlobalContractClient:
        kwargs.pop("include_credentials")
        return GlobalContractClient(session=session, **kwargs)

    factory = TurnHostFactoryV4("saved")
    contribution = factory.capture(_context(tmp_path, factory)).contributions[0]
    with pytest.raises(ValueError, match="initial fields"):
        contribution.invoke(factory.operation_id, {**session.initial, extra: "injected"},
                            SimpleNamespace(
                                contract_client=client,
                                assert_current=lambda: None,
                                cancellation=_cancellation(),
                            ))
    assert session.calls == 0
    assert not list(tmp_path.rglob("turns.sqlite3"))


def test_saved_factory_checks_invocation_before_claim(tmp_path: Path) -> None:
    from core_runtime.global_contract_dispatch import GlobalContractClient
    from tests.test_saved_turn_coordinator import _Session

    session = _Session(tmp_path)

    def client(**kwargs: Any) -> GlobalContractClient:
        kwargs.pop("include_credentials")
        return GlobalContractClient(session=session, **kwargs)

    def expired() -> None:
        raise PermissionError("expired capture")

    factory = TurnHostFactoryV4("saved")
    contribution = factory.capture(_context(tmp_path, factory)).contributions[0]
    with pytest.raises(PermissionError, match="expired capture"):
        contribution.invoke(factory.operation_id, session.initial,
                            SimpleNamespace(
                                contract_client=client,
                                assert_current=expired,
                                cancellation=_cancellation(),
                            ))
    assert session.calls == 0
    assert not list(tmp_path.rglob("turns.sqlite3"))


def test_captured_begin_retains_input_identity_without_restarting(tmp_path: Path) -> None:
    first_digest = canonical_digest({"request": {"content": "first"}})
    changed_digest = canonical_digest({"request": {"content": "changed"}})
    before = _invoke(tmp_path, "lifecycle", **BEGIN, input_digest=first_digest)
    assert before["input_digest"] == first_digest
    assert _invoke(tmp_path, "lifecycle", **BEGIN, input_digest=first_digest) == before
    with pytest.raises(TurnConflict, match="input identity"):
        _invoke(tmp_path, "lifecycle", **BEGIN, input_digest=changed_digest)
    assert _invoke(tmp_path, "resource", operation="get", turn_id="turn") == before
    assert before["status"] == "queued"


def test_event_owner_requires_matching_conversation_and_returns_same_snapshot(
    tmp_path: Path,
) -> None:
    before = _invoke(tmp_path, "lifecycle", **BEGIN)
    assert _invoke(
        tmp_path,
        "events",
        operation="get",
        turn_id="turn",
        conversation_id="conversation",
    ) == before
    with pytest.raises(PermissionError, match="does not belong"):
        _invoke(
            tmp_path,
            "events",
            operation="get",
            turn_id="turn",
            conversation_id="foreign",
        )
    with pytest.raises(PermissionError, match="fields"):
        _invoke(tmp_path, "events", operation="get", turn_id="turn")


def test_saved_begin_computes_identity_and_never_restarts_running_turn(tmp_path: Path) -> None:
    before = _invoke(tmp_path, "lifecycle", operation="begin_saved", **SAVED_INPUT)
    assert before["input_digest"] == canonical_digest(SAVED_INPUT)
    assert before["request_id"].startswith("saved-turn.")
    assert "original private text" not in json.dumps(before)
    running = _invoke(tmp_path, "lifecycle", operation="claim_saved", **SAVED_INPUT)["turn"]
    assert _invoke(tmp_path, "lifecycle", operation="begin_saved", **SAVED_INPUT) == running
    changed = copy.deepcopy(SAVED_INPUT)
    changed["request"]["content"] = "changed text with identical IDs"
    with pytest.raises(TurnConflict, match="input identity"):
        _invoke(tmp_path, "lifecycle", operation="begin_saved", **changed)
    assert _invoke(tmp_path, "resource", operation="get", turn_id="saved-turn") == running


def test_saved_claim_is_durable_across_host_recapture(tmp_path: Path) -> None:
    first = _invoke(tmp_path, "lifecycle", operation="claim_saved", **SAVED_INPUT)
    assert first["claimed"] is True
    assert first["turn"]["status"] == "running"
    assert "original private text" not in json.dumps(first)
    assert _invoke(tmp_path, "lifecycle", operation="claim_saved", **SAVED_INPUT) == {
        "claimed": False, "turn": first["turn"],
    }
    assert _invoke(tmp_path, "resource", operation="get", turn_id="saved-turn") == first["turn"]


@pytest.mark.parametrize("status", ["completed", "waiting", "failed", "cancelled"])
def test_management_cannot_forge_saved_execution_status(tmp_path: Path, status: str) -> None:
    """A management grant cannot impersonate a saved result or confirmed stop."""
    claimed = _invoke(tmp_path, "lifecycle", operation="claim_saved", **SAVED_INPUT)["turn"]
    database = next(tmp_path.rglob("turns.sqlite3"))
    before = database.read_bytes()
    with pytest.raises(PermissionError, match="coordinator-owned"):
        _invoke(
            tmp_path, "lifecycle", operation="transition", turn_id="saved-turn",
            expected_revision=claimed["revision"], status=status,
            details={"result_reference": {"assistant_message_id": "forged"}},
        )
    assert database.read_bytes() == before
    assert _invoke(tmp_path, "resource", operation="get", turn_id="saved-turn") == claimed
    with pytest.raises(PermissionError, match="fields"):
        _invoke(
            tmp_path, "lifecycle", operation="transition", turn_id="saved-turn",
            expected_revision=claimed["revision"], status="completed",
            reject_saved_transition=False,
        )
    assert database.read_bytes() == before


@pytest.mark.parametrize("operation", ["begin_saved", "claim_saved"])
@pytest.mark.parametrize("field", ["input_digest", "request_id", "state", "outcome", "target"])
def test_saved_begin_cannot_accept_caller_execution_identity(
    tmp_path: Path, field: str, operation: str,
) -> None:
    with pytest.raises(ValueError):
        _invoke(tmp_path, "lifecycle", operation=operation, **SAVED_INPUT,
                **{field: "caller-supplied"})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("operation", ["begin_saved", "claim_saved"])
@pytest.mark.parametrize("kind", ["resource", "events"])
def test_saved_begin_remains_denied_on_read_contracts(
    tmp_path: Path, kind: str, operation: str,
) -> None:
    with pytest.raises(PermissionError):
        _invoke(tmp_path, kind, operation=operation, **SAVED_INPUT)
    assert not list(tmp_path.iterdir())


def test_saved_begin_checks_utf8_byte_budget_before_creating_record(tmp_path: Path) -> None:
    payload = copy.deepcopy(SAVED_INPUT)
    payload["request"]["content"] = "あ" * 21000
    with pytest.raises(ValueError, match="byte limit"):
        _invoke(tmp_path, "lifecycle", operation="begin_saved", **payload)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "patch",
    [
        {"profile_id": "other"},
        {"approved": True},
        {"user_data_root": "/tmp/other"},
        {"operation": "execute"},
        {"conversation_revision": True},
        {"turn_id": None},
    ],
)
def test_invalid_begin_never_creates_owner_files(tmp_path: Path, patch: dict) -> None:
    with pytest.raises((PermissionError, ValueError)):
        _invoke(tmp_path, "lifecycle", **{**BEGIN, **patch})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "field", ["profile_id", "user_data_root", "domain_ids", "provider_bindings"]
)
def test_incomplete_capture_does_not_create_files(tmp_path: Path, field: str) -> None:
    factory = TurnHostFactoryV4("lifecycle")
    context = _context(tmp_path, factory)
    setattr(
        context,
        field,
        {} if field == "domain_ids" else () if field == "provider_bindings" else None,
    )
    with pytest.raises(PermissionError):
        factory.capture(context)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["contract_id", "operation_id", "contract_version"])
def test_wrong_binding_is_rejected(tmp_path: Path, field: str) -> None:
    factory = TurnHostFactoryV4("resource")
    context = _context(tmp_path, factory)
    setattr(context.provider_bindings[0].operation, field, "other")
    with pytest.raises(PermissionError):
        factory.capture(context)


@pytest.mark.parametrize(
    "patch",
    [
        {"expected_revision": True},
        {"details": []},
        {"approved": True},
        {"status": None},
    ],
)
def test_invalid_mutation_leaves_state_unchanged(tmp_path: Path, patch: dict) -> None:
    before = _invoke(tmp_path, "lifecycle", **BEGIN)
    with pytest.raises((PermissionError, ValueError)):
        _invoke(
            tmp_path,
            "lifecycle",
            **{
                "operation": "transition",
                "turn_id": "turn",
                "expected_revision": 1,
                "status": "running",
                **patch,
            },
        )
    assert _invoke(tmp_path, "resource", operation="get", turn_id="turn") == before


def test_filter_is_applied_before_limit(tmp_path: Path) -> None:
    _invoke(tmp_path, "lifecycle", **{**BEGIN, "turn_id": "z", "request_id": "z"})
    _invoke(tmp_path, "lifecycle", **{**BEGIN, "conversation_id": "other"})
    values = _invoke(
        tmp_path, "resource", operation="list", conversation_id="conversation", limit=1
    )
    assert [turn["id"] for turn in values["turns"]] == ["z"]


def test_offline_legacy_pin_requires_verified_replacement_identity() -> None:
    from scripts.migrate_manifest_authority import _load_v4, _matches_legacy_adapter

    root = Path(__file__).resolve().parents[1] / "ecosystem" / "rumi_turn_runtime_pack"
    manifest = json.loads((root / "rumi.pack.v3.json").read_text())
    entrypoint = manifest["entrypoints"][0]
    v4 = _load_v4(root)
    digest = entrypoint["artifact_hash"]
    assert _matches_legacy_adapter(root, entrypoint, v4, digest)
    assert not _matches_legacy_adapter(root, {**entrypoint, "symbol": "other"}, v4, digest)
    assert not _matches_legacy_adapter(root, entrypoint, v4, "sha256:" + "0" * 64)
    altered = copy.deepcopy(v4)
    altered["functions"] = {key: "sha256:" + "0" * 64 for key in v4["functions"]}
    assert not _matches_legacy_adapter(root, entrypoint, altered, digest)
