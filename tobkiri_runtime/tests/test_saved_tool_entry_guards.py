"""Exact private guard lifetime and mutation tests."""

from types import SimpleNamespace
import pytest
import tobkiri_host.saved_tool_entry_guards as module


def test_exact_capture_repeats_proof_and_expires() -> None:
    registry = module.SavedToolEntryGuardRegistry()
    context = SimpleNamespace(request_id="owned", capture="real")
    envelope = SimpleNamespace(context=context, request_digest="exact")
    calls = []
    assert registry.capture(envelope) is None
    with registry.register(context, "exact", lambda: calls.append(True)):
        guard = registry.capture(envelope)
        guard()
        guard()
        assert len(calls) == 3
        with pytest.raises(PermissionError):
            with registry.register(context, "exact", lambda: None):
                pass
        envelope.request_digest = "changed"
        with pytest.raises(PermissionError):
            guard()
        with pytest.raises(PermissionError):
            registry.capture(envelope)
        envelope.request_digest = "exact"
    with pytest.raises(PermissionError):
        guard()
    assert registry.capture(envelope) is None


def test_revocation_guard_repeats_before_nested_effect() -> None:
    registry = module.SavedToolEntryGuardRegistry()
    context = SimpleNamespace(request_id="owned")
    current = [True]

    def proof() -> None:
        if not current[0]:
            raise PermissionError("revoked")

    with registry.register(context, "exact", proof):
        nested_parent_guard = registry.capture(
            SimpleNamespace(context=context, request_digest="exact")
        )
        current[0] = False
        with pytest.raises(PermissionError, match="revoked"):
            nested_parent_guard()
