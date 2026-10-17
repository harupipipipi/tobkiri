"""Actual captured wake sessions satisfy the native presenter journal contract.

These are production-composition units with a passive wake adapter. They do
not deliver a wake, invoke an AI, or approve a native effect.
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
