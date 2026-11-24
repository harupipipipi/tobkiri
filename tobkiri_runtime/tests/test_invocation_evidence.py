"""Generic evidence transport is exact, nondelegating and revocable."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.invocation_evidence import (
    EvidenceBinding, EvidenceDenied, InvocationEvidenceRegistry,
)


def _binding() -> EvidenceBinding:
    return EvidenceBinding(
        "issuer", "target", "contract.v1", "1.0.0", "operation", "sha256:" + "1" * 64,
        "one-attempt", "profile", "sha256:" + "2" * 64,
        "activation", "sha256:" + "3" * 64, 1, "owner", "session",
    )


def _registry():
    registry = InvocationEvidenceRegistry()
    data = {"values": ["committed"]}
    calls = []
    def resolve(reference, binding):
        calls.append((reference, binding))
        return SimpleNamespace(read=lambda selector: data)
    registry.register("issuer", "evidence.v1", resolve)
    return registry, data, calls


def _receive(registry, binding, receiver):
    return registry.receive(
        kind="evidence.v1", binding=binding, receiver=receiver, guard=lambda: None,
    )


def test_evidence_crosses_worker_context_and_is_revoked_after_return():
    registry, data, calls = _registry()
    binding, receiver = _binding(), object()
    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=lambda: None):
        with ThreadPoolExecutor(max_workers=1) as executor:
            context = copy_context()
            view = executor.submit(context.run, _receive, registry, binding, receiver).result()
        assert view.issuer_principal == "issuer"
        value = view.read("bound-input")
        value["values"].append("caller mutation")
        assert data == {"values": ["committed"]}
        assert _receive(registry, binding, receiver).read("bound-input") == data
    assert calls == [("attempt", binding)]
    with pytest.raises(EvidenceDenied):
        view.read("bound-input")


@pytest.mark.parametrize("field,value", [
    ("issuer_principal", "descendant"), ("target_principal", "other"),
    ("contract_id", "other.v1"), ("contract_version", "2.0.0"),
    ("operation_id", "other"),
    ("payload_digest", "changed"), ("idempotency_key", "different-attempt"),
    ("profile_id", "other"), ("plan_digest", "different"),
    ("activation_id", "other"), ("activation_digest", "changed"),
    ("security_epoch", 2), ("presentation_owner_principal", "other"),
    ("presentation_owner_session", "other"),
])
def test_every_dispatch_dimension_is_bound(field, value):
    registry, _, _ = _registry()
    binding = _binding()
    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=lambda: None):
        with pytest.raises(EvidenceDenied):
            _receive(registry, replace(binding, **{field: value}), object())


def test_equal_fields_do_not_authorize_second_envelope_or_other_capture():
    registry, _, _ = _registry()
    other, _, _ = _registry()
    binding = _binding()
    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=lambda: None):
        _receive(registry, binding, object())
        with pytest.raises(EvidenceDenied):
            _receive(registry, binding, object())
        with pytest.raises(EvidenceDenied):
            _receive(other, binding, object())


def test_copied_context_cannot_bind_after_scope_exits():
    registry, _, _ = _registry()
    binding = _binding()
    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=lambda: None):
        context = copy_context()
    with pytest.raises(EvidenceDenied):
        context.run(_receive, registry, binding, object())


def test_registry_close_revokes_inflight_view():
    registry, _, _ = _registry()
    binding = _binding()
    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=lambda: None):
        view = _receive(registry, binding, object())
        registry.close()
        with pytest.raises(EvidenceDenied):
            view.read("bound-input")
    with pytest.raises(EvidenceDenied):
        registry.register("issuer", "new", lambda *_: None)


def test_issuer_guard_is_rechecked_for_every_read():
    registry, _, _ = _registry()
    binding = _binding()
    active = True
    def guard():
        if not active:
            raise EvidenceDenied("issuer cancelled")
    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=guard):
        view = _receive(registry, binding, object())
        active = False
        with pytest.raises(EvidenceDenied, match="issuer cancelled"):
            view.read("bound-input")


def test_duplicate_issuer_and_unregistered_kind_fail_closed():
    registry, _, _ = _registry()
    with pytest.raises(EvidenceDenied):
        registry.register("issuer", "evidence.v1", lambda *_: None)
    with pytest.raises(EvidenceDenied):
        with registry.dispatch(kind="unknown", reference="attempt", binding=_binding(), guard=lambda: None):
            pytest.fail("unknown issuer dispatched")


def test_snapshot_revocation_cannot_return_evidence():
    from collections.abc import Mapping
    registry = InvocationEvidenceRegistry()
    class ClosingMapping(Mapping):
        def __iter__(self):
            registry.close()
            return iter(["value"])
        def __len__(self):
            return 1
        def __getitem__(self, key):
            return "committed"
    registry.register("issuer", "evidence.v1", lambda *_: SimpleNamespace(read=lambda _: ClosingMapping()))
    binding = _binding()
    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=lambda: None):
        view = _receive(registry, binding, object())
        with pytest.raises(EvidenceDenied):
            view.read("bound-input")


def test_missing_receiver_does_not_claim_scope():
    registry, _, _ = _registry()
    binding = _binding()
    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=lambda: None):
        with pytest.raises(EvidenceDenied, match="envelope is missing"):
            _receive(registry, binding, None)
        assert _receive(registry, binding, object()).read("bound-input")


def test_batch_registration_is_atomic():
    registry, _, _ = _registry()
    with pytest.raises(EvidenceDenied):
        registry.register_many((
            ("issuer", "new.kind", lambda *_: None),
            ("issuer", "evidence.v1", lambda *_: None),
        ))
    with pytest.raises(EvidenceDenied, match="unavailable"):
        with registry.dispatch(kind="new.kind", reference="attempt", binding=_binding(), guard=lambda: None):
            pytest.fail("partially registered declaration escaped")


def test_closed_view_rejects_before_reentering_expired_issuer():
    registry, _, _ = _registry()
    binding = _binding()
    expired = False
    calls = []

    def guard():
        calls.append("guard")
        if expired:
            raise PermissionError("expired issuer lease")

    with registry.dispatch(kind="evidence.v1", reference="attempt", binding=binding, guard=guard):
        view = _receive(registry, binding, object())
    expired = True
    before = len(calls)
    with pytest.raises(EvidenceDenied, match="no longer live"):
        view.read("bound-input")
    assert len(calls) == before
