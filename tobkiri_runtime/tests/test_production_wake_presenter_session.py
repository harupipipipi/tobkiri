"""Exercise native presenter journals and scheduled Calendar saved Turns.

The Calendar regression uses the real wake delivery and signed HTTP entrypoints,
with doubles for Launcher IPC and post-claim orchestration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityStore
from core_runtime.bootstrap import production_v4
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.panel_auth import PanelAuthBinding, PanelAuthManager
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.test_captured_wake_v4 import _Adapter
from tobkiri_protocol.canonical import canonical_digest


def test_actual_production_wake_context_registers_two_presenter_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the original producer closure, owner binding and real auth manager."""
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    observed: list[dict[str, Any]] = []
    original = production_v4.bind_production_wake_v4

    def bind(**arguments: Any) -> Any:
        # Retain the actual closure passed by production. The original binder,
        # recurring authority logic and durable registration are not replaced.
        driver = original(**arguments, adapter_factory=_Adapter)
        observed.append({**arguments, "driver": driver})
        return driver

    monkeypatch.setattr(production_v4, "bind_production_wake_v4", bind)
    store = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    session = production_v4.capture_production_dispatch(
        active,
        bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=store,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
    )
    try:
        assert len(observed) == 1
        captured = observed[0]
        scope = captured["context_scope"]
        declaration = captured["declaration"]
        owner = captured["owner_principal_id"]
        state_key = captured["state_path"].stem
        manager = PanelAuthManager(bootstrap_secret="unit-only-bootstrap")
        contexts: list[Any] = []
        journals: list[str] = []
        for number, occurrence in enumerate(("occurrence-one", "occurrence-two")):
            with scope(occurrence) as context:
                contexts.append(context)
                assert context.caller_principal.value == owner
                suffix = (
                    f".{owner.removeprefix('sha256:')[:24]}"
                    f".{active.activation['fencing_token']}"
                )
                assert context.caller_session_id.endswith(suffix)
                journal = context.caller_session_id[: -len(suffix)]
                journals.append(journal)
                expected_digest = canonical_digest(
                    {"registration": state_key, "occurrence": occurrence}
                ).removeprefix("sha256:")
                current = session.context_for(
                    declaration.contract_id, declaration.operation_id, journal
                )
                assert current.caller_principal == context.caller_principal
                assert current.caller_domain_id == context.caller_domain_id
                assert current.target_domain_id == context.target_domain_id
                assert current.caller_session_id == context.caller_session_id
                binding = PanelAuthBinding(
                    profile_id=context.profile_id,
                    profile_revision=context.profile_revision,
                    activation_id=context.activation_id,
                    plan_digest=context.plan_digest,
                    security_epoch=context.security_epoch,
                )
                request_id = f"wake-presenter-request-{number}"
                manager.record_approval_presenter_grant(request_id, journal, binding)
                code = manager.issue_login_code(
                    binding, presenter_request_id=request_id
                )
                exchanged = manager.exchange_code(
                    code["code"], binding, presenter_request_id=request_id
                )
                assert exchanged is not None
                verified = manager.verify_session(exchanged["session_id"], binding)
                assert verified is not None
                assert verified["session_id"] == journal
                assert verified["request_scope"] == request_id
                assert journal == "wake-" + expected_digest
            # The clock caller mapping is released by the original finally.
            # This route has no captured Shell edge to fall back to afterward.
            with pytest.raises(AuthorityDenied, match="captured Shell caller edge"):
                session.context_for(
                    declaration.contract_id, declaration.operation_id, journal
                )
        assert journals[0] != journals[1]
        assert contexts[0].caller_domain_id != contexts[1].caller_domain_id
        assert contexts[0].target_domain_id == contexts[1].target_domain_id
        assert contexts[0].request_id != contexts[1].request_id
        assert contexts[0].caller_principal == contexts[1].caller_principal
        assert captured["driver"]._registration.registration_id.startswith("wake.")
        assert captured["driver"]._adapter.leases == {}

        with pytest.raises(RuntimeError, match="unit body failure"):
            with scope("exception-cleanup") as context:
                journal = context.caller_session_id.rsplit(".", 2)[0]
                raise RuntimeError("unit body failure")
        with pytest.raises(AuthorityDenied, match="captured Shell caller edge"):
            session.context_for(
                declaration.contract_id, declaration.operation_id, journal
            )
    finally:
        session.close()


def test_original_dot_wake_journal_is_still_rejected() -> None:
    """Producer compatibility is fixed without relaxing PanelAuth validation."""
    manager = PanelAuthManager(bootstrap_secret="unit-only-bootstrap")
    binding = PanelAuthBinding("profile", "revision", "activation", "plan", 1)
    with pytest.raises(ValueError, match="approval presenter grant is invalid"):
        manager.record_approval_presenter_grant(
            "wake-presenter-request", "wake." + "a" * 64, binding
        )


@pytest.mark.parametrize("mode", ("automatic", "manual"))
def test_native_calendar_saved_turn_preserves_input_and_claim_on_lost_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Run automatic and manual signed Calendar dispatch through Turn begin and claim."""
    from contextlib import contextmanager
    from functools import partial
    import sqlite3
    import sys
    import threading
    import uuid

    from core_runtime import native_provider_prepare_ipc as ipc
    from core_runtime.authority.v4 import LeaseState
    from core_runtime.global_contract_dispatch import GlobalContractClient
    from core_runtime.provider_prepare_native_v4 import (
        NativeProviderRegistryRequiresLegacyRead,
    )
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from ecosystem.rumi_schedule_store_pack.runtime.store import ScheduleStore
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
    from ecosystem.rumi_turn_runtime_pack.runtime.scheduled_job import (
        ScheduledSourceLedger,
    )
    from tests.test_production_frontend_contract_http import (
        SAVED_TURN_HTTP_TIMEOUT_SECONDS,
        _SavedPackVmBackend,
        _authenticate,
        _captured_production_server,
        _contract,
        _publish_native_runtime_fixture,
        _request,
    )
    from tobkiri_host.backends import BackendRegistry

    data = tmp_path / "user-data"
    owner = DurableTurnRuntime("defaults", user_data_root=data)
    original_bind_wake = production_v4.bind_production_wake_v4
    original_load_factory = production_v4._load_verified_host_provider_factory
    original_bind_input = DurableTurnRuntime.bind_saved_input
    original_recover_input = DurableTurnRuntime.saved_input
    original_release = DurableTurnRuntime.release_calendar_preparation
    original_invoke = GlobalContractClient.invoke
    wakes = []
    observed = {}
    errors = []
    borrows = []
    legacy = []
    legacy_start = []
    starts = []
    lifecycle_calls = []
    releases = []
    continuation = []
    expired_callbacks = []
    expired_publications = []

    def bind_wake(**arguments: Any) -> Any:
        driver = original_bind_wake(**arguments, adapter_factory=_Adapter)
        wakes.append(driver)
        return driver

    def invoke(
        client: GlobalContractClient,
        contract: str,
        operation: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        if (contract, operation) != ("conversation.saved-turn.v1", "saved_complete"):
            return original_invoke(client, contract, operation, payload, **kwargs)
        assert client.consumer_pack_id == "rumi_turn_runtime_pack"
        accepted = payload["request"]
        turn_id = accepted["turn_id"]
        target = accepted["conversation_id"]
        assert owner.get(turn_id)["status"] == "running"
        continuation.append(target)
        return {
            "status": "error",
            "error": {
                "code": "AI_COMPLETION_UNAVAILABLE",
                "message": "Saved conversation did not complete.",
            },
            "turn_id": turn_id,
            "conversation_id": target,
            **{
                f"{role}_message_id": "message:" + canonical_digest(
                    [target, turn_id, role],
                ).removeprefix("sha256:")
                for role in ("user", "assistant")
            },
            "user_persistence": "not_written",
            "assistant_persistence": "not_written",
            "reconciliation_required": False,
        }

    def release(
        store: DurableTurnRuntime, **kwargs: Any,
    ) -> dict[str, Any]:
        releases.append(kwargs["turn_id"])
        return original_release(store, **kwargs)

    def begin_legacy(
        store: DurableTurnRuntime, payload: dict[str, Any],
    ) -> dict[str, Any]:
        legacy_start.append((payload["request"]["conversation_id"], "begin"))
        return DurableTurnRuntime.begin_saved(store, payload)

    def claim_legacy(
        store: DurableTurnRuntime, payload: dict[str, Any],
    ) -> dict[str, Any]:
        legacy_start.append((payload["request"]["conversation_id"], "claim"))
        return DurableTurnRuntime.claim_saved(store, payload)

    def bind_legacy(
        store: DurableTurnRuntime, source: dict[str, Any], accepted: dict[str, Any],
    ) -> dict[str, Any]:
        legacy.append(source["request"]["conversation_id"])
        return original_bind_input(store, source, accepted)

    def load_factory(
        pack_root: Path, function_id: str, bindings: tuple[Any, ...],
    ) -> tuple[Any, str]:
        factory, backend_id = original_load_factory(pack_root, function_id, bindings)
        if function_id == "rumi_turn_runtime_pack.chat-saved-job-adapter":
            store = sys.modules[type(factory).__module__].DurableTurnRuntime
            monkeypatch.setattr(store, "bind_saved_input", bind_legacy)
            monkeypatch.setattr(store, "release_calendar_preparation", release)
        if function_id == "rumi_turn_runtime_pack.turn-runtime.lifecycle":
            store = sys.modules[type(factory).__module__].DurableTurnRuntime
            monkeypatch.setattr(store, "begin_saved", begin_legacy)
            monkeypatch.setattr(store, "claim_saved", claim_legacy)
        return factory, backend_id

    def assert_borrow(
        request: ipc.NativeSavedInputRequest | ipc.NativeSavedTurnStartRequest,
        caller: str,
    ) -> tuple[Any, Any]:
        request.assert_current()
        child, adapter, broker, scheduler = (
            observed[name] for name in (
                "rumi_turn_runtime_pack.turn-lifecycle",
                "rumi_turn_runtime_pack.chat-saved-job-adapter",
                "rumi_job_action_broker_pack.job-action-broker",
                "rumi_scheduler_runtime_pack.scheduler-control",
            )
        )
        chain = (child, adapter, broker, scheduler)
        schedule_id = adapter.payload["schedule_id"]
        manual = schedule_id.startswith("manual-")
        assert request.caller == caller == ("calendar-manual" if manual else "calendar")
        if manual:
            calendar = observed["rumi_schedule_store_pack.calendar-manage"]
            chain = (*chain, calendar)
            for invocation in (scheduler, calendar):
                assert invocation.payload["operation"] == "trigger"
                assert invocation.payload["profile_id"] == "defaults"
                assert invocation.payload["schedule_id"] == schedule_id
        else:
            assert scheduler.payload == {"operation": "tick", "limit": 20}
        assert broker.payload["schedule_id"] == schedule_id
        key = f"{schedule_id}:{broker.payload['lease_id']}"
        assert broker.payload["operation"] == adapter.payload["operation"] == "dispatch"
        assert broker.payload["idempotency_key"] == key
        assert adapter.payload["idempotency_key"] == key
        assert adapter.idempotency_key == key
        assert request.context is child.context
        assert all(
            current.context.caller_principal == parent.target_principal
            for current, parent in zip(chain[:-1], chain[1:], strict=True)
        )
        expired_callbacks.append(request.assert_current)
        assert request.runtime_instance == server._native_provider_prepare_instance
        assert request.deadline_monotonic == child.deadline_monotonic
        deadline_order = [item.deadline_monotonic for item in chain]
        assert deadline_order == sorted(deadline_order)
        reservations = {item.resource_reservation_id for item in chain}
        assert len(reservations) == len(chain) and None not in reservations
        assert len({item.lease.token for item in chain}) == len(chain)
        leases = [
            authority.inspect_lease_token(item.lease.token.decode("ascii"))
            for item in chain
        ]
        assert all(state is LeaseState.DISPATCHED for _, state in leases)
        root_lease = leases[-1][0]
        assert root_lease.caller.principal_id == chain[-1].context.caller_principal.value
        expected_root = (
            "shell.tauri.default" if manual
            else "rumi_scheduler_runtime_pack.scheduler.clock"
        )
        assert root_lease.caller.function_id == expected_root
        return child, adapter

    def write(
        request: ipc.NativeSavedInputRequest, *, caller: str,
    ) -> dict[str, Any]:
        child, adapter = assert_borrow(request, caller)
        key = adapter.payload["idempotency_key"]
        lean = request.source
        turn_id = lean["request"]["turn_id"]
        destination = lean["request"]["conversation_id"]
        borrows.append((destination, child.payload["operation"]))
        assert turn_id == "calendar:" + canonical_digest(
            ["defaults", adapter.payload["schedule_id"], key]
        ).removeprefix("sha256:")
        if request.accepted is None:
            assert request.mark_publication_possible is None
            assert "request" not in child.payload
            return {"accepted": original_recover_input(owner, lean)}
        assert request.mark_publication_possible is not None
        assert canonical_digest(child.payload["request"]) == canonical_digest(
            request.accepted["request"],
        )
        pinned = ScheduledSourceLedger(data, "defaults").recover_source(key, turn_id)
        assert pinned == lean
        reservation = owner.recover_calendar_preparation(
            turn_id=turn_id, occurrence_key=key, source=adapter.payload["payload"],
        )
        assert reservation["status"] == "bound"
        assert reservation["payload"] == lean
        if destination in {"legacy", "claim-unconfirmed"}:
            # The unconfirmed claim is the only possible native write for its job.
            raise NativeProviderRegistryRequiresLegacyRead()
        if destination == "winner":
            # Simulate an independent capture winning before this bind's readback.
            winner = {"request": {**request.accepted["request"], "task_context": {
                "version": "tobkiri.saved-task-context.v1",
                "source_id": "winner-context",
                "conversation_id": destination,
                "recipient_id": f"conversation:{destination}",
                "input_id": turn_id,
                "source_revision": 1,
                "generation": 1,
                "items": [{"id": "note", "kind": "goal", "body": "Calendar winner"}],
            }}}
            return {"accepted": original_bind_input(owner, lean, winner)}
        request.mark_publication_possible()
        result = original_bind_input(owner, lean, request.accepted)
        if destination == "ambiguous":
            raise OSError("fixture lost the native reply after commit")
        return {"accepted": result}

    def write_start(
        request: ipc.NativeSavedTurnStartRequest, *, caller: str,
    ) -> dict[str, Any]:
        child, adapter = assert_borrow(request, caller)
        expired_publications.append(request.mark_publication_possible)
        target = request.conversation_id
        starts.append((target, request.action))
        assert child.payload["operation"] == f"{request.action}_saved"
        accepted = {"request": child.payload["request"]}
        assert request.turn_id == accepted["request"]["turn_id"]
        assert (
            request.conversation_revision == accepted["request"]["conversation_revision"]
        )
        key = adapter.payload["idempotency_key"]
        source = ScheduledSourceLedger(data, "defaults").recover_source(
            key, request.turn_id,
        )
        assert source is not None
        assert original_recover_input(owner, source) == accepted
        assert request.source_digest == canonical_digest(source)
        assert request.input_digest == canonical_digest(accepted)
        assert (request.source_digest != request.input_digest) == (target == "winner")
        with sqlite3.connect(owner.path) as connection:
            identities = connection.execute(
                "SELECT source_digest, accepted_digest FROM saved_inputs WHERE id=?",
                (request.turn_id,),
            ).fetchone()
        assert identities == (request.source_digest, request.input_digest)
        assert owner.get(request.turn_id)["status"] == "queued"
        if request.action == "begin":
            if target == "legacy":
                raise NativeProviderRegistryRequiresLegacyRead()
            # Calendar preparation already owns the accepted queued record.
            return {"claimed": False, "turn": owner.get(request.turn_id)}
        request.mark_publication_possible()
        if target == "claim-unconfirmed":
            raise OSError("fixture lost the claim reply before commit")
        result = owner.claim_saved(accepted)
        assert result["claimed"]
        if target == "claim-committed":
            raise OSError("fixture lost the claim reply after commit")
        return result

    _publish_native_runtime_fixture(monkeypatch)
    monkeypatch.setattr(GlobalContractClient, "invoke", invoke)
    monkeypatch.setattr(
        ipc, "capture_launcher_calendar_turn_start_writer",
        lambda: partial(write_start, caller="calendar"),
    )
    monkeypatch.setattr(
        ipc, "capture_launcher_manual_calendar_turn_start_writer",
        lambda: partial(write_start, caller="calendar-manual"),
    )
    monkeypatch.setattr(production_v4, "bind_production_wake_v4", bind_wake)
    monkeypatch.setattr(
        production_v4, "_load_verified_host_provider_factory", load_factory,
    )
    monkeypatch.setattr(
        ipc, "capture_launcher_saved_input_writer",
        lambda caller="saved-turn": (
            partial(write, caller=caller) if caller in {"calendar", "calendar-manual"}
            else None
        ),
    )
    with contextmanager(_captured_production_server)(
        tmp_path, monkeypatch, packvm_backends=BackendRegistry((_SavedPackVmBackend(),)),
    ) as (server, session, authority):
        original_dispatch = session.broker._dispatch

        def dispatch(backend: Any, envelope: Any, *args: Any, **kwargs: Any) -> Any:
            observed[envelope.operation_id] = envelope
            if envelope.operation_id == "rumi_turn_runtime_pack.turn-lifecycle":
                lifecycle_calls.append(envelope.payload["operation"])
            try:
                result = original_dispatch(backend, envelope, *args, **kwargs)
                if (
                    envelope.operation_id == "rumi_turn_runtime_pack.turn-lifecycle"
                    and envelope.payload["operation"] == "claim_saved"
                    and envelope.payload["request"]["conversation_id"] in {
                        "claim-broker-ack", "manual-claim-broker-ack",
                    }
                ):
                    raise OSError("fixture lost the committed nested Broker acknowledgement")
                return result
            except Exception as error:
                errors.append((envelope.operation_id, str(error.__cause__ or error)))
                raise

        monkeypatch.setattr(session.broker, "_dispatch", dispatch)
        assert len(wakes) == 1
        conversations = ConversationStore("defaults", user_data_root=data)
        schedules = ScheduleStore("defaults", root=data)
        manual = mode == "manual"
        if manual:
            cookie, csrf, origin = _authenticate(server)
            continued = ("manual-complete",)
            unknown_after_claim = ("manual-claim-broker-ack",)
            destinations = (*continued, *unknown_after_claim)
            skipped_start = ()
        else:
            wakes[0].arm(86400000)
            continued = ("complete", "legacy", "winner")
            unknown_after_claim = ("claim-committed", "claim-broker-ack")
            destinations = (
                *continued, "ambiguous", "claim-unconfirmed", *unknown_after_claim,
            )
            skipped_start = ("ambiguous",)
        for destination in destinations:
            conversations.create(
                {"id": destination, "model_reference": "model-profile-1"},
                expected_revision=conversations.snapshot()["revision"],
            )
            intent = {
                "profile_id": "defaults", "conversation_id": destination, "message": "Hi",
            }
            schedules.apply("create", {
                "schedule_id": destination,
                "name": destination,
                "action_id": "chat.saved",
                "payload": intent,
                "next_run_at_ms": schedules.clock() + 86400000 if manual else 1,
                "interval_ms": 0,
                "max_attempts": 1,
                "expected_revision": schedules.snapshot()["revision"],
            })
            previous_calls = len(lifecycle_calls)
            if manual:
                if destination in continued:
                    # The shipped client sends a concrete schedule path and empty body.
                    path = f"/api/agent/schedules/{destination}/trigger"
                    body = {}
                else:
                    path = "/api/agent/schedules/{schedule_id}/trigger"
                    body = {"schedule_id": destination}
                request_headers = {
                    "Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf,
                    "X-Tobkiri-Request-ID": str(uuid.uuid4()),
                }

                def trigger() -> tuple[int, dict[str, Any]]:
                    status, response, _ = _request(
                        server, "POST", _contract("POST", path),
                        body=body, headers=request_headers,
                        timeout_seconds=SAVED_TURN_HTTP_TIMEOUT_SECONDS,
                    )
                    return status, response

                status, response = trigger()
                assert status == 200 and response["success"], (response, errors)
                if destination in unknown_after_claim:
                    calls_before_retry = list(lifecycle_calls)
                    assert trigger() == (status, response)
                    assert lifecycle_calls == calls_before_retry
                facade = response["data"]
                assert facade["status"] == "ok", facade
                result = facade["data"]
                assert result["triggered"]["id"] == destination
                assert result["count"] == 1
            else:
                # Invoke the binding's original callback as a delivered wake.
                result = wakes[0]._invoke(destination, lambda: None, threading.Event())
            expected_calls = ["recover_saved_input", "bind_saved_input"]
            if destination not in skipped_start:
                expected_calls += ["begin_saved", "claim_saved"]
            assert lifecycle_calls[previous_calls:] == expected_calls, (result, errors)
            assert borrows[-2:] == [
                (destination, "recover_saved_input"),
                (destination, "bind_saved_input"),
            ], (result, errors)
            job = result["dispatched"][0]["result"]
            adapter = observed["rumi_turn_runtime_pack.chat-saved-job-adapter"]
            key = adapter.payload["idempotency_key"]
            turn_id = "calendar:" + canonical_digest(
                ["defaults", destination, key],
            ).removeprefix("sha256:")
            source = ScheduledSourceLedger(data, "defaults").recover_source(key, turn_id)
            assert source is not None
            accepted = original_recover_input(owner, source)
            assert accepted is not None
            assert ("task_context" in accepted["request"]) == (destination == "winner")
            turn = owner.get(turn_id)
            assert turn["input_digest"] == canonical_digest(accepted)
            claimed_events = [
                event for event in turn["events"] if event["name"] == "turn.running"
            ]
            assert job["status"] == "reconciliation_required", (job, errors)
            if destination in continued:
                assert turn["status"] == "failed", (job, errors, continuation)
                assert turn["events"][-1]["details"]["error_code"] == (
                    "AI_COMPLETION_UNAVAILABLE"
                )
                assert len(claimed_events) == 1
            else:
                possibly_claimed = destination in unknown_after_claim
                assert turn["status"] == ("running" if possibly_claimed else "queued")
                assert len(claimed_events) == int(possibly_claimed)
                assert owner.recover_calendar_preparation(
                    turn_id=turn_id, occurrence_key=key, source=intent,
                )["status"] == ("started" if possibly_claimed else "bound")
            assert conversations.get(destination)["messages"] == []

        assert legacy == ([] if manual else ["legacy", "claim-unconfirmed"])
        assert legacy_start == ([] if manual else [("legacy", "begin")])
        assert starts == [
            (target, action)
            for target in destinations if target not in skipped_start
            for action in ("begin", "claim")
        ]
        assert releases == []
        assert continuation == list(continued)
        for guard in (*expired_callbacks, *expired_publications):
            with pytest.raises(PermissionError):
                guard()
