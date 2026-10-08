"""Finite, native-approved creation of one absent selected-workspace file."""

from __future__ import annotations

import hashlib
import json
import re
import time
from threading import BoundedSemaphore
from typing import Any, Mapping
import uuid

from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)
from core_runtime.file_edit_receipts import committed_file_edit_receipt
from core_runtime.owned_file_approval_v4 import (
    assert_file_request_live,
    close_file_tool_request,
    open_file_tool_approval,
    file_tool_edit_after_success,
    record_file_tool_edit,
    register_file_tool_request,
)
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
    capture_selected_workspace_binding,
)
from tobkiri_host.ports import WorkspaceMutationIdentity, WorkspaceMutationLeaseRequest
from tobkiri_host.workspace_mutation import WorkspaceMutationBinding
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.file_create_v1 import (
    PLAN_VERSION,
    file_arguments,
    validate_execute_payload,
)

PACK = "rumi_default_tools_pack"
CONTRACT = "tobkiri.service.file.create.v1"
PREPARE = f"{PACK}.file-create-prepare"
EXECUTE = f"{PACK}.file-create"
LOCAL_CONTRACT = "tobkiri.service.tool.local.operation.v1"
LOCAL_OPERATION = f"{PACK}.file-create-operation"
LOCAL_FUNCTION = f"{PACK}.file-create-tool"
EFFECT = "tobkiri.service.interactive-effect.v1"
EFFECT_OPERATION = "interactive_effect.manage"


def _binding(context: Any) -> WorkspaceMutationBinding:
    value = capture_selected_workspace_binding(
        context.profile_id, user_data_root=context.user_data_root
    )
    return WorkspaceMutationBinding.from_mapping(value, profile_id=context.profile_id)


def _identity(invocation: Any) -> WorkspaceMutationIdentity:
    envelope = invocation.envelope
    value = envelope.context
    return WorkspaceMutationIdentity(
        context=value,
        target_principal=envelope.target_principal,
        target_domain_id=value.target_domain_id,
        target_boot_epoch=value.target_boot_epoch,
        target_namespace=value.handle_namespace,
    )


def _request(value: Any, binding: WorkspaceMutationBinding) -> tuple[str, bytes]:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {"workspace_id", "expected_mount_revision", "path", "content", "invocation_key"}
        or value["workspace_id"] != binding.workspace_id
        or type(value["expected_mount_revision"]) is not int
        or value["expected_mount_revision"] != binding.mount_revision
    ):
        raise PermissionError("file create selected workspace changed")
    return file_arguments({key: value[key] for key in ("path", "content")})


def create_plan(request: Mapping[str, Any], binding: WorkspaceMutationBinding) -> dict[str, Any]:
    """Seal exact content, selected mount identity and absent-file intent."""
    path, data = _request(request, binding)
    plan = {
        "version": PLAN_VERSION,
        "profile_id": binding.profile_id,
        "workspace_id": binding.workspace_id,
        "mount_revision": binding.mount_revision,
        "canonical_root": str(binding.canonical_root),
        "root_st_dev": binding.root_st_dev,
        "root_st_ino": binding.root_st_ino,
        "path": path,
        "byte_count": len(data),
        "content_digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "request_digest": canonical_digest(dict(request)),
        "preimage": "absent",
    }
    return {**plan, "plan_digest": canonical_digest(plan)}


def _bind_prepare(context: Any) -> HostFunction:
    if context.user_data_root is None or context.workspace_mutation_port is None:
        raise PermissionError("file create Host ports are unavailable")

    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        invocation.assert_current()
        binding = _binding(context)
        path, data = _request(payload, binding)
        assert_file_request_live(payload, invocation.envelope.context)
        identity = _identity(invocation)
        port = context.workspace_mutation_port
        lease = port.acquire_lease(WorkspaceMutationLeaseRequest(identity, binding))
        try:
            port.bind_absent(
                lease,
                identity,
                relative_path=path,
                ttl_seconds=30,
                max_uses=1,
                max_bytes=len(data),
            )
            invocation.assert_current()
            return create_plan(payload, binding)
        finally:
            port.close_lease(lease, identity)

    return invoke


def _bind_execute(context: Any) -> HostFunction:
    if context.user_data_root is None or context.workspace_mutation_port is None:
        raise PermissionError("file create Host ports are unavailable")

    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        invocation.assert_current()
        if set(payload) != {"request", "plan"}:
            raise ValueError("file create execute payload is invalid")
        request, plan = payload["request"], payload["plan"]
        validate_execute_payload(request, plan)
        binding = _binding(context)
        expected = create_plan(request, binding)
        if plan != expected or binding.profile_id != invocation.envelope.context.profile_id:
            raise PermissionError("file create selected workspace changed")
        path, data = _request(request, binding)
        assert_file_request_live(request, invocation.envelope.context)
        identity = _identity(invocation)
        port = context.workspace_mutation_port
        lease = port.acquire_lease(WorkspaceMutationLeaseRequest(identity, binding))
        try:
            handle = port.bind_absent(
                lease,
                identity,
                relative_path=path,
                ttl_seconds=30,
                max_uses=1,
                max_bytes=len(data),
            )
            invocation.assert_current()
            assert_file_request_live(request, invocation.envelope.context)
            written = port.create_file(lease, identity, handle, data, mode=0o600)
            try:
                record_file_tool_edit(
                    request,
                    invocation.envelope.context,
                    committed_file_edit_receipt(
                        mutation_id=request["invocation_key"],
                        operation="create",
                        profile_id=binding.profile_id,
                        workspace_id=binding.workspace_id,
                        root_identity=(
                            str(binding.canonical_root),
                            binding.root_st_dev,
                            binding.root_st_ino,
                        ),
                        frame_identity=invocation.envelope.context.request_id,
                        path=path,
                        before=None,
                        after=data,
                        sensitive=any(
                            part.lower() in {".env", ".secrets", "secrets"}
                            or part.lower().startswith(".env.")
                            for part in path.split("/")
                        ),
                    ),
                )
            except (PermissionError, ValueError, TypeError):
                # Optional metadata must not change an already published save.
                # The Broker still owns cancellation, audit and final success.
                pass
            return {
                "created": True,
                "workspace_id": binding.workspace_id,
                "path": path,
                "byte_count": written,
                "content_digest": expected["content_digest"],
            }
        finally:
            port.close_lease(lease, identity)

    return invoke


def _bind_tool(context: Any) -> HostFunction:
    if (
        context.user_data_root is None
        or context.interactive_approval_port is None
        or context.authority_approval_window_port is None
    ):
        raise PermissionError("file tool native approval is unavailable")

    single_flight = BoundedSemaphore(1)

    def run(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        if (
            set(payload) != {"tool_id", "tool_call_id", "arguments"}
            or payload["tool_id"] != "coding_file_create"
            or not isinstance(payload["tool_call_id"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", payload["tool_call_id"]) is None
        ):
            raise ValueError("file tool payload is invalid")
        path, data = file_arguments(payload["arguments"])
        invocation.assert_current()
        binding = _binding(context)
        request = {
            **dict(payload["arguments"]),
            "workspace_id": binding.workspace_id,
            "expected_mount_revision": binding.mount_revision,
        }
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({EFFECT}),
            consumer_pack_id=PACK,
            include_credentials=False,
        )
        policy_capture = getattr(context, "selected_tool_policy_port", None)
        inheritance = policy_capture(invocation) if policy_capture is not None else None
        request = register_file_tool_request(
            request, invocation, policy_inheritance=inheritance,
        )
        try:
            effect = client.invoke(
                EFFECT,
                EFFECT_OPERATION,
                {
                    "phase": "prepare",
                    "effect_kind": "file_create",
                    "request": request,
                    "correlation_id": str(uuid.uuid4()),
                },
            )
            effect_id = effect.get("effect_id")
            if not isinstance(effect_id, str):
                raise PermissionError("file tool approval is unavailable")
            try:
                if inheritance is None:
                    open_file_tool_approval(
                        invocation,
                        effect_status=effect,
                        approval_port=context.interactive_approval_port,
                        window_port=context.authority_approval_window_port,
                    )
                else:
                    # The exact inner write uses the controller's typed policy
                    # authorization; no future native approval is fabricated.
                    inheritance.assert_current()
                end = time.monotonic() + 90
                resumed = False
                while time.monotonic() < end:
                    invocation.assert_current()
                    effect = client.invoke(
                        EFFECT,
                        EFFECT_OPERATION,
                        {
                            "phase": "status",
                            "effect_id": effect_id,
                        },
                    )
                    if effect.get("effect_id") != effect_id:
                        raise PermissionError("file tool approval changed")
                    state = effect.get("state")
                    if state == "approved" and not resumed:
                        resumed = True
                        effect = client.invoke(
                            EFFECT,
                            EFFECT_OPERATION,
                            {
                                "phase": "resume",
                                "effect_id": effect_id,
                            },
                        )
                        if effect.get("effect_id") != effect_id:
                            raise PermissionError("file tool approval changed")
                        state = effect.get("state")
                    if state == "succeeded" and resumed:
                        try:
                            receipt = file_tool_edit_after_success(
                                request, invocation.envelope.context, effect_id
                            )
                        except PermissionError:
                            receipt = None
                        return {
                            "result": json.dumps(
                                {
                                    "created": True,
                                    "path": path,
                                    "workspace_id": binding.workspace_id,
                                    "byte_count": len(data),
                                    "content_digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                                    **({"file_edit_receipt": receipt} if receipt else {}),
                                },
                                sort_keys=True,
                            ),
                            "is_error": False,
                            "widget": None,
                        }
                    if state not in {"approval_pending", "approved", "claimed", "dispatched"}:
                        raise PermissionError("file create was not approved and completed")
                    time.sleep(0.25)
                raise PermissionError("file create approval wait expired")
            except BaseException:
                # Cancellation/expiry leaves no runnable future effect. The guarded
                # client may already be fenced; the controller's expiry/capture
                # guards remain authoritative if this cleanup cannot be dispatched.
                try:
                    client.invoke(
                        EFFECT, EFFECT_OPERATION, {"phase": "cancel", "effect_id": effect_id}
                    )
                except Exception:
                    pass
                raise
        finally:
            close_file_tool_request(request["invocation_key"])

    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        # Keep one finite approval wait per capture so multiple saved trees
        # cannot exhaust the Broker workers required by the native decision.
        if not single_flight.acquire(blocking=False):
            raise PermissionError("file tool approval is already pending")
        try:
            return run(payload, invocation)
        finally:
            single_flight.release()

    return invoke


HOST_PROVIDER_FACTORY = {
    f"{PACK}.file-create-prepare.service": SingleOperationHostFactoryV4(
        f"{PACK}.file-create-prepare.service",
        CONTRACT,
        PREPARE,
        _bind_prepare,
    ),
    f"{PACK}.file-create.service": SingleOperationHostFactoryV4(
        f"{PACK}.file-create.service",
        CONTRACT,
        EXECUTE,
        _bind_execute,
    ),
    LOCAL_FUNCTION: SingleOperationHostFactoryV4(
        LOCAL_FUNCTION,
        LOCAL_CONTRACT,
        LOCAL_OPERATION,
        _bind_tool,
    ),
}
