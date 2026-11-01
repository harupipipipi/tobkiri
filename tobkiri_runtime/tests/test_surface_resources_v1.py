"""Public resource tickets cannot carry or transfer ambient Host authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
import json
from threading import Event
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.surface_resources import (
    CapturedSurfaceResourceProvider,
    PrivateSurfaceSelection,
    SurfaceResourceActionAdapter,
    SurfaceResourcePort,
    SurfaceResourceProviderError,
    SurfaceResourceRejected,
    SurfaceResourceScope,
    SurfaceResourceUnavailable,
)


def digest(character: str = "a") -> str:
    """Return deterministic valid capture digests."""
    return "sha256:" + character * 64


def scope() -> SurfaceResourceScope:
    """Build server-derived identity, independent of public resource input."""
    return SurfaceResourceScope(
        profile_id="profile.one",
        profile_revision=digest("b"),
        activation_id="activation.one",
        plan_hash=digest("c"),
        catalog_hash=digest("d"),
        security_epoch=7,
        presentation_owner_principal_id="principal.owner",
        presentation_owner_session_id="session.owner",
        view_descriptor_hash=digest("e"),
        renderer_id="renderer.one",
        renderer_api_version="1.0.0",
        renderer_digest=digest("f"),
        renderer_descriptor_hash=digest("e"),
        renderer_expires_at_ms=1_700_000_400_000,
        consumer_contract_id="example.inspect.v1",
        consumer_contract_version="1.0.0",
        consumer_operation_id="inspect",
        consumer_principal_id="principal.inspect",
        consumer_provider_id="provider.inspect",
        consumer_function_id="function.inspect",
        consumer_artifact_digest=digest(),
        consumer_schema_digest=digest("b"),
    )


def context(request_id: str = "request.acquire") -> RequestContext:
    """Create fresh Broker-authenticated context for each of the three RPCs."""
    return RequestContext(
        request_id=request_id,
        trace_id="trace.one",
        caller_principal=OpaqueAuthorityRef("principal.authenticated"),
        profile_id="profile.one",
        profile_revision=digest("b"),
        activation_id="activation.one",
        activation_digest=digest(),
        plan_digest=digest("c"),
        security_epoch=7,
        caller_session_id="session.one",
        caller_domain_id="domain.caller",
        caller_boot_epoch=1,
        target_domain_id="domain.target",
        target_boot_epoch=2,
        target_backend_digest=digest(),
        profile_authority_digest=digest(),
        fencing_token=1,
        handle_namespace="namespace.one",
    )


@dataclass
class Clock:
    """Control monotonic expiry independently of public wall-clock timestamps."""

    now: float = 10

    def monotonic(self) -> float:
        """Return the fake monotonic clock."""
        return self.now

    def wall(self) -> float:
        """Return a stable positive epoch clock."""
        return 1_700_000_000 + self.now


class Provider:
    """Recording trusted adapter whose private values must never enter JSON."""

    def __init__(self) -> None:
        self.acquired: list[object] = []
        self.exchanged: list[object] = []
        self.consumed: list[object] = []
        self.revoked: list[object] = []
        self.requests: list[str] = []

    def acquire(self, kind: str, request: RequestContext) -> PrivateSurfaceSelection:
        """Select a private resource without accepting a client path."""
        reference = {
            "private_path": "/private/selected",
            "kind": kind,
            "request_id": request.request_id,
        }
        self.acquired.append(reference)
        return PrivateSurfaceSelection(reference, "Selected image")

    def exchange(
        self,
        reference: object,
        request: RequestContext,
    ) -> PrivateSurfaceSelection:
        """Create a portable private selection for a later consumer RPC."""
        self.requests.append(request.request_id)
        result = {"private_selection": reference, "portable": True}
        self.exchanged.append(result)
        return PrivateSurfaceSelection(result, "Ready image")

    def consume(self, reference: object, request: RequestContext) -> object:
        """Rebind privately to the fresh consuming Logic invocation."""
        self.requests.append(request.request_id)
        result = {"private_handle": reference, "request_id": request.request_id}
        self.consumed.append(result)
        return result

    def revoke(self, reference: object) -> None:
        """Record idempotent cleanup by identity."""
        if not any(item is reference for item in self.revoked):
            self.revoked.append(reference)

    def adapter(self, **changes: Any) -> CapturedSurfaceResourceProvider:
        """Expose only the explicit capture callbacks."""
        return replace(
            CapturedSurfaceResourceProvider(
                acquire=self.acquire,
                exchange=self.exchange,
                revoke=self.revoke,
                consume=self.consume,
            ),
            **changes,
        )


@pytest.fixture
def rig() -> Iterator[tuple[SurfaceResourcePort, Provider, Clock]]:
    """Capture an isolated provider and deterministic clock."""
    provider, clock = Provider(), Clock()
    port = SurfaceResourcePort(
        provider.adapter(), clock=clock.monotonic, wall_clock=clock.wall
    )
    yield port, provider, clock
    port.close()


def invoke(port: SurfaceResourcePort, **changes: Any) -> dict[str, object]:
    """Invoke with server capture arguments that public callers cannot choose."""
    arguments = {
        "scope": scope(),
        "context": context(),
        "assert_current": lambda: None,
        "deadline_monotonic": 100.0,
    }
    request = changes.pop("request", {"operation": "acquire", "kind": "image"})
    arguments.update(changes)
    return port.invoke(request, **arguments)


def exchange(
    port: SurfaceResourcePort,
    selection_id: object,
    **changes: Any,
) -> dict[str, object]:
    """Run exchange in a different authenticated RPC."""
    return invoke(
        port,
        request={
            "operation": "exchange",
            "selection_id": selection_id,
            "kind": "image",
        },
        context=context("request.exchange"),
        **changes,
    )


def consume(port: SurfaceResourcePort, selection_id: object, **changes: Any) -> object:
    """Consume in a third authenticated Logic invocation."""
    arguments = {
        "kind": "image",
        "scope": scope(),
        "context": context("request.logic"),
        "assert_current": lambda: None,
        "deadline_monotonic": 100.0,
    }
    arguments.update(changes)
    return port.consume(selection_id, **arguments)


def test_three_rpc_flow_keeps_private_values_out_of_wire_and_rebinds_once(rig) -> None:
    port, provider, _ = rig
    selected = invoke(port)
    assert set(selected) == {
        "version",
        "selection_id",
        "kind",
        "display_name",
        "expires_at_ms",
        "stage",
    }
    assert selected["version"] == "tobkiri.ui.surface-resource.v1"
    assert selected["stage"] == "selected"
    assert 32 <= len(selected["selection_id"]) <= 128
    assert selected["expires_at_ms"] > 0
    exchanged = exchange(port, selected["selection_id"])
    assert exchanged["stage"] == "exchanged"
    assert exchanged["selection_id"] != selected["selection_id"]
    assert "private" not in json.dumps([selected, exchanged])
    local = consume(port, exchanged["selection_id"])
    assert local is provider.consumed[0]
    assert local["request_id"] == "request.logic"
    assert provider.requests == ["request.exchange", "request.logic"]
    assert provider.acquired[0] in provider.revoked
    assert provider.exchanged[0] in provider.revoked
    with pytest.raises(SurfaceResourceRejected):
        consume(port, exchanged["selection_id"])
    port.close()
    assert provider.consumed[0] in provider.revoked


def test_unbound_provider_and_missing_rebinding_are_unavailable(rig) -> None:
    with pytest.raises(SurfaceResourceUnavailable):
        invoke(SurfaceResourcePort())
    _, provider, clock = rig
    port = SurfaceResourcePort(
        provider.adapter(consume=None), clock=clock.monotonic, wall_clock=clock.wall
    )
    selected = invoke(port)
    exchanged = exchange(port, selected["selection_id"])
    with pytest.raises(SurfaceResourceUnavailable):
        consume(port, exchanged["selection_id"])
    port.close()


@pytest.mark.parametrize(
    "field",
    [
        "approved",
        "profile_id",
        "profile_revision",
        "principal",
        "handle",
        "path",
        "raw_path",
        "owner_pack_id",
        "security_epoch",
        "_private",
        "selection_id",
    ],
)
def test_public_acquisition_rejects_authority_paths_and_extra_fields(
    rig, field
) -> None:
    port, provider, _ = rig
    with pytest.raises(SurfaceResourceRejected):
        invoke(port, request={"operation": "acquire", "kind": "image", field: True})
    assert not provider.acquired


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "consume", "kind": "image"},
        {"operation": [], "kind": "image"},
        {"operation": "acquire", "kind": []},
        {"operation": "acquire", "kind": "uri"},
        {"operation": "exchange", "kind": "image", "selection_id": "handle:forged"},
        {"operation": "exchange", "kind": "image", "selection_id": "x" * 129},
    ],
)
def test_malformed_public_requests_fail_closed(rig, payload) -> None:
    port, provider, _ = rig
    with pytest.raises(SurfaceResourceRejected):
        invoke(port, request=payload)
    assert not provider.acquired


@pytest.mark.parametrize(
    "field",
    [
        field
        for field in scope().__dataclass_fields__
        if field != "renderer_api_version"
    ],
)
def test_every_scope_pin_rejects_cross_capture_exchange(rig, field) -> None:
    port, provider, _ = rig
    selected = invoke(port)
    original = getattr(scope(), field)
    alternate = (
        8
        if field == "security_epoch"
        else original + 1
        if field == "renderer_expires_at_ms"
        else (
            digest("1")
            if field.endswith(("_hash", "_digest")) or field == "profile_revision"
            else original + ".other"
        )
    )
    with pytest.raises(SurfaceResourceRejected):
        exchange(
            port, selected["selection_id"], scope=replace(scope(), **{field: alternate})
        )
    assert not provider.exchanged
    assert exchange(port, selected["selection_id"])["stage"] == "exchanged"


@pytest.mark.parametrize(
    "field",
    [
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_digest",
        "security_epoch",
    ],
)
def test_authenticated_context_must_match_server_capture(rig, field) -> None:
    port, provider, _ = rig
    value = (
        8
        if field == "security_epoch"
        else (
            digest("1")
            if field in {"profile_revision", "plan_digest"}
            else "other.identity"
        )
    )
    with pytest.raises(SurfaceResourceRejected):
        invoke(port, context=replace(context(), **{field: value}))
    assert not provider.acquired


def test_forgery_stage_kind_mismatch_and_replay_do_not_dispatch(rig) -> None:
    port, provider, _ = rig
    selected = invoke(port)
    with pytest.raises(SurfaceResourceRejected):
        exchange(port, "x" * 43)
    with pytest.raises(SurfaceResourceRejected):
        invoke(
            port,
            request={
                "operation": "exchange",
                "kind": "file",
                "selection_id": selected["selection_id"],
            },
        )
    with pytest.raises(SurfaceResourceRejected):
        consume(port, selected["selection_id"])
    exchanged = exchange(port, selected["selection_id"])
    with pytest.raises(SurfaceResourceRejected):
        exchange(port, selected["selection_id"])
    with pytest.raises(SurfaceResourceRejected):
        exchange(port, exchanged["selection_id"])
    assert len(provider.exchanged) == 1


def test_consume_revalidates_exact_consumer_and_session_scope(rig) -> None:
    port, provider, _ = rig
    token = exchange(port, invoke(port)["selection_id"])["selection_id"]
    for field in (
        "consumer_operation_id",
        "consumer_function_id",
        "presentation_owner_session_id",
        "renderer_id",
    ):
        with pytest.raises(SurfaceResourceRejected):
            consume(port, token, scope=replace(scope(), **{field: "other.identity"}))
    assert not provider.consumed
    consume(port, token)


@pytest.mark.parametrize("ttl", [0, 301, True, float("nan"), float("inf")])
def test_invalid_server_ttl_never_dispatches(rig, ttl) -> None:
    port, provider, _ = rig
    with pytest.raises(SurfaceResourceRejected):
        invoke(port, ttl_seconds=ttl)
    assert not provider.acquired


def test_expiry_is_capped_by_capture_deadline_and_cannot_be_extended(rig) -> None:
    port, provider, clock = rig
    selected = invoke(port, ttl_seconds=300, deadline_monotonic=15)
    assert selected["expires_at_ms"] == int((clock.wall() + 5) * 1000)
    exchanged = exchange(port, selected["selection_id"], deadline_monotonic=13)
    assert exchanged["expires_at_ms"] == int((clock.wall() + 3) * 1000)
    clock.now = 13
    with pytest.raises(SurfaceResourceRejected):
        consume(port, exchanged["selection_id"])
    assert provider.exchanged[0] in provider.revoked
    assert not provider.consumed


def test_entry_quota_includes_consumed_resources_until_cleanup() -> None:
    provider, clock = Provider(), Clock()
    port = SurfaceResourcePort(
        provider.adapter(), max_entries=1, clock=clock.monotonic, wall_clock=clock.wall
    )
    token = exchange(port, invoke(port)["selection_id"])["selection_id"]
    local = consume(port, token)
    with pytest.raises(SurfaceResourceRejected, match="quota"):
        invoke(port)
    clock.now = 40
    invoke(port)
    assert local in provider.revoked
    port.close()


def test_provider_failure_does_not_leak_private_text_or_restore_token(rig) -> None:
    _, provider, clock = rig

    def fail(reference: object, request: RequestContext) -> PrivateSurfaceSelection:
        raise RuntimeError("/private/path secret-principal")

    port = SurfaceResourcePort(
        provider.adapter(exchange=fail), clock=clock.monotonic, wall_clock=clock.wall
    )
    selected = invoke(port)
    with pytest.raises(SurfaceResourceProviderError) as error:
        exchange(port, selected["selection_id"])
    assert "private" not in str(error.value)
    assert provider.acquired[0] in provider.revoked
    with pytest.raises(SurfaceResourceRejected):
        exchange(port, selected["selection_id"])
    port.close()


def test_changed_capture_after_provider_effect_revokes_unpublished_result(rig) -> None:
    _, provider, clock = rig
    current = True

    def acquire(kind: str, request: RequestContext) -> PrivateSurfaceSelection:
        nonlocal current
        result = provider.acquire(kind, request)
        current = False
        return result

    def guard() -> None:
        if not current:
            raise RuntimeError("capture changed")

    port = SurfaceResourcePort(
        provider.adapter(acquire=acquire), clock=clock.monotonic, wall_clock=clock.wall
    )
    with pytest.raises(SurfaceResourceRejected):
        invoke(port, assert_current=guard)
    assert provider.acquired == provider.revoked
    port.close()


@pytest.mark.parametrize("phase", ["acquire", "exchange", "consume"])
def test_close_race_cancels_pending_effect_and_revokes_late_output(phase) -> None:
    provider, clock = Provider(), Clock()
    entered, release, cancelled = Event(), Event(), Event()

    def blocked(*arguments: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return getattr(provider, phase)(*arguments)

    adapter = provider.adapter(**{phase: blocked}, cancel=cancelled.set)
    port = SurfaceResourcePort(adapter, clock=clock.monotonic, wall_clock=clock.wall)
    if phase == "acquire":
        action = partial(invoke, port)
    else:
        selected = invoke(port)["selection_id"]
        if phase == "exchange":
            action = partial(exchange, port, selected)
        else:
            exchanged = exchange(port, selected)["selection_id"]
            action = partial(consume, port, exchanged)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(action)
        try:
            assert entered.wait(5)
            port.close()
            assert cancelled.is_set()
        finally:
            release.set()
        with pytest.raises(SurfaceResourceUnavailable):
            future.result(timeout=5)
    assert all(
        any(item is revoked for revoked in provider.revoked)
        for item in provider.acquired + provider.exchanged + provider.consumed
    )
    with pytest.raises(SurfaceResourceUnavailable):
        invoke(port)


def test_typed_action_adapter_uses_authenticated_envelope_and_server_scope(rig) -> None:
    port, _, _ = rig
    checks: list[bool] = []
    invocation = SimpleNamespace(
        envelope=SimpleNamespace(context=context(), deadline_monotonic=100.0),
        assert_current=lambda: checks.append(True),
        presentation_owner_principal_id=scope().presentation_owner_principal_id,
        presentation_owner_session_id=scope().presentation_owner_session_id,
    )
    adapter = SurfaceResourceActionAdapter(port, lambda authenticated: scope())
    result = adapter.invoke({"operation": "acquire", "kind": "image"}, invocation)
    assert result["stage"] == "selected"
    assert len(checks) >= 3
    with pytest.raises(SurfaceResourceRejected):
        adapter.invoke(
            {"operation": "acquire", "kind": "image", "profile_id": "profile.one"},
            invocation,
        )


@pytest.mark.parametrize(
    "field",
    [
        "presentation_owner_principal_id",
        "presentation_owner_session_id",
    ],
)
def test_action_adapter_rejects_changed_authenticated_origin(rig, field: str) -> None:
    """A trusted scope callback cannot substitute the actual originating owner."""
    port, provider, _ = rig
    invocation = SimpleNamespace(
        envelope=SimpleNamespace(context=context(), deadline_monotonic=100.0),
        assert_current=lambda: None,
        presentation_owner_principal_id=scope().presentation_owner_principal_id,
        presentation_owner_session_id=scope().presentation_owner_session_id,
    )
    setattr(invocation, field, "other.owner")
    with pytest.raises(SurfaceResourceRejected):
        SurfaceResourceActionAdapter(port, lambda authenticated: scope()).invoke(
            {"operation": "acquire", "kind": "image"},
            invocation,
        )
    assert provider.acquired == []


def test_failed_close_cleanup_is_quarantined_and_can_be_retried(rig) -> None:
    _, provider, clock = rig
    fail = True

    def revoke(reference: object) -> None:
        if fail:
            raise RuntimeError("private cleanup details")
        provider.revoke(reference)

    port = SurfaceResourcePort(
        provider.adapter(revoke=revoke), clock=clock.monotonic, wall_clock=clock.wall
    )
    invoke(port)
    with pytest.raises(SurfaceResourceProviderError):
        port.close()
    with pytest.raises(SurfaceResourceUnavailable):
        invoke(port)
    fail = False
    port.close()
    assert provider.acquired == provider.revoked


def test_resource_lifetime_cannot_outlive_captured_renderer(rig) -> None:
    """A fresh action deadline cannot extend the original renderer lease."""
    port, provider, clock = rig
    pinned = replace(scope(), renderer_expires_at_ms=int((clock.wall() + 3) * 1000))
    selected = invoke(port, scope=pinned, ttl_seconds=300)
    assert selected["expires_at_ms"] == pinned.renderer_expires_at_ms
    clock.now += 3
    with pytest.raises(SurfaceResourceRejected):
        exchange(port, selected["selection_id"], scope=pinned)
    assert not provider.exchanged


def test_unknown_renderer_api_is_not_a_server_resource_capture() -> None:
    """No client/future API can be substituted for the selected renderer."""
    with pytest.raises(ValueError):
        replace(scope(), renderer_api_version="2.0.0")


def test_action_adapter_consume_binds_actual_contract_and_operation(rig) -> None:
    """The exchanged token is not a bearer grant for another consumer."""
    port, provider, _ = rig
    ticket = exchange(port, invoke(port)["selection_id"])
    invocation = SimpleNamespace(
        envelope=SimpleNamespace(
            context=context(),
            deadline_monotonic=100.0,
            contract_id="other.contract",
            contract_version=scope().consumer_contract_version,
            operation_id=scope().consumer_operation_id,
            target_principal=OpaqueAuthorityRef(scope().consumer_principal_id),
        ),
        assert_current=lambda: None,
        presentation_owner_principal_id=scope().presentation_owner_principal_id,
        presentation_owner_session_id=scope().presentation_owner_session_id,
    )
    adapter = SurfaceResourceActionAdapter(port, lambda authenticated: scope())
    with pytest.raises(SurfaceResourceRejected):
        adapter.consume(ticket["selection_id"], kind="image", invocation=invocation)
    assert not provider.consumed
    invocation.envelope.contract_id = scope().consumer_contract_id
    invocation.envelope.operation_id = "other.operation"
    with pytest.raises(SurfaceResourceRejected):
        adapter.consume(ticket["selection_id"], kind="image", invocation=invocation)
    invocation.envelope.operation_id = scope().consumer_operation_id
    invocation.envelope.contract_version = "2.0.0"
    with pytest.raises(SurfaceResourceRejected):
        adapter.consume(ticket["selection_id"], kind="image", invocation=invocation)
    invocation.envelope.contract_version = scope().consumer_contract_version
    invocation.envelope.target_principal = OpaqueAuthorityRef("principal.other")
    with pytest.raises(SurfaceResourceRejected):
        adapter.consume(ticket["selection_id"], kind="image", invocation=invocation)
    invocation.envelope.target_principal = OpaqueAuthorityRef(
        scope().consumer_principal_id
    )
    assert adapter.consume(ticket["selection_id"], kind="image", invocation=invocation)
    assert len(provider.consumed) == 1
