"""Real encrypted store CAS and strict mixed pending-effect recovery."""

from concurrent.futures import ThreadPoolExecutor
import pytest
from core_runtime.authority.v4 import AuthorityStore
from tests.test_interactive_approval_v4 import (
    _interactive_fixture,
    _prepare_execute_effect,
)
from tobkiri_host.interactive_effects import PendingEffectController, PendingEffectError
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.saved_tool_admission_store import (
    HostSavedToolAdmissionStore as AdmissionStore,
)
from tobkiri_host.typed_pending_effect_persistence import (
    TypedPendingEffectPersistence as EffectPersistence,
)


def binding():
    return {
        "capture_digest": "capture",
        "turn_id": "saved-turn",
        "tool_call_id": "tool-call",
        "owner_principal": "owner",
        "owner_session": "owner-session",
        "operation_digest": "exact-operation",
    }


def test_durable_reservation_reopen_and_concurrent_claims(tmp_path):
    first_store = AuthorityStore(tmp_path / "authority.sqlite3")
    second_store = AuthorityStore(tmp_path / "authority.sqlite3")
    first, second = (AdmissionStore(first_store), AdmissionStore(second_store))

    def reserve(store):
        try:
            return store.reserve(binding())
        except PermissionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (first, second)))
    assert sum((result is not None for result in results)) == 1
    reserved = next((result for result in results if result is not None))
    admitted = first.admit(
        reserved,
        {
            "retained_operation_digest": "read",
            "consent_request_id": "consent-request",
            "consent_request_digest": "consent-request-digest",
            "consent_target": "signed-target",
        },
    )

    def claim(store):
        try:
            store.claim(admitted, "read")
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, (first, second))) == 1
    reopened = AdmissionStore(AuthorityStore(tmp_path / "authority.sqlite3"))
    with pytest.raises(PermissionError, match="already claimed"):
        reopened.reserve(binding())
    with pytest.raises(PermissionError):
        reopened.reserve({**binding(), "operation_digest": "changed"})


def test_known_receipts_preserve_real_pending_effect_recovery(tmp_path):
    harness, authority, edge, backend, broker, original = _interactive_fixture(tmp_path)
    pending, context, scope, digest = _prepare_execute_effect(
        harness,
        edge,
        broker,
        original,
        request_id="real-pending-effect",
        message="real fixture",
    )
    admission = AdmissionStore(authority)
    admission.reserve(binding())
    for kind, prefix in (
        ("action_approval_policy_v1", "action-approval-policy-v1-"),
        ("action_approval_policy_selection_v1", "action-approval-policy-selection-v1-"),
    ):
        key = prefix + "a" * 64
        authority.create_host_pending_effect(
            key, {"effect_id": key, "record_kind": kind, "state": "native_approved"}
        )
    persistence = EffectPersistence(authority)
    assert len(persistence.list_host_pending_effects()) == 1
    recovered = PendingEffectController(
        persistence=persistence,
        approvals=authority,
        coordinator_principal=OpaqueAuthorityRef(edge.coordinator.principal_id),
        coordinator_publisher_lineage="publisher.coordinator",
        clock=harness.clock,
    ).recover()
    assert len(recovered) == 1
    assert recovered[0].effect_id == pending.effect_id
    assert backend.invocations == 0
    broker.close()


@pytest.mark.parametrize(
    "kind,identifier",
    [
        ("unrecognized_host_record", "saved-tool-admission-v1-" + "a" * 64),
        ("saved_tool_admission_v1", "pending-effect-malformed"),
        ("saved_tool_admission_v1", "saved-tool-admission-v1-invalid"),
    ],
)
def test_unknown_or_malformed_effect_rows_still_fail_strict_recovery(tmp_path, kind, identifier):
    harness, authority, edge, backend, broker, original = _interactive_fixture(tmp_path)
    authority.create_host_pending_effect(
        identifier, {"effect_id": identifier, "record_kind": kind, "state": "corrupt"}
    )
    persistence = EffectPersistence(authority)
    assert len(persistence.list_host_pending_effects()) == 1
    controller = PendingEffectController(
        persistence=persistence,
        approvals=authority,
        coordinator_principal=OpaqueAuthorityRef(edge.coordinator.principal_id),
        coordinator_publisher_lineage="publisher.coordinator",
        clock=harness.clock,
    )
    with pytest.raises(PendingEffectError):
        controller.recover()
    broker.close()
