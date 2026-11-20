"""Exercise native presenter journals and automatic Calendar Saved Input.

Tests use a passive wake adapter. The Calendar regression invokes the real
captured wake delivery callback with doubles for Launcher IPC and Turn execution.
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


def test_native_calendar_saved_input_keeps_preadmitted_source_on_lost_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify signed automatic wake through input ownership with IPC and turn doubles."""
    from contextlib import contextmanager
    import importlib
    import sys
    import threading

    from core_runtime import native_provider_prepare_ipc as ipc
    from core_runtime.authority.v4 import LeaseState
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
        _SavedPackVmBackend,
        _captured_production_server,
        _publish_native_runtime_fixture,
    )
    from tobkiri_host.backends import BackendRegistry

    data = tmp_path / "user-data"
    owner = DurableTurnRuntime("defaults", user_data_root=data)
    original_bind_wake = production_v4.bind_production_wake_v4
    original_load_factory = production_v4._load_verified_host_provider_factory
    original_bind_input = DurableTurnRuntime.bind_saved_input
    original_recover_input = DurableTurnRuntime.saved_input
    wakes = []
    observed = {}
    errors = []
    borrows = []
    legacy = []
    saved = []
    expired_callbacks = []

    def bind_wake(**arguments: Any) -> Any:
        driver = original_bind_wake(**arguments, adapter_factory=_Adapter)
        wakes.append(driver)
        return driver

    def execute_saved(
        store: DurableTurnRuntime, accepted: dict[str, Any], **kwargs: Any,
    ) -> dict[str, Any]:
        kwargs["guard"]()
        saved.append(accepted)
        return {"status": "completed", "turn_id": accepted["request"]["turn_id"]}

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
            adapter_module = sys.modules[type(factory).__module__]
            captured_package, _, _ = adapter_module.__name__.rpartition(".")
            saved_module = importlib.import_module(f"{captured_package}.scheduled_saved")
            monkeypatch.setattr(saved_module, "execute_saved_turn", execute_saved)
            monkeypatch.setattr(
                adapter_module.DurableTurnRuntime, "bind_saved_input", bind_legacy,
            )
        return factory, backend_id

    def write(request: ipc.NativeSavedInputRequest) -> dict[str, Any]:
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
        assert request.caller == "calendar"
        assert request.context is child.context
        assert all(
            current.context.caller_principal == parent.target_principal
            for current, parent in zip(chain[:-1], chain[1:], strict=True)
        )
        expired_callbacks.append(request.assert_current)
        assert request.deadline_monotonic <= scheduler.deadline_monotonic
        assert len({item.resource_reservation_id for item in chain}) == 4
        assert None not in {item.resource_reservation_id for item in chain}
        assert len({item.lease.token for item in chain}) == 4
        assert all(
            authority.inspect_lease_token(item.lease.token.decode("ascii"))[1]
            is LeaseState.DISPATCHED for item in chain
        )
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
        if destination == "legacy":
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

    _publish_native_runtime_fixture(monkeypatch)
    monkeypatch.setattr(production_v4, "bind_production_wake_v4", bind_wake)
    monkeypatch.setattr(
        production_v4, "_load_verified_host_provider_factory", load_factory,
    )
    monkeypatch.setattr(
        ipc, "capture_launcher_saved_input_writer",
        lambda caller="saved-turn": write if caller == "calendar" else None,
    )
    with contextmanager(_captured_production_server)(
        tmp_path, monkeypatch, packvm_backends=BackendRegistry((_SavedPackVmBackend(),)),
    ) as (_server, session, authority):
        original_dispatch = session.broker._dispatch

        def dispatch(backend: Any, envelope: Any, *args: Any, **kwargs: Any) -> Any:
            observed[envelope.operation_id] = envelope
            try:
                return original_dispatch(backend, envelope, *args, **kwargs)
            except Exception as error:
                errors.append((envelope.operation_id, str(error.__cause__ or error)))
                raise

        monkeypatch.setattr(session.broker, "_dispatch", dispatch)
        assert len(wakes) == 1
        wakes[0].arm(86400000)
        conversations = ConversationStore("defaults", user_data_root=data)
        schedules = ScheduleStore("defaults", root=data)
        for destination in ("complete", "legacy", "winner", "ambiguous"):
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
                "next_run_at_ms": 1,
                "interval_ms": 0,
                "max_attempts": 1,
                "expected_revision": schedules.snapshot()["revision"],
            })
            # Invoke the binding's original callback as a delivered wake.
            result = wakes[0]._invoke(destination, lambda: None, threading.Event())
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
            if destination != "ambiguous":
                assert job["status"] == "completed", job
                assert saved[-1]["request"]["conversation_id"] == destination
                assert original_recover_input(owner, source) == saved[-1]
                assert ("task_context" in saved[-1]["request"]) == (destination == "winner")
                continue
            assert job["status"] == "reconciliation_required", job
            assert original_recover_input(owner, source) == source
            assert owner.get(turn_id)["status"] == "queued"
            assert owner.recover_calendar_preparation(
                turn_id=turn_id, occurrence_key=key, source=intent,
            )["status"] == "bound"
            assert conversations.get(destination)["messages"] == []
        assert legacy == ["legacy"]
        assert [value["request"]["conversation_id"] for value in saved] == [
            "complete", "legacy", "winner",
        ]
        for guard in expired_callbacks:
            with pytest.raises(PermissionError):
                guard()
