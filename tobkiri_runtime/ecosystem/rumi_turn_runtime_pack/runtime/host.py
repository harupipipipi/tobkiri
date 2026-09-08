"""Captured turn contracts share durable owner state, never execution authority."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

_PACK = "rumi_turn_runtime_pack"
_CONTRACTS = {
    "lifecycle": ("tobkiri.action.turn.lifecycle.v1", "turn-lifecycle"),
    "resource": ("tobkiri.resource.turn.v1", "turn-resource"),
    "events": ("tobkiri.event.turn.v1", "turn-events"),
}
_MUTATIONS = {
    "transition": ({"status"}, {"details"}),
    "steer": ({"guidance"}, set()),
    "handoff": ({"target"}, set()),
    "consume_guidance": (set(), {"guidance_ids"}),
    "cancel_guidance": ({"guidance_id"}, set()),
}


class TurnHostFactoryV4:
    """Bind one exact turn function to a captured Profile and data root."""

    def __init__(self, kind: str) -> None:
        """Select only a statically declared owner contract."""
        self.contract_id, operation = _CONTRACTS[kind]
        self.kind = kind
        self.function_id = f"{_PACK}.turn-runtime.{kind}"
        self.operation_id = f"{_PACK}.{operation}"

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture immutable routing identity without creating durable files."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("turn capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != self.function_id
            or operation.contract_id != self.contract_id
            or operation.operation_id != self.operation_id
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("turn capture binding is invalid")
        domain_id = context.domain_ids.get(
            (self.contract_id, self.operation_id, binding.principal_ref.value)
        )
        if not domain_id:
            raise PermissionError("turn capture domain is unavailable")
        store = DurableTurnRuntime(context.profile_id, user_data_root=context.user_data_root)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            del invocation  # The captured backend/Broker enforce execution authority.
            if operation_id != self.operation_id or payload.get("profile_id") != store.profile_id:
                raise PermissionError("turn request does not match capture")
            action = payload.get("operation")
            if not isinstance(action, str):
                raise ValueError("turn operation is required")
            values = {
                key: value
                for key, value in payload.items()
                if key not in {"profile_id", "operation", "_session_id"}
            }
            if self.kind != "lifecycle":
                if action == "get":
                    _fields(values, {"turn_id"})
                    record = store.get(_identifier(values["turn_id"]))
                    if record is None:
                        raise KeyError("turn is unavailable")
                    return record
                if action == "list":
                    _fields(values, set(), {"limit", "conversation_id"})
                    conversation_id = values.get("conversation_id")
                    if "conversation_id" in values:
                        conversation_id = _identifier(conversation_id)
                    return {
                        "turns": store.list(
                            limit=values.get("limit", 100), conversation_id=conversation_id
                        )
                    }
                raise PermissionError("turn read operation is not permitted")
            if action == "begin":
                _fields(
                    values, {"turn_id", "request_id", "conversation_id", "conversation_revision"},
                    {"input_digest"},
                )
                return store.begin(values)
            if action not in _MUTATIONS:
                raise PermissionError("turn mutation is not permitted")
            required, optional = _MUTATIONS[action]
            _fields(values, required | {"turn_id", "expected_revision"}, optional)
            turn_id = _identifier(values.pop("turn_id"))
            revision = values.pop("expected_revision")
            for field in ("guidance", "target", "details"):
                if field in values and not isinstance(values[field], Mapping):
                    raise ValueError("turn mutation requires an object")
            for field in ("status", "guidance_id"):
                if field in values:
                    values[field] = _identifier(values[field])
            if "guidance_ids" in values:
                ids = values["guidance_ids"]
                if not isinstance(ids, list) or len(ids) > 200:
                    raise ValueError("turn guidance IDs must be a bounded list")
                values["guidance_ids"] = [_identifier(value) for value in ids]
            return store.mutate(action, turn_id, expected_revision=revision, **values)

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=self.contract_id,
                    contract_version=operation.contract_version,
                    operation_id=self.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


def _fields(
    values: Mapping[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    if not required <= set(values) or set(values) - required - (optional or set()):
        raise PermissionError("turn operation fields are invalid")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or value.strip() != value:
        raise ValueError("turn identifier is required")
    return value


HOST_PROVIDER_FACTORY = {
    f"{_PACK}.turn-runtime.{kind}": TurnHostFactoryV4(kind) for kind in _CONTRACTS
}
