"""Host-private production composition for finite saved-tool consent."""

from __future__ import annotations

from typing import Any, Callable, Mapping

LOCAL_EXECUTOR_FUNCTION = "rumi_tool_local_executor_pack.tool-executor.local"
LOCAL_EXECUTOR_CONTRACT = "tobkiri.service.tool.execute.v1"
LOCAL_EXECUTOR_OPERATION = "rumi_tool_local_executor_pack.tool-local-execute"


class LateBoundSavedToolConsentPortV4:
    """Expose one execution callable, without authority or policy storage."""

    def __init__(self) -> None:
        self._execute: Callable[..., Mapping[str, Any]] | None = None

    @property
    def available(self) -> bool:
        """Report availability of the exact optional captured consent feature."""
        return self._execute is not None

    def bind(self, execute: Callable[..., Mapping[str, Any]]) -> None:
        """Bind once after production creates its actual Broker."""
        if self._execute is not None or not callable(execute):
            raise PermissionError("saved tool consent binding is invalid")
        self._execute = execute

    def __call__(
        self, invocation: Any, execution: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Execute only an authenticated invocation of the captured executor."""
        invocation.assert_current()
        envelope = invocation.envelope
        if (envelope.contract_id, envelope.operation_id) != (
            LOCAL_EXECUTOR_CONTRACT,
            LOCAL_EXECUTOR_OPERATION,
        ) or self._execute is None:
            raise PermissionError("saved tool consent route is unavailable")
        return self._execute(invocation, execution, payload)


def captured_consent_route(edges: Any, executor_bindings: Any) -> Any:
    """Attest exactly one actual Profile edge from the verified executor."""
    from tobkiri_host.saved_tool_consent import (
        CONSENT_CONTRACT,
        CONSENT_OPERATION,
        ConsentRouteAttestation,
    )

    if len(executor_bindings) != 1:
        raise PermissionError("saved tool executor capture is ambiguous")
    executor = executor_bindings[0]
    if executor.function.function_id != LOCAL_EXECUTOR_FUNCTION or (
        executor.operation.contract_id,
        executor.operation.operation_id,
    ) != (LOCAL_EXECUTOR_CONTRACT, LOCAL_EXECUTOR_OPERATION):
        raise PermissionError("saved tool executor capture changed")
    matches = tuple(
        edge
        for edge in edges
        if edge.caller.principal_id == executor.principal_ref.value
        and (
            edge.resolved_binding.operation.contract_id,
            edge.resolved_binding.operation.operation_id,
        )
        == (CONSENT_CONTRACT, CONSENT_OPERATION)
    )
    if len(matches) != 1:
        raise PermissionError("signed saved tool consent edge is unavailable")
    edge = matches[0]
    if edge.authority_mode != "interactive_only":
        raise PermissionError("saved tool consent edge is not interactive-only")
    if edge.target.principal_id != edge.resolved_binding.principal_ref.value:
        raise PermissionError("signed saved tool consent target changed")
    route = ConsentRouteAttestation(
        binding=edge.resolved_binding,
        ceiling=edge.ceilings.caller_effect.to_dict(),
    )
    route.validate()
    return route


def assert_saved_tool_requested_mode(invocation: Any) -> str:
    """Reject unsettled modes before every saved local tool execution branch."""
    from tobkiri_host.saved_tool_context import saved_tool_owner_and_request_scope

    _, accepted = saved_tool_owner_and_request_scope(invocation)
    saved = [accepted]
    request = saved[0].envelope.payload.get("request")
    if not isinstance(request, Mapping):
        raise PermissionError("saved tool mode request is unavailable")
    if request.get("action_approval_mode", "ask") != "ask":
        raise PermissionError("saved tool elevated mode settlement is unavailable")
    return "ask"


def optional_captured_consent_route(edges: Any, executor_bindings: Any) -> Any:
    """Disable only finite consent when its optional signed topology is absent.

    The existing native file-create route remains available. No fallback Grant
    or authority is created for a missing, ambiguous or changed consent edge.
    """
    try:
        return captured_consent_route(edges, executor_bindings)
    except PermissionError:
        return None


def capture_saved_tool_requested_mode(invocation: Any) -> str:
    """Read a preference only after actual saved ancestry and owner validation."""
    from tobkiri_host.saved_tool_context import saved_tool_owner_and_request_scope

    _, accepted = saved_tool_owner_and_request_scope(invocation)
    request = accepted.envelope.payload.get("request")
    if not isinstance(request, Mapping):
        raise PermissionError("saved tool mode request is unavailable")
    mode = request.get("action_approval_mode", "ask")
    if mode not in {"ask", "agent", "full"}:
        raise PermissionError("unsupported saved tool requested mode")
    return mode
