"""Authenticated budget inventory stays outside both strict effect consumers."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from core_runtime.authority.v4 import AuthorityStoreError
from core_runtime.authority.v4_models import authority_digest
from core_runtime.bootstrap.production_v4 import _recover_interactive_effect_controller
from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.generation_transport_budget import GenerationBudgetPolicy
from tests.test_interactive_effects import _Approvals, _controller, _prepare
from tests import test_native_generation_budget as native
from tests.test_tobkiri_host_execution_integration import (
    context,
    frame,
    make_broker,
)
from tobkiri_host.generation_budget_persistence import is_generation_budget_record
from tobkiri_host.interactive_effects import PendingEffectError, PendingEffectStatus
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.typed_pending_effect_persistence import TypedPendingEffectPersistence


@pytest.fixture
def native_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Reuse the real approved Native fixture without importing a fixture symbol."""
    return native.setup.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def mixed(native_setup: SimpleNamespace) -> Iterator[SimpleNamespace]:
    """Create all four rows through the real, approved Native owner in temp state."""
    native_setup.contribution.invoke(
        "rumi_credential_broker_pack.credential-manage",
        native_setup.credential_payload,
        native_setup.invocation,
    )
    store = native_setup.harness.store
    budget_rows = store.list_host_pending_effects()
    assert len(budget_rows) == 4
    persistence = TypedPendingEffectPersistence(store)
    approvals = _Approvals()
    broker = make_broker()
    controller = _controller(persistence, approvals)
    correlation = str(uuid.uuid4())
    pending, _ = _prepare(controller, broker.broker, correlation)
    yield SimpleNamespace(
        setup=native_setup,
        store=store,
        persistence=persistence,
        approvals=approvals,
        controller=controller,
        pending=pending,
        budget_rows=budget_rows,
        correlation=correlation,
    )
    broker.broker.close()


def lookup(mixed: SimpleNamespace) -> PendingEffectStatus:
    """Use the existing owner-scoped reply-loss lookup, without execution."""
    return mixed.controller.find_for_presentation(
        correlation_id=mixed.correlation,
        presentation_owner_principal_id="authority:presenter",
        presentation_owner_session_id="presenter-session",
        context=context(),
        contract_id=frame().contract_id,
        operation_id=frame().operation_id,
        target_principal=OpaqueAuthorityRef("authority:notification-send"),
    )


def test_real_native_budget_rows_preserve_recovery_and_reply_loss_lookup(
    mixed: SimpleNamespace,
) -> None:
    """Both consumers see the genuine pending effect and leave budget rows untouched."""
    assert all(is_generation_budget_record(*row) for row in mixed.budget_rows)
    assert len(mixed.persistence.list_host_pending_effects()) == 1
    assert lookup(mixed).effect_id == mixed.pending.effect_id
    _recover_interactive_effect_controller(mixed.controller)
    recovered = mixed.controller.recover()
    assert [value.effect_id for value in recovered] == [mixed.pending.effect_id]
    assert lookup(mixed).effect_id == mixed.pending.effect_id
    for revision, payload in mixed.budget_rows:
        assert mixed.store.get_host_pending_effect(payload["effect_id"]) == (
            revision,
            payload,
        )
    # Filtering is inventory-only: direct authenticated access and CAS stay bound.
    ledger = next(row for row in mixed.budget_rows if row[1].get("attempts") == [])
    assert mixed.persistence.get_host_pending_effect(ledger[1]["effect_id"]) == ledger


@pytest.mark.parametrize("count", [1, 2, 3])
def test_legitimate_consumed_and_exhausted_ledgers_stay_outside_effect_recovery(
    mixed: SimpleNamespace,
    count: int,
) -> None:
    """Real ledger CAS does not turn lifetime state into an interactive record."""
    revision, original = next(
        row for row in mixed.budget_rows if row[1].get("attempts") == []
    )
    current = deepcopy(original)
    for index in range(count):
        current["attempts"].append(authority_digest({"synthetic_attempt": index}))
        current["state"] = "exhausted" if index == 2 else "active"
        revision = mixed.store.compare_and_swap_host_pending_effect(
            current["effect_id"],
            expected_revision=revision,
            payload=current,
        )
    before = mixed.store.get_host_pending_effect(current["effect_id"])
    assert is_generation_budget_record(*before)
    assert lookup(mixed).effect_id == mixed.pending.effect_id
    mixed.controller.recover()
    assert mixed.store.get_host_pending_effect(current["effect_id"]) == before


@pytest.mark.parametrize(
    "change",
    [
        "unknown_kind",
        "wrong_namespace",
        "wrong_state",
        "extra_interactive_field",
        "template_mismatch",
        "invalid_template_limit",
        "bad_revision",
    ],
)
def test_malformed_budget_templates_remain_visible_and_fail_both_consumers(
    mixed: SimpleNamespace,
    change: str,
) -> None:
    """A recognized prefix alone never hides malformed or disguised state."""
    _, original = next(
        row
        for row in mixed.budget_rows
        if row[1].get("kind") == "native_generation_budget_template_v1"
    )
    payload = deepcopy(original)
    if change == "unknown_kind":
        payload["kind"] = "unknown_host_state"
    elif change == "wrong_namespace":
        payload["effect_id"] = "pending-effect-malformed"
    elif change == "wrong_state":
        payload["state"] = "corrupt"
    elif change == "extra_interactive_field":
        payload["prepared"] = {"unexpected": "interactive snapshot"}
    elif change == "template_mismatch":
        payload["template"]["profile_id"] = "other-profile"
    elif change == "invalid_template_limit":
        payload["template"]["max_requests"] = 4
    assert not is_generation_budget_record(
        2 if change == "bad_revision" else 1, payload
    )
    if payload["effect_id"] == original["effect_id"]:
        mixed.store.compare_and_swap_host_pending_effect(
            payload["effect_id"],
            expected_revision=1,
            payload=payload,
        )
    else:
        mixed.store.create_host_pending_effect(payload["effect_id"], payload)
    assert len(mixed.persistence.list_host_pending_effects()) == 2
    with pytest.raises(PendingEffectError):
        lookup(mixed)
    with pytest.raises(
        AuthorityDenied, match="interactive effect recovery is unavailable"
    ):
        _recover_interactive_effect_controller(mixed.controller)


@pytest.mark.parametrize("row_kind", ["draft", "pointer", "ledger"])
def test_malformed_other_budget_rows_and_unknown_interactive_rows_fail_closed(
    mixed: SimpleNamespace,
    row_kind: str,
) -> None:
    """Every budget shape and an untyped corrupt interactive row remain strict."""
    _, original = next(
        row
        for row in mixed.budget_rows
        if (row_kind == "draft" and "plan" in row[1])
        or (row_kind == "pointer" and row[1]["state"] == "enrolled")
        or (row_kind == "ledger" and "attempts" in row[1])
    )
    payload = {**deepcopy(original), "context": {"disguised": True}}
    assert not is_generation_budget_record(1, payload)
    mixed.store.compare_and_swap_host_pending_effect(
        payload["effect_id"],
        expected_revision=1,
        payload=payload,
    )
    with pytest.raises(PendingEffectError):
        lookup(mixed)
    with pytest.raises(AuthorityDenied):
        _recover_interactive_effect_controller(mixed.controller)


def test_unknown_corrupt_interactive_record_is_never_hidden(
    mixed: SimpleNamespace,
) -> None:
    payload = {
        "effect_id": "pending-effect-unknown",
        "state": "corrupt",
        "kind": "unrecognized_host_record",
    }
    mixed.store.create_host_pending_effect(payload["effect_id"], payload)
    with pytest.raises(PendingEffectError):
        lookup(mixed)
    with pytest.raises(AuthorityDenied):
        _recover_interactive_effect_controller(mixed.controller)


@pytest.mark.parametrize("damage", ["ciphertext", "digest"])
def test_budget_inventory_authentication_happens_before_owner_filtering(
    mixed: SimpleNamespace,
    damage: str,
) -> None:
    """Even an otherwise recognized budget row cannot bypass store authentication."""
    identifier = mixed.setup.owner.template.effect_id
    with sqlite3.connect(mixed.store.path) as connection:
        if damage == "ciphertext":
            connection.execute(
                "UPDATE host_pending_effects SET encrypted_payload=? WHERE effect_id=?",
                (b"not-authentic-ciphertext", identifier),
            )
        else:
            connection.execute(
                "UPDATE host_pending_effects SET payload_digest=? WHERE effect_id=?",
                ("sha256:" + "0" * 64, identifier),
            )
    with pytest.raises(AuthorityStoreError):
        mixed.persistence.list_host_pending_effects()
    with pytest.raises(PendingEffectError):
        lookup(mixed)
    with pytest.raises(AuthorityDenied):
        _recover_interactive_effect_controller(mixed.controller)


def test_all_policy_fields_remain_bound_to_namespace_not_test_identifiers() -> None:
    """Generic approved enrollment IDs are not restricted to this test's UUIDs."""
    policy = GenerationBudgetPolicy(
        enrollment_id="approved-general-enrollment",
        profile_id="other-profile",
        consumer_pack_id="rumi_provider_adapters_pack",
        connection_id="connection/general",
        credential_handle="credential:opaque-general",
        credential_key_version="key:general",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        model="model:free",
        max_requests=2,
        max_output_tokens=512,
    )
    payload = {
        "effect_id": policy.effect_id,
        "state": "active",
        "kind": "credential_generation_budget_v1",
        "policy": asdict(policy),
        "policy_digest": policy.digest,
        "approval_request_id": "original-native-approval",
        "approval_digest": authority_digest({"approved": True}),
        "attempts": [],
    }
    assert is_generation_budget_record(1, payload)
    assert not is_generation_budget_record(
        1, {**payload, "effect_id": "pending-effect-a"}
    )
    assert not is_generation_budget_record(
        1, {**payload, "policy_digest": "sha256:bad"}
    )
    assert not is_generation_budget_record(1, {**payload, "attempts": ["malformed"]})
