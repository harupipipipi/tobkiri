"""Separate clipboard read and write requests for the Viewer host broker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_host.effects import ProviderOutcome


_MAX_TEXT_BYTES: Final[int] = 1_048_576
_FORBIDDEN_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {"approved", "approval_token", "authority_token", "viewer_host_approved", "yolo_mode"}
)


@dataclass(frozen=True)
class ClipboardHostService:
    """Build one clipboard HostIntent without directly reading or writing it."""

    access: str
    operation: str

    def invoke(
        self,
        arguments: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a caller-bound clipboard HostIntent or typed denial."""

        normalized_arguments = dict(arguments or {})
        forbidden = sorted(_FORBIDDEN_ARGUMENTS.intersection(normalized_arguments))
        if forbidden:
            return {
                "status": "denied",
                "success": False,
                "error_type": "client_authority_material_forbidden",
                "forbidden_arguments": forbidden,
            }
        normalized_arguments.pop("_contract_consumer_pack_id", None)
        normalized_arguments.pop(
            "_contract_consumer_function_id",
            normalized_arguments.pop("_source_function_id", ""),
        )
        normalized_arguments.pop("profile_id", None)
        if self.access == "read" and normalized_arguments:
            return {
                "status": "denied",
                "success": False,
                "error_type": "clipboard_read_arguments_forbidden",
            }
        if self.access == "write":
            text = normalized_arguments.get("text")
            if not isinstance(text, str):
                return {
                    "status": "denied",
                    "success": False,
                    "error_type": "clipboard_text_required",
                }
            if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
                return {
                    "status": "denied",
                    "success": False,
                    "error_type": "clipboard_text_too_large",
                    "max_bytes": _MAX_TEXT_BYTES,
                }
            normalized_arguments = {"text": text, "format": "text/plain"}
        caller_context = dict(context or {})
        return {
            "type": "host_intent",
            "version": 1,
            "operation": "host.intent.execute",
            "args": normalized_arguments,
            "stream": {"enabled": False},
            "reason": str(caller_context.get("reason") or "").strip(),
            "caller": {
                "pack_id": "",
                "function_id": "",
            },
            "conversation_id": str(
                caller_context.get("conversation_id") or ""
            ).strip(),
            "host_function_id": self.operation,
        }


def create_clipboard_reader(_context: dict[str, Any] | None = None) -> ClipboardHostService:
    """Create the clipboard read provider."""

    return ClipboardHostService(access="read", operation="computer.clipboard.read")


def create_clipboard_writer(_context: dict[str, Any] | None = None) -> ClipboardHostService:
    """Create the clipboard write provider."""

    return ClipboardHostService(access="write", operation="computer.clipboard.write")


@dataclass(frozen=True)
class ClipboardHostFactoryV4:
    """Connect the exact signed clipboard operation to its finite Host port."""

    access: str

    @property
    def function_id(self) -> str:
        """Return the separately authorized read or write Function identity."""
        return f"rumi_clipboard_host_service_pack.clipboard.{self.access}"

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture one exact operation; importing this module grants no access."""
        contract = (
            "tobkiri.resource.clipboard.v1" if self.access == "read"
            else "tobkiri.action.clipboard.v1"
        )
        operation = f"rumi_clipboard_host_service_pack.clipboard-{self.access}"
        if len(context.provider_bindings) != 1:
            raise PermissionError("clipboard requires one exact binding")
        binding = context.provider_bindings[0]
        if (
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != contract
            or binding.operation.operation_id != operation
        ):
            raise PermissionError("clipboard provider binding is invalid")
        key = (contract, operation, binding.principal_ref.value)
        domain = context.domain_ids.get(key)
        if not domain:
            raise PermissionError("clipboard provider domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> ProviderOutcome:
            if (
                operation_id != operation
                or invocation.envelope.context.profile_id != context.profile_id
                or invocation.envelope.target_domain.value != domain
                or invocation.envelope.target_principal != binding.principal_ref
                or dict(payload) != dict(invocation.envelope.payload)
            ):
                raise PermissionError("clipboard invocation binding changed")
            return invocation.clipboard()

        return CapturedHostProviderV4((HostProviderContributionV4(
            contract_id=contract,
            contract_version=binding.operation.contract_version,
            operation_id=operation,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain,
            invoke=invoke,
        ),), lambda: None)


HOST_PROVIDER_FACTORY = {
    factory.function_id: factory
    for factory in (ClipboardHostFactoryV4("read"), ClipboardHostFactoryV4("write"))
}
