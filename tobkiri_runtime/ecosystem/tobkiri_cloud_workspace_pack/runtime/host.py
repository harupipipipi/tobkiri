"""Exact Profile/Plan-bound Host factories with no privileged execution path."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.service import (
    INSPECT,
    WORKSPACE,
    CloudWorkspace,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.store import WorkspaceStore
from ecosystem.tobkiri_cloud_workspace_pack.runtime.task import DEPENDENCIES
from tobkiri_protocol.workspace_capsule_v1 import canonical, digest

PACK_ID = "tobkiri_cloud_workspace_pack"
BINDINGS = {
    "resource": ("tobkiri.resource.cloud.workspace.v1", "workspace-resource"),
    "manage": ("tobkiri.action.cloud.workspace.v1", "workspace-manage"),
}
FIELDS = {
    "resource": {
        "get_for_conversation": {"conversation_id"},
        "export": {"workspace_id", "expected_revision"},
        "verify": {"workspace_id", "expected_revision"},
        "task_source": {"workspace_id", "expected_revision", "expected_writer_epoch"},
    },
    "manage": {
        "initialize": {"conversation_id", "expected_revision", "expected_writer_epoch"},
        "capture": {
            "conversation_id",
            "expected_revision",
            "expected_writer_epoch",
            "paths",
        },
        "import": {
            "conversation_id",
            "expected_revision",
            "expected_writer_epoch",
            "archive_base64",
        },
        "restore": {"conversation_id", "expected_revision", "expected_writer_epoch"},
        "prepare_handoff": {
            "conversation_id",
            "expected_revision",
            "expected_writer_epoch",
            "expected_receiver_head",
        },
        "task_prepare": {
            "conversation_id",
            "expected_revision",
            "expected_writer_epoch",
            "argv_json",
        },
        "task_resume": {
            "conversation_id",
            "expected_revision",
            "expected_writer_epoch",
        },
        "task_status": {
            "conversation_id",
            "expected_revision",
            "expected_writer_epoch",
        },
        "task_cancel": {
            "conversation_id",
            "expected_revision",
            "expected_writer_epoch",
        },
        "task_apply": {"conversation_id", "expected_revision", "expected_writer_epoch"},
    },
}


class CloudWorkspaceHostFactoryV4:
    """Expose only the exact selected operation and Pack-owned local state."""

    def __init__(self, kind: str) -> None:
        """Select a statically declared resource or managed action function."""
        self.kind = kind
        self.contract_id, suffix = BINDINGS[kind]
        self.operation_id = f"{PACK_ID}.{suffix}"
        self.function_id = f"{PACK_ID}.{kind}"

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Reject foreign identities and capture an immutable local Profile scope."""
        if context.user_data_root is None or len(context.provider_bindings) != 1:
            raise PermissionError("cloud workspace capture is incomplete")
        binding = context.provider_bindings[0]
        if (
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != self.contract_id
            or binding.operation.operation_id != self.operation_id
            or binding.operation.contract_version != "1.0.0"
        ):
            raise PermissionError("cloud workspace capture identity is invalid")
        domain = context.domain_ids.get(
            (self.contract_id, self.operation_id, binding.principal_ref.value)
        )
        if not domain:
            raise PermissionError("cloud workspace domain is unavailable")
        store = WorkspaceStore(context.user_data_root, context.profile_id)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            envelope = invocation.envelope
            captured = envelope.context
            if (
                operation_id != self.operation_id
                or captured.profile_id != context.profile_id
                or captured.plan_digest != context.plan_digest
                or captured.security_epoch != context.security_epoch
                or envelope.target_principal.value != binding.principal_ref.value
                or captured.target_domain_id != domain
                or payload.get("profile_id", context.profile_id) != context.profile_id
            ):
                raise PermissionError("cloud workspace invocation scope differs")
            action = payload.get("operation")
            if action not in FIELDS[self.kind]:
                raise ValueError("cloud workspace operation is invalid")
            fields = FIELDS[self.kind][action]
            if set(payload) - fields - {
                "profile_id",
                "operation",
                "_session_id",
            } or not fields <= set(payload):
                raise PermissionError("cloud workspace request fields are invalid")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({WORKSPACE, INSPECT}) | DEPENDENCIES,
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
            service = CloudWorkspace(
                store,
                client,
                plan_digest=context.plan_digest,
                actor=invocation.presentation_owner_principal_id,
                request_id=captured.request_id,
                guard=invocation.assert_current,
                private_owner=digest(
                    canonical(
                        [
                            invocation.presentation_owner_principal_id,
                            invocation.presentation_owner_session_id,
                        ]
                    )
                ),
            )
            values = {key: payload[key] for key in fields}
            if self.kind == "resource":
                if action == "get_for_conversation":
                    return service.snapshot(values["conversation_id"])
                if action == "task_source":
                    return service.task_source(**values)
                return getattr(service, action)(
                    values["workspace_id"], values["expected_revision"]
                )
            return service.invoke(action, values)

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=self.contract_id,
                    contract_version="1.0.0",
                    operation_id=self.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {
    f"{PACK_ID}.{kind}": CloudWorkspaceHostFactoryV4(kind) for kind in BINDINGS
}
