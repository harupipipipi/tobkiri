from __future__ import annotations

import base64
import json
import platform
import threading
import uuid
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from blocks._common import error, ok, timestamp
except ModuleNotFoundError:
    from ecosystem.defaultspack.blocks._common import error, ok, timestamp

try:
    from ecosystem.defaultspack.domain.tool_policy.internal_context import (
        internal_tool_decision,
        internal_tool_decision_allows,
        tool_server_approval_context_is_internal,
    )
except ModuleNotFoundError:
    from domain.tool_policy.internal_context import internal_tool_decision, internal_tool_decision_allows, tool_server_approval_context_is_internal  # type: ignore

from ecosystem.defaultspack.backend.sandbox.control_lease import ControlLeaseManager
from ecosystem.defaultspack.backend.sandbox.desktop_access_exchange import DesktopAccessExchange
from ecosystem.defaultspack.backend.sandbox.cancellation import (
    CancellationRegistry,
    CancellationToken,
    RuntimeOperationCancelled,
    cancellation_context,
)
from ecosystem.defaultspack.backend.sandbox.errors import SandboxContractError
from ecosystem.defaultspack.backend.sandbox.frame_cache import FrameCache
from ecosystem.defaultspack.backend.sandbox.models import (
    EnsureRuntimeRequest,
    OperationResult,
    ProgressEvent,
    RuntimeProviderStatus,
    RuntimeRequirements,
    UninstallRuntimeRequest,
    UpdateRuntimeRequest,
)
from ecosystem.defaultspack.backend.sandbox.lifecycle_sweeper import LifecycleSweeper
from ecosystem.defaultspack.backend.sandbox.operation_store import RuntimeOperationStore
from ecosystem.defaultspack.backend.sandbox.provider_registry import ProviderRegistry
from ecosystem.defaultspack.backend.sandbox.providers import (
    CloudflareSandboxBridgeProvider,
    DockerProvider,
    LinuxNativeProvider,
    MacLimaProvider,
    WindowsWslProvider,
)
from ecosystem.defaultspack.backend.sandbox.sandbox_manager import SandboxManager
from ecosystem.defaultspack.backend.sandbox.template_catalog import sandbox_template_catalog
from ecosystem.defaultspack.domain.artifact.store import ArtifactStore
from ecosystem.defaultspack.domain.tool.schema_adapter import list_or_empty, mapping_or_empty


RUNTIME_NOT_READY = "MANAGED_RUNTIME_NOT_READY"
LOCAL_DESKTOP_PRINCIPAL_ID = "local-user"
RUNNING_STATES = {"ready", "busy", "running"}
DESKTOP_RUNTIME_CAPABILITIES = frozenset({"sandbox.desktop", "sandbox.desktop_input", "sandbox.snapshot"})


class _SandboxApiService:
    def __init__(self, *, start_lifecycle_sweeper: bool = False, lifecycle_sweep_interval_seconds: float = 30.0) -> None:
        self.provider_registry = ProviderRegistry()
        self.provider_registry.register(LinuxNativeProvider())
        self.provider_registry.register(MacLimaProvider())
        self.provider_registry.register(WindowsWslProvider())
        self.provider_registry.register(DockerProvider())
        self.provider_registry.register(CloudflareSandboxBridgeProvider())
        self.manager = SandboxManager(provider_registry=self.provider_registry)
        self.desktop_exchange = DesktopAccessExchange(
            self.manager.state_dir / "desktop_access_exchange.json"
        )
        self.operation_store = RuntimeOperationStore(self.manager.state_dir / "runtime_operations.json")
        self.operation_cancellations = CancellationRegistry()
        self.operation_store.interrupt_nonterminal(
            updated_at=timestamp(),
            message="Runtime operation was interrupted by a Rumi service restart. Start the operation again to continue.",
        )
        self.frame_cache = FrameCache()
        self.lease_manager = ControlLeaseManager()
        self.lifecycle_sweeper = LifecycleSweeper(
            lambda: _sweep_lifecycle(self),
            interval_seconds=lifecycle_sweep_interval_seconds,
        )
        if start_lifecycle_sweeper:
            self.lifecycle_sweeper.start()

    def close(self) -> None:
        self.lifecycle_sweeper.stop()


_SERVICE: _SandboxApiService | None = None
_SERVICE_LOCK = threading.RLock()


def run(input_data: dict[str, Any] | None, context: dict[str, Any] | None = None):
    payload = input_data if isinstance(input_data, dict) else {}
    context_payload = context if isinstance(context, dict) else {}
    if _has_legacy_desktop_key(payload):
        return _api_error(
            "Reusable desktop access keys are no longer accepted; exchange a one-time code for a scoped session credential.",
            "DESKTOP_ACCESS_KEY_MIGRATION_REQUIRED",
            400,
        )
    handler = str(payload.get("_handler") or "runtime_providers")
    service = _service()
    try:
        if handler == "runtime_providers":
            return ok(_runtime_providers(service))
        if handler == "runtime_doctor":
            return ok(_runtime_doctor(service))
        if handler == "runtime_ensure":
            return ok(_runtime_ensure(service, payload))
        if handler == "runtime_update":
            return ok(_runtime_update(service, payload))
        if handler == "runtime_uninstall":
            return _runtime_operation_response(_runtime_uninstall(service, payload))
        if handler == "runtime_operations":
            return ok({"operations": _operation_store(service).list()})
        if handler in {"runtime_operation", "runtime_operation_get"}:
            return _runtime_operation_get(service, payload)
        if handler in {"runtime_cancel", "runtime_operation_cancel"}:
            return _runtime_operation_cancel(service, payload)
        if handler == "sandbox_templates":
            return ok({"templates": _template_summaries()})
        if handler == "sandboxes_list":
            return ok({"sandboxes": [_sandbox_payload(item) for item in service.manager.list_instances()]})
        if handler == "sandboxes_create":
            return _sandbox_create(service, payload, context_payload, display=False)
        if handler == "sandbox_get":
            return _sandbox_get(service, payload)
        if handler == "sandbox_exec":
            return _sandbox_exec(service, payload, context_payload)
        if handler == "sandbox_files_read":
            return _sandbox_files_read(service, payload)
        if handler == "sandbox_files_apply_patch":
            return _sandbox_files_apply_patch(service, payload)
        if handler == "sandbox_port_expose":
            return _sandbox_port_expose(service, payload, context_payload)
        if handler == "sandbox_delete":
            return _sandbox_delete(service, payload)
        if handler in {"sandbox_start", "sandbox_stop", "sandbox_restart"}:
            return _sandbox_lifecycle(service, payload, action=handler.removeprefix("sandbox_"))
        if handler == "desktops_list":
            return ok({"desktops": _desktop_list(service)})
        if handler == "desktops_create":
            return _sandbox_create(service, payload, context_payload, display=True)
        if handler == "desktop_get":
            return _desktop_get(service, payload, context_payload)
        if handler == "desktop_delete":
            return _desktop_delete(service, payload, context_payload)
        if handler in {"desktop_start", "desktop_stop", "desktop_restart"}:
            return _desktop_lifecycle(service, payload, context_payload, action=handler.removeprefix("desktop_"))
        if handler == "desktop_frame":
            return _desktop_frame(service, payload, context_payload)
        if handler == "desktop_input":
            return _desktop_input(service, payload, context_payload)
        if handler == "desktop_ai_input":
            return _desktop_ai_input(service, payload, context_payload)
        if handler == "desktop_rules_update":
            return _desktop_rules_update(service, payload, context_payload)
        if handler == "desktop_access_request":
            return _desktop_access_request(service, payload, context_payload)
        if handler == "desktop_access_grant":
            return _desktop_access_grant(service, payload, context_payload)
        if handler == "desktop_exchange_issue":
            return _desktop_exchange_issue(service, payload, context_payload)
        if handler == "desktop_exchange_redeem":
            return _desktop_exchange_redeem(service, payload, context_payload)
        if handler == "desktop_grants_list":
            return _desktop_grants_list(service, payload, context_payload)
        if handler == "desktop_grant_revoke":
            return _desktop_grant_revoke(service, payload, context_payload)
        if handler == "desktop_grants_expire":
            return _desktop_grants_expire(service, context_payload)
        if handler == "desktop_control_acquire":
            return _desktop_control_acquire(service, payload, context_payload)
        if handler == "desktop_control_renew":
            return _desktop_control_renew(service, payload, context_payload)
        if handler == "desktop_control_release":
            return _desktop_control_release(service, payload, context_payload)
    except SandboxContractError as exc:
        return _api_error(exc.message, exc.code, exc.status_code, details=exc.details)
    return _api_error(f"Unknown sandbox API handler: {handler}", "UNKNOWN_SANDBOX_API_HANDLER", 400)


def _service() -> _SandboxApiService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = _SandboxApiService(start_lifecycle_sweeper=True)
        return _SERVICE


def _reset_service_for_tests(service: _SandboxApiService | None = None) -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None and _SERVICE is not service and hasattr(_SERVICE, "close"):
            _SERVICE.close()
        _SERVICE = service


def _operation_store(service: _SandboxApiService) -> RuntimeOperationStore:
    store = getattr(service, "operation_store", None)
    if isinstance(store, RuntimeOperationStore):
        return store
    store = RuntimeOperationStore()
    setattr(service, "operation_store", store)
    return store


def _operation_cancellations(service: _SandboxApiService) -> CancellationRegistry:
    registry = getattr(service, "operation_cancellations", None)
    if isinstance(registry, CancellationRegistry):
        return registry
    registry = CancellationRegistry()
    setattr(service, "operation_cancellations", registry)
    return registry


def _runtime_operation_response(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") == "error" and isinstance(result.get("error"), dict):
        return result
    return ok(result)


def _sweep_lifecycle(service: _SandboxApiService) -> list[dict[str, Any]]:
    results = service.manager.enforce_lifecycle()
    _cleanup_lifecycle_results(service, results)
    return results


def _cleanup_lifecycle_results(service: _SandboxApiService, results: list[dict[str, Any]]) -> None:
    for result in results:
        if result.get("ok") is not True:
            continue
        sandbox_id = str(result.get("sandbox_id") or result.get("seat_id") or "").strip()
        if not sandbox_id:
            continue
        if str(result.get("lifecycle_action") or "") in {"stop", "destroy"}:
            service.frame_cache.discard(sandbox_id)
            service.lease_manager.invalidate(sandbox_id)
            _desktop_exchange(service).revoke_seat(sandbox_id, reason="lifecycle_change")


class _RecordingProgressSink:
    def __init__(self, service: _SandboxApiService, *, provider_id: str, operation_id: str) -> None:
        self._service = service
        self._provider_id = provider_id
        self._operation_id = operation_id
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        current = _operation_store(self._service).get(self._operation_id)
        if isinstance(current, dict) and current.get("status") == "cancelled":
            raise SandboxContractError(
                "RUNTIME_OPERATION_CANCELLED",
                "Runtime operation was cancelled.",
                status_code=409,
            )
        normalized = ProgressEvent(
            operation_id=self._operation_id,
            stage=event.stage,
            message=event.message,
            percent=event.percent,
            details=event.details,
        )
        updated = _operation_store(self._service).append_progress(
            _progress_event_payload(normalized),
            provider_id=self._provider_id,
            updated_at=timestamp(),
        )
        if updated.get("status") == "cancelled":
            raise SandboxContractError(
                "RUNTIME_OPERATION_CANCELLED",
                "Runtime operation was cancelled.",
                status_code=409,
            )
        self.events.append(normalized)


def _runtime_providers(service: _SandboxApiService) -> dict[str, Any]:
    default_provider_id = _default_provider_id()
    provider_statuses = list(service.provider_registry.doctor_all())
    selected_provider_id = _selected_runtime_provider_id(provider_statuses, default_provider_id=default_provider_id)
    statuses = [
        _provider_payload(status, selected=status.provider_id == selected_provider_id)
        for status in provider_statuses
    ]
    if not any(provider.get("provider_id") == default_provider_id for provider in statuses):
        statuses.insert(0, _placeholder_provider(default_provider_id, selected=selected_provider_id == default_provider_id))
    statuses.sort(key=lambda provider: (provider.get("provider_id") != default_provider_id, str(provider.get("provider_id") or "")))
    return {
        "providers": statuses,
        "selected_provider_id": selected_provider_id,
        "default_provider_id": default_provider_id,
        "runtime_version": None,
        "guest_protocol": 1,
    }


def _runtime_doctor(service: _SandboxApiService) -> dict[str, Any]:
    providers_response = _runtime_providers(service)
    providers = providers_response["providers"]
    selected_provider_id = providers_response.get("selected_provider_id")
    selected = next((provider for provider in providers if provider.get("provider_id") == selected_provider_id), providers[0] if providers else {})
    ready = selected.get("ready") is True and _provider_payload_supports_desktop(selected)
    return {
        "status": "ready" if ready else "needs_setup",
        "providers": providers,
        "selected_provider_id": selected.get("provider_id"),
        "missing": selected.get("missing", []),
        "message": "Rumi Managed Runtime is ready."
        if ready
        else str(selected.get("message") or "Selected runtime provider needs setup before desktops can start."),
        "diagnostics": {
            "runtime_api": "available",
            "execution_provider": selected.get("status", "unknown"),
            "generated_at": timestamp(),
        },
        "generated_at": timestamp(),
    }


def _runtime_ensure(service: _SandboxApiService, payload: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(payload.get("provider_id") or _default_provider_id())
    requirements = RuntimeRequirements(provider_id=provider_id)
    try:
        provider = service.provider_registry.get(provider_id)
    except SandboxContractError:
        return _record_operation(service, _runtime_operation("failed", provider_id=provider_id))
    operation_id = _runtime_operation_id(payload, provider_id=provider_id, action="ensure")
    operation = _start_runtime_operation(
        service,
        operation_id=operation_id,
        provider_id=provider_id,
        action="ensure",
        worker=lambda sink: provider.ensure(
            EnsureRuntimeRequest(provider_id=provider_id, requirements=requirements),
            sink,
        ),
    )
    return operation


def _runtime_update(service: _SandboxApiService, payload: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(payload.get("provider_id") or _default_provider_id())
    try:
        provider = service.provider_registry.get(provider_id)
    except SandboxContractError:
        return _record_operation(service, _runtime_operation("failed", provider_id=provider_id, operation_id="managed-runtime-update"))
    operation_id = _runtime_operation_id(payload, provider_id=provider_id, action="update")
    operation = _start_runtime_operation(
        service,
        operation_id=operation_id,
        provider_id=provider_id,
        action="update",
        worker=lambda sink: provider.update(UpdateRuntimeRequest(provider_id=provider_id), sink),
    )
    return operation


def _runtime_uninstall(service: _SandboxApiService, payload: dict[str, Any]) -> dict[str, Any]:
    confirmation_error = _destructive_confirmation_error(payload, action="uninstall", resource="runtime")
    if confirmation_error is not None:
        return confirmation_error
    provider_id = str(payload.get("provider_id") or _default_provider_id())
    remove_state = bool(payload.get("remove_state"))
    try:
        provider = service.provider_registry.get(provider_id)
    except SandboxContractError:
        return _record_operation(service, _runtime_operation("failed", provider_id=provider_id, operation_id="managed-runtime-uninstall"))
    operation_id = _runtime_operation_id(payload, provider_id=provider_id, action="uninstall")

    def worker(sink: _RecordingProgressSink) -> OperationResult:
        result = provider.uninstall(
            UninstallRuntimeRequest(
                provider_id=provider_id,
                remove_state=remove_state,
            ),
            sink,
        )
        if result.ok:
            for seat_id in service.manager.mark_provider_uninstalled(provider_id, remove_state=remove_state):
                service.frame_cache.discard(seat_id)
                service.lease_manager.invalidate(seat_id)
        return result

    return _start_runtime_operation(
        service,
        operation_id=operation_id,
        provider_id=provider_id,
        action="uninstall",
        worker=worker,
    )


def _runtime_operation_get(service: _SandboxApiService, payload: dict[str, Any]):
    operation_id = str(payload.get("operation_id") or "").strip()
    operation = _operation_store(service).get(operation_id)
    if operation is None:
        return _api_error(f"Runtime operation not found: {operation_id}", "RUNTIME_OPERATION_NOT_FOUND", 404)
    return ok(operation)


def _runtime_operation_cancel(service: _SandboxApiService, payload: dict[str, Any]):
    operation_id = str(payload.get("operation_id") or "").strip()
    operation = _operation_store(service).cancel(operation_id, updated_at=timestamp())
    if operation is None:
        return _api_error(f"Runtime operation not found: {operation_id}", "RUNTIME_OPERATION_NOT_FOUND", 404)
    if operation.get("cancelled") is True:
        _operation_cancellations(service).cancel(operation_id)
    return ok(operation)


def _record_operation(service: _SandboxApiService, operation: dict[str, Any]) -> dict[str, Any]:
    return _operation_store(service).put(operation)


def _start_runtime_operation(
    service: _SandboxApiService,
    *,
    operation_id: str,
    provider_id: str,
    action: str,
    worker: Any,
) -> dict[str, Any]:
    operation, reserved = _operation_store(service).reserve_provider_operation({
        "operation_id": operation_id,
        "status": "running",
        "step": "queued",
        "message": f"Runtime {action} operation queued.",
        "progress": 0,
        "progress_events": [],
        "reboot_required": False,
        "provider_id": provider_id,
        "updated_at": timestamp(),
        "error": None,
    })
    if not reserved:
        return operation
    cancel_token = CancellationToken()
    _operation_cancellations(service).register(operation_id, cancel_token)

    def run_worker() -> None:
        sink = _RecordingProgressSink(service, provider_id=provider_id, operation_id=operation_id)
        try:
            with cancellation_context(cancel_token):
                result = worker(sink)
            _record_operation(
                service,
                _operation_payload(result, progress_events=sink.events, operation_id=operation_id),
            )
        except RuntimeOperationCancelled as exc:
            _record_operation(
                service,
                {
                    "operation_id": operation_id,
                    "status": "cancelled",
                    "cancelled": True,
                    "step": "cancelled",
                    "message": str(exc) or "Runtime operation was cancelled.",
                    "progress": 0,
                    "progress_events": [_progress_event_payload(event) for event in sink.events],
                    "reboot_required": False,
                    "provider_id": provider_id,
                    "updated_at": timestamp(),
                    "error": None,
                },
            )
        except SandboxContractError as exc:
            _record_operation(
                service,
                {
                    "operation_id": operation_id,
                    "status": "failed",
                    "step": "failed",
                    "message": exc.message,
                    "progress": 0,
                    "progress_events": [_progress_event_payload(event) for event in sink.events],
                    "reboot_required": False,
                    "provider_id": provider_id,
                    "updated_at": timestamp(),
                    "error": {"code": exc.code, "message": exc.message, "details": _jsonable(exc.details)},
                },
            )
        except Exception as exc:
            _record_operation(
                service,
                {
                    "operation_id": operation_id,
                    "status": "failed",
                    "step": "failed",
                    "message": f"Runtime {action} operation failed: {exc}",
                    "progress": 0,
                    "progress_events": [_progress_event_payload(event) for event in sink.events],
                    "reboot_required": False,
                    "provider_id": provider_id,
                    "updated_at": timestamp(),
                    "error": {"code": RUNTIME_NOT_READY, "message": str(exc)},
                },
            )
        finally:
            _operation_cancellations(service).unregister(operation_id, cancel_token)

    thread = threading.Thread(
        target=run_worker,
        name=f"rumi-runtime-{provider_id}-{action}",
        daemon=True,
    )
    thread.start()
    return operation


def _runtime_operation_id(payload: dict[str, Any], *, provider_id: str, action: str) -> str:
    raw_request_id = str(payload.get("request_id") or "").strip()
    if raw_request_id:
        safe = "".join(char if char.isalnum() or char in "._:-" else "-" for char in raw_request_id)
        safe = safe.strip(".:-_")
        if safe:
            return safe[:120]
    clean_provider = "".join(char if char.isalnum() or char in "._:-" else "-" for char in provider_id).strip(".:-_")
    return f"{clean_provider or 'runtime'}-{action}-{uuid.uuid4().hex[:12]}"


def _runtime_operation(status: str, *, provider_id: Any = None, operation_id: str = "managed-runtime-setup") -> dict[str, Any]:
    failed = status == "failed"
    message = "Managed runtime provider is not registered." if failed else "Runtime operation completed."
    return {
        "operation_id": operation_id,
        "status": status,
        "step": "provider_setup",
        "message": message,
        "progress": 0 if failed else 100,
        "reboot_required": False,
        "provider_id": str(provider_id or _default_provider_id()),
        "updated_at": timestamp(),
        "error": {
            "code": RUNTIME_NOT_READY,
            "message": message,
        } if failed else None,
    }


def _selected_runtime_provider_id(statuses: list[RuntimeProviderStatus], *, default_provider_id: str) -> str:
    default_status = next((status for status in statuses if status.provider_id == default_provider_id), None)
    if default_status is not None and _status_is_desktop_ready(default_status):
        return default_status.provider_id
    ready_status = next((status for status in statuses if _status_is_desktop_ready(status)), None)
    if ready_status is not None:
        return ready_status.provider_id
    if default_status is not None:
        return default_status.provider_id
    desktop_status = next((status for status in statuses if _status_supports_desktop(status)), None)
    if desktop_status is not None:
        return desktop_status.provider_id
    if default_provider_id:
        return default_provider_id
    first_status = statuses[0] if statuses else None
    return first_status.provider_id if first_status is not None else default_provider_id


def _status_supports_desktop(status: RuntimeProviderStatus) -> bool:
    return DESKTOP_RUNTIME_CAPABILITIES.issubset(status.capabilities)


def _status_is_desktop_ready(status: RuntimeProviderStatus) -> bool:
    return status.ready and _status_supports_desktop(status)


def _provider_payload_supports_desktop(provider: dict[str, Any]) -> bool:
    capabilities = list_or_empty(provider.get("capabilities"))
    return DESKTOP_RUNTIME_CAPABILITIES.issubset({str(capability) for capability in capabilities})


def _sandbox_create(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any], *, display: bool):
    resolution = mapping_or_empty(payload.get("resolution"))
    access = mapping_or_empty(payload.get("access"))
    rules = payload.get("rules")
    provisioning = payload.get("provisioning") if isinstance(payload.get("provisioning"), dict) else None
    starter = str(payload.get("starter") or "")
    created = service.manager.create(
        image="ubuntu:22.04",
        display=display,
        provider_id=str(payload.get("provider_id") or "auto"),
        name=str(payload.get("name") or ("Ubuntu Desktop" if display else "Sandbox")),
        template_id=str(
            payload.get("template_id")
            or _default_create_template_id(
                display=display,
                provider_id=payload.get("provider_id"),
                starter=payload.get("starter"),
            )
        ),
        width=_positive_int(resolution.get("width"), 1440),
        height=_positive_int(resolution.get("height"), 900),
        role=str(payload.get("role") or ""),
        rules=rules,
        access_mode=str(access.get("mode") or payload.get("access_mode") or "owner_only"),
        access_key=str(access.get("access_key") or payload.get("access_key") or ""),
        access_owner_id=_desktop_principal_id(context) if display else None,
        access_request_required=bool(access.get("request_required") or payload.get("access_request_required")),
        provisioning=provisioning,
        assigned_agent_id=str(payload.get("assigned_agent") or payload.get("assigned_agent_id") or ""),
        workspace_id=str(payload.get("workspace_id") or ""),
        workspace_access=str(payload.get("workspace_access") or ""),
        starter=starter,
        browser_url=str(payload.get("browser_url") or ""),
        network_approved=display
        and starter.strip().lower() in {"browser", "browser_url"}
        and _context_has_server_approval(context),
    )
    if created.get("ok") is not True:
        return _api_error(str(created.get("error") or "Sandbox create failed"), str(created.get("code") or RUNTIME_NOT_READY), int(created.get("status_code") or 503))
    status = service.manager.status(str(created["sandbox_id"]))
    data = _desktop_payload(service, status) if display else _sandbox_payload(status)
    if display and created.get("access_key"):
        data["access_key"] = created.get("access_key")
        data["access_key_hint"] = created.get("access_key_hint")
    return ok(data)


def _sandbox_get(service: _SandboxApiService, payload: dict[str, Any]):
    status = service.manager.status(str(payload.get("sandbox_id") or ""))
    if status.get("ok") is not True:
        return _api_error(str(status.get("error") or "Sandbox not found"), str(status.get("code") or "SANDBOX_NOT_FOUND"), int(status.get("status_code") or 404))
    return ok(_sandbox_payload(status))


def _sandbox_exec(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    sandbox_id = str(payload.get("sandbox_id") or "")
    result = service.manager.exec(
        sandbox_id,
        payload,
        approved_secret_ids=_approved_secret_ids_from_context(context),
    )
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Sandbox exec failed"), str(result.get("code") or "SANDBOX_EXEC_FAILED"), int(result.get("status_code") or 400), details=result.get("details"))
    return ok(result)


def _sandbox_files_read(service: _SandboxApiService, payload: dict[str, Any]):
    sandbox_id = str(payload.get("sandbox_id") or "")
    result = service.manager.read_file(sandbox_id, payload)
    if result.get("ok") is True:
        return ok(result)
    return _api_error(str(result.get("error") or "Sandbox file read failed"), str(result.get("code") or "SANDBOX_FILES_NOT_READY"), int(result.get("status_code") or 501), details=result.get("details"))


def _sandbox_files_apply_patch(service: _SandboxApiService, payload: dict[str, Any]):
    sandbox_id = str(payload.get("sandbox_id") or "")
    result = service.manager.apply_file_patch(sandbox_id, payload)
    if result.get("ok") is True:
        return ok(result)
    return _api_error(str(result.get("error") or "Sandbox file patch failed"), str(result.get("code") or "SANDBOX_FILES_NOT_READY"), int(result.get("status_code") or 501), details=result.get("details"))


def _sandbox_port_expose(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    sandbox_id = str(payload.get("sandbox_id") or "")
    result = service.manager.expose_port(sandbox_id, payload, approved=_context_has_server_approval(context))
    if result.get("ok") is True:
        return ok(result)
    return _api_error(str(result.get("error") or "Sandbox port exposure failed"), str(result.get("code") or "SANDBOX_PORTS_NOT_READY"), int(result.get("status_code") or 501), details=result.get("details"))


def _sandbox_delete(service: _SandboxApiService, payload: dict[str, Any]):
    sandbox_id = str(payload.get("sandbox_id") or "")
    confirmation_error = _destructive_confirmation_error(payload, action="delete", resource="sandbox")
    if confirmation_error is not None:
        return confirmation_error
    result = service.manager.destroy(sandbox_id)
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Sandbox delete failed"), str(result.get("code") or "SANDBOX_DELETE_FAILED"), int(result.get("status_code") or 400))
    service.frame_cache.discard(sandbox_id)
    service.lease_manager.invalidate(sandbox_id)
    return ok({"deleted": True, "sandbox_id": sandbox_id})


def _sandbox_lifecycle(service: _SandboxApiService, payload: dict[str, Any], *, action: str):
    sandbox_id = str(payload.get("sandbox_id") or "")
    if action == "stop":
        confirmation_error = _destructive_confirmation_error(payload, action="stop", resource="sandbox")
        if confirmation_error is not None:
            return confirmation_error
    if action == "start":
        result = service.manager.start(sandbox_id)
    elif action == "stop":
        result = service.manager.stop(sandbox_id)
    elif action == "restart":
        result = service.manager.restart(sandbox_id)
    else:
        return _api_error(f"Unknown sandbox lifecycle action: {action}", "SANDBOX_LIFECYCLE_UNKNOWN", 400)
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Sandbox lifecycle failed"), str(result.get("code") or "SANDBOX_LIFECYCLE_FAILED"), int(result.get("status_code") or 400))
    if action in {"stop", "restart"}:
        service.frame_cache.discard(sandbox_id)
        service.lease_manager.invalidate(sandbox_id)
    return ok(_sandbox_payload(result))


def _desktop_list(service: _SandboxApiService) -> list[dict[str, Any]]:
    desktops: list[dict[str, Any]] = []
    for item in service.manager.list_instances():
        if not isinstance(item, dict) or item.get("display") is not True:
            continue
        try:
            desktops.append(_desktop_payload(service, item))
        except Exception:
            desktops.append(_desktop_payload_error(item))
    return sorted(desktops, key=_desktop_list_sort_key)


def _desktop_list_sort_key(desktop: dict[str, Any]) -> tuple[int, float, str]:
    status = str(desktop.get("status") or "").strip().lower()
    if status == "running":
        status_rank = 0
    elif status in {"ready", "busy", "starting", "pending"}:
        status_rank = 1
    elif status in {"stopped", "paused"}:
        status_rank = 2
    elif status in {"destroyed", "deleted"}:
        status_rank = 4
    elif status == "failed":
        status_rank = 5
    else:
        status_rank = 3
    try:
        updated_rank = -float(desktop.get("updated_at") or desktop.get("created_at") or 0)
    except (TypeError, ValueError):
        updated_rank = 0.0
    return (status_rank, updated_rank, str(desktop.get("name") or desktop.get("seat_id") or ""))


def _desktop_get(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.read", payload, context)
    if access_error is not None:
        return access_error
    status = service.manager.status(seat_id)
    if status.get("ok") is not True:
        return _api_error(str(status.get("error") or "Desktop not found"), str(status.get("code") or "DESKTOP_NOT_FOUND"), int(status.get("status_code") or 404))
    return ok(_desktop_payload(service, status))


def _desktop_delete(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.delete", payload, context)
    if access_error is not None:
        return access_error
    confirmation_error = _destructive_confirmation_error(payload, action="delete", resource="desktop")
    if confirmation_error is not None:
        return confirmation_error
    result = service.manager.destroy(seat_id)
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Desktop delete failed"), str(result.get("code") or "DESKTOP_DELETE_FAILED"), int(result.get("status_code") or 400))
    service.frame_cache.discard(seat_id)
    service.lease_manager.invalidate(seat_id)
    _desktop_exchange(service).revoke_seat(seat_id, reason="desktop_destroyed")
    return ok({"deleted": True, "seat_id": seat_id})


def _desktop_lifecycle(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any], *, action: str):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, f"desktop.{action}", payload, context)
    if access_error is not None:
        return access_error
    if action == "stop":
        confirmation_error = _destructive_confirmation_error(payload, action="stop", resource="desktop")
        if confirmation_error is not None:
            return confirmation_error
    if action == "start":
        result = service.manager.start(seat_id)
    elif action == "stop":
        result = service.manager.stop(seat_id)
    elif action == "restart":
        result = service.manager.restart(seat_id)
    else:
        return _api_error(f"Unknown desktop lifecycle action: {action}", "DESKTOP_LIFECYCLE_UNKNOWN", 400)
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Desktop lifecycle failed"), str(result.get("code") or "DESKTOP_LIFECYCLE_FAILED"), int(result.get("status_code") or 400))
    if action in {"stop", "restart"}:
        service.frame_cache.discard(seat_id)
        service.lease_manager.invalidate(seat_id)
        _desktop_exchange(service).revoke_seat(seat_id, reason=f"desktop_{action}")
    return ok(_desktop_payload(service, result))


def _desktop_frame(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.frame", payload, context)
    if access_error is not None:
        return access_error
    after_seq = _optional_int(payload.get("after"))
    reservation = service.frame_cache.reserve_capture(seat_id)
    if reservation is not None:
        try:
            screenshot = service.manager.screenshot(seat_id)
            if screenshot.get("ok") is True:
                frame_data, content_type = _frame_bytes(screenshot)
                service.frame_cache.put_frame(
                    seat_id,
                    frame_data,
                    content_type=content_type,
                    width=_positive_int(screenshot.get("width"), 0),
                    height=_positive_int(screenshot.get("height"), 0),
                    source=str(screenshot.get("source") or screenshot.get("provider_id") or "managed_runtime"),
                )
        finally:
            service.frame_cache.release_capture(seat_id)

    try:
        fetched = service.frame_cache.get_frame(seat_id, after_seq=after_seq)
    except SandboxContractError as exc:
        return _api_error(exc.message, exc.code, exc.status_code)
    if fetched.not_modified:
        return {
            "_empty": True,
            "status_code": 204,
            "headers": {"X-Rumi-Frame-Seq": str(fetched.frame_seq)},
        }
    assert fetched.frame is not None
    frame = fetched.frame
    artifact = _persist_desktop_frame_artifact(frame)
    return {
        "_binary": True,
        "status_code": 200,
        "content_type": frame.content_type,
        "body": frame.data,
        "artifacts": [artifact],
        "artifact_paths": [artifact["path"]],
        "headers": {
            "Cache-Control": "no-store",
            "X-Rumi-Frame-Seq": str(frame.frame_seq),
            "X-Rumi-Frame-Width": str(frame.width),
            "X-Rumi-Frame-Height": str(frame.height),
            "X-Rumi-Captured-At": str(frame.captured_at),
            "X-Rumi-Artifact-Path": artifact["path"],
            "X-Rumi-Artifact-Id": artifact["artifact_id"],
        },
    }


def _desktop_input(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.input", payload, context)
    if access_error is not None:
        return access_error
    lease_token = payload.get("lease_token")
    service.lease_manager.validate_human_input(seat_id, str(lease_token) if lease_token is not None else None)
    result = service.manager.desktop_input(seat_id, payload, actor="human")
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Desktop input failed"), str(result.get("code") or "DESKTOP_INPUT_FAILED"), int(result.get("status_code") or 400))
    return ok(_desktop_input_payload(result, seat_id=seat_id, actor="human"))


def _desktop_ai_input(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.ai_input", payload, context)
    if access_error is not None:
        return access_error
    service.lease_manager.validate_ai_input(seat_id)
    agent_id = _agent_id(payload, context)
    if agent_id is None:
        return _api_error("AI desktop input requires a server-authenticated agent principal.", "DESKTOP_AGENT_PRINCIPAL_REQUIRED", 403)
    manager_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"agent_id", "actor_agent_id", "assigned_agent_id"}
    }
    result = service.manager.desktop_input(seat_id, manager_payload, actor="ai", authenticated_agent_id=agent_id)
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Desktop input failed"), str(result.get("code") or "DESKTOP_INPUT_FAILED"), int(result.get("status_code") or 400))
    return ok(_desktop_input_payload(result, seat_id=seat_id, actor="ai"))


def _desktop_rules_update(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.rules.update", payload, context)
    if access_error is not None:
        return access_error
    access = mapping_or_empty(payload.get("access"))
    changes_access_policy = bool(access) or any(
        key in payload for key in ("access_mode", "access_request_required")
    )
    if changes_access_policy:
        owner_result = service.manager.validate_desktop_access(
            seat_id,
            None,
            owner_id=_desktop_principal_id(context),
        )
        if owner_result.get("ok") is not True:
            return _api_error(
                str(owner_result.get("error") or "Desktop owner access is required"),
                str(owner_result.get("code") or "DESKTOP_OWNER_REQUIRED"),
                int(owner_result.get("status_code") or 403),
            )
    result = service.manager.update_desktop_rules(
        seat_id,
        role=payload.get("role"),
        rules=payload.get("rules"),
        access_mode=str(access.get("mode") or payload.get("access_mode") or "") or None,
        access_key=str(access.get("access_key") or payload.get("access_key") or "") or None,
        access_owner_id=_desktop_principal_id(context),
        access_request_required=access.get("request_required")
        if "request_required" in access
        else payload.get("access_request_required"),
    )
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Desktop rules update failed"), str(result.get("code") or "DESKTOP_RULES_UPDATE_FAILED"), int(result.get("status_code") or 400))
    _desktop_exchange(service).revoke_seat(seat_id, reason="desktop_policy_changed")
    data = _desktop_payload(service, result)
    if result.get("access_key"):
        data["access_key"] = result.get("access_key")
        data["access_key_hint"] = result.get("access_key_hint")
    return ok(data)


def _desktop_access_request(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    requester_id = _desktop_principal_id(context)
    if requester_id is None:
        return _api_error("Desktop access requests require a server-derived principal.", "DESKTOP_PRINCIPAL_REQUIRED", 403)
    result = service.manager.create_desktop_access_request(
        seat_id,
        requester_id=requester_id,
        reason=str(payload.get("reason") or ""),
    )
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Desktop access request failed"), str(result.get("code") or "DESKTOP_ACCESS_REQUEST_FAILED"), int(result.get("status_code") or 400))
    return ok({key: value for key, value in result.items() if key != "ok"})


def _desktop_access_grant(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    owner_id = _desktop_principal_id(context)
    if owner_id is None:
        return _api_error("Desktop access grants require a server-derived principal.", "DESKTOP_PRINCIPAL_REQUIRED", 403)
    result = service.manager.grant_desktop_access_request(
        seat_id,
        str(payload.get("request_id") or ""),
        owner_id=owner_id,
        approved=_truthy(payload.get("approved", True)),
    )
    if result.get("ok") is not True:
        return _api_error(str(result.get("error") or "Desktop access grant failed"), str(result.get("code") or "DESKTOP_ACCESS_GRANT_FAILED"), int(result.get("status_code") or 400))
    return ok({key: value for key, value in result.items() if key != "ok"})


def _desktop_exchange_issue(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "").strip()
    binding = _desktop_binding_context(context)
    if binding is None or not _owner_or_local_ui(service, seat_id, context):
        return _api_error("Only the authenticated desktop owner or local UI can issue exchange codes.", "DESKTOP_EXCHANGE_ISSUER_FORBIDDEN", 403)
    requested = payload.get("operations")
    operations = requested if isinstance(requested, list) else []
    try:
        result = _desktop_exchange(service).issue(
            **binding,
            seat_id=seat_id,
            operations=operations,
            code_ttl_seconds=int(payload.get("code_ttl_seconds") or 60),
            credential_ttl_seconds=int(payload.get("credential_ttl_seconds") or 300),
        )
    except (TypeError, ValueError) as exc:
        return _api_error(str(exc), "DESKTOP_EXCHANGE_ISSUE_INVALID", 400)
    return ok(result)


def _desktop_exchange_redeem(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    binding = _desktop_binding_context(context)
    if binding is None:
        return _api_error("Trusted origin and authenticated principal/device/session context are required.", "DESKTOP_EXCHANGE_CONTEXT_REQUIRED", 403)
    result = _desktop_exchange(service).exchange(str(payload.get("exchange_code") or ""), context=binding)
    if result.get("ok") is not True:
        return _api_error(str(result["error"]), str(result["code"]), int(result["status_code"]))
    return ok({key: value for key, value in result.items() if key != "ok"})


def _desktop_grants_list(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "").strip()
    if not _owner_or_local_ui(service, seat_id, context):
        return _api_error("Desktop grant metadata requires owner access.", "DESKTOP_GRANT_ADMIN_FORBIDDEN", 403)
    return ok({"grants": _desktop_exchange(service).list_metadata(seat_id=seat_id)})


def _desktop_grant_revoke(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "").strip()
    if not _owner_or_local_ui(service, seat_id, context):
        return _api_error("Desktop grant revocation requires owner access.", "DESKTOP_GRANT_ADMIN_FORBIDDEN", 403)
    identifier = str(payload.get("grant_id") or "")
    if not _desktop_exchange(service).revoke(identifier, seat_id=seat_id):
        return _api_error("Desktop grant was not found.", "DESKTOP_GRANT_NOT_FOUND", 404)
    return ok({"grant_id": identifier, "revoked": True})


def _desktop_grants_expire(service: _SandboxApiService, context: dict[str, Any]):
    if context.get("owner_pack") != "defaultspack" and context.get("source") != "defaultspack_local_ui":
        return _api_error("Grant expiry is restricted to the local service.", "DESKTOP_GRANT_ADMIN_FORBIDDEN", 403)
    return ok({"expired": _desktop_exchange(service).expire()})


def _desktop_control_acquire(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.control.acquire", payload, context)
    if access_error is not None:
        return access_error
    running_error = _desktop_running_error(service, seat_id)
    if running_error is not None:
        return running_error
    owner_id = _desktop_principal_id(context)
    if owner_id is None:
        return _api_error("Desktop control requires a server-derived principal.", "DESKTOP_PRINCIPAL_REQUIRED", 403)
    grant = service.lease_manager.acquire(seat_id, owner_id)
    response = grant.to_response()
    response["expires_at"] = _iso_timestamp(grant.expires_at)
    return ok(response)


def _desktop_control_renew(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.control.renew", payload, context)
    if access_error is not None:
        return access_error
    running_error = _desktop_running_error(service, seat_id)
    if running_error is not None:
        return running_error
    owner_id = _desktop_principal_id(context)
    if owner_id is None:
        return _api_error("Desktop control requires a server-derived principal.", "DESKTOP_PRINCIPAL_REQUIRED", 403)
    lease_token = str(payload.get("lease_token") or "")
    renewed = service.lease_manager.renew(seat_id, owner_id, lease_token)
    response = renewed.to_dict()
    response["acquired_at"] = _iso_timestamp(renewed.acquired_at)
    response["expires_at"] = _iso_timestamp(renewed.expires_at)
    return ok(response)


def _desktop_control_release(service: _SandboxApiService, payload: dict[str, Any], context: dict[str, Any]):
    seat_id = str(payload.get("seat_id") or "")
    access_error = _desktop_access_error(service, seat_id, "desktop.control.release", payload, context)
    if access_error is not None:
        return access_error
    owner_id = _desktop_principal_id(context)
    if owner_id is None:
        return _api_error("Desktop control requires a server-derived principal.", "DESKTOP_PRINCIPAL_REQUIRED", 403)
    lease_token = str(payload.get("lease_token") or "")
    released = service.lease_manager.release(seat_id, owner_id, lease_token)
    return ok({"released": released, "seat_id": seat_id})


def _provider_payload(status: RuntimeProviderStatus, *, selected: bool) -> dict[str, Any]:
    state = "ready" if status.ready else "needs_setup" if status.available else "unavailable"
    return {
        "provider_id": status.provider_id,
        "label": _provider_label(status.provider_id),
        "status": state,
        "available": status.available,
        "installed": status.installed,
        "ready": status.ready,
        "selected": selected,
        "managed": status.provider_id != "docker",
        "platform": status.platform,
        "version": status.version,
        "guest_protocol": 1,
        "capabilities": sorted(status.capabilities),
        "missing": [
            {
                "code": diagnostic.code,
                "severity": diagnostic.severity,
                "message": diagnostic.message,
                "detail": json.dumps(dict(diagnostic.details), ensure_ascii=False) if diagnostic.details else None,
            }
            for diagnostic in status.diagnostics
        ] + [
            {
                "code": str(item),
                "severity": "warning",
                "message": f"Missing runtime requirement: {item}",
            }
            for item in status.missing_requirements
        ],
        "isolation": _provider_isolation(status.provider_id, status.ready),
        "message": "Ready" if status.ready else (status.user_action or "Provider setup is required before desktops can start."),
    }


def _operation_payload(
    result: OperationResult,
    *,
    progress_events: list[ProgressEvent] | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    events = [_progress_event_payload(event) for event in progress_events or []]
    last_event = events[-1] if events else {}
    event_progress = last_event.get("percent")
    progress = int(float(event_progress)) if isinstance(event_progress, (int, float)) else (100 if result.ok else 0)
    message = str(last_event.get("message") or ("Runtime provider is ready." if result.ok else (result.user_action or "Runtime provider is not ready.")))
    return {
        "operation_id": operation_id or result.operation_id,
        "status": "completed" if result.ok else "failed",
        "step": str(last_event.get("stage") or result.status),
        "message": message,
        "progress": max(0, min(100, progress)),
        "progress_events": events,
        "reboot_required": result.reboot_required,
        "provider_id": result.provider_id,
        "updated_at": timestamp(),
        "error": None if result.ok else {
            "code": RUNTIME_NOT_READY,
            "message": result.user_action or "Managed runtime provider setup is not available.",
        },
    }


def _progress_event_payload(event: ProgressEvent) -> dict[str, Any]:
    details = dict(event.details) if isinstance(event.details, dict) else {}
    return {
        "operation_id": event.operation_id,
        "stage": event.stage,
        "message": event.message,
        "percent": event.percent,
        "details": _jsonable(details),
        "recorded_at": timestamp(),
    }


def _sandbox_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_id": item.get("sandbox_id"),
        "template_id": item.get("template_id"),
        "name": item.get("name"),
        "status": item.get("state") or item.get("status"),
        "state": item.get("state") or item.get("status"),
        "provider_id": item.get("provider_id"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _desktop_seat_id(item: dict[str, Any]) -> str:
    return str(item.get("sandbox_id") or item.get("seat_id") or "").strip()


def _desktop_id(item: dict[str, Any], *, seat_id: str) -> str:
    raw_id = item.get("id")
    if raw_id is not None:
        desktop_id = str(raw_id).strip()
        if desktop_id:
            return desktop_id
    return seat_id


def _desktop_payload(service: _SandboxApiService, item: dict[str, Any]) -> dict[str, Any]:
    seat_id = _desktop_seat_id(item)
    desktop_id = _desktop_id(item, seat_id=seat_id)
    desktop = mapping_or_empty(item.get("desktop_spec"))
    opaque = mapping_or_empty(item.get("provider_opaque_state"))
    metadata = mapping_or_empty(opaque.get("metadata"))
    startup = _desktop_startup_payload(opaque=opaque, metadata=metadata)
    startup_status = opaque.get("startup_status")
    if startup_status is None and isinstance(metadata.get("startup_status"), dict):
        startup_status = metadata.get("startup_status")
    state = str(item.get("state") or item.get("status") or "unknown")
    frame = service.frame_cache.last_metadata(seat_id)
    lease = service.lease_manager.active_lease(seat_id)
    access = mapping_or_empty(item.get("desktop_access"))
    rules = mapping_or_empty(item.get("desktop_rules"))
    provisioning = mapping_or_empty(item.get("desktop_provisioning"))
    workspace = mapping_or_empty(item.get("workspace_binding"))
    network = mapping_or_empty(item.get("network_policy"))
    network_mode = str(network.get("mode") or "off")
    return {
        "id": desktop_id,
        "seat_id": seat_id,
        "sandbox_id": seat_id,
        "name": item.get("name") or "Ubuntu Desktop",
        "status": "running" if state in RUNNING_STATES else state,
        "provider_id": item.get("provider_id"),
        "provider_label": _provider_label(str(item.get("provider_id") or "")),
        "template_id": item.get("template_id") or "desktop.ubuntu",
        "startup": startup,
        "desktop_spec": _desktop_spec_payload(desktop),
        "metadata": {
            "startup": startup,
            "startup_status": _jsonable(startup_status) if isinstance(startup_status, dict) else None,
        },
        "resolution": {
            "width": _positive_int(desktop.get("width"), 1440),
            "height": _positive_int(desktop.get("height"), 900),
        },
        "frame": None if frame is None else {
            "frame_seq": frame.get("frame_seq"),
            "width": frame.get("width"),
            "height": frame.get("height"),
            "mime_type": frame.get("content_type"),
            "captured_at": frame.get("captured_at"),
        },
        "assigned_agent": item.get("assigned_agent_id"),
        "control": {
            "holder": "human" if lease is not None else "none",
            "lease_expires_at": None if lease is None else _iso_timestamp(lease.expires_at),
        },
        "isolation": _provider_isolation(str(item.get("provider_id") or ""), state in RUNNING_STATES),
        "network_policy": {
            "summary": network_mode,
            "default": network_mode,
            "allowed": network.get("allowlist") or [],
            "approval_required": bool(network.get("approval_required")),
        },
        "workspace": {
            "workspace_id": workspace.get("workspace_id"),
            "label": workspace.get("workspace_id"),
            "access": workspace.get("mode") or "none",
        },
        "role": rules.get("role"),
        "rules": {
            "role": rules.get("role"),
            "instructions": rules.get("instructions") or "",
            "rule_ids": rules.get("rule_ids") or [],
        },
        "access_policy": {
            "mode": access.get("mode") or "owner_only",
            "owner_id": access.get("owner_id"),
            "key_required": bool(access.get("key_required")),
            "request_required": bool(access.get("request_required")),
            "key_hint": access.get("key_hint"),
            "link_enabled": bool(access.get("link_enabled")),
        },
        "provisioning": {
            "packages": provisioning.get("packages") or [],
            "apps": provisioning.get("apps") or [],
            "mcp_servers": provisioning.get("mcp_servers") or [],
            "status": provisioning.get("status") or "declared",
        },
        "last_error": item.get("last_error"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _desktop_startup_payload(*, opaque: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any] | None:
    startup = opaque.get("startup") if isinstance(opaque.get("startup"), dict) else None
    if startup is None and isinstance(metadata.get("startup"), dict):
        startup = metadata.get("startup")
    if not isinstance(startup, dict):
        return None
    payload: dict[str, Any] = {}
    starter = str(startup.get("starter") or "").strip()
    if starter:
        payload["starter"] = starter
    browser_url = str(startup.get("browser_url") or "").strip()
    if browser_url:
        payload["browser_url"] = browser_url
    return payload or None


def _desktop_spec_payload(desktop: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(desktop, dict) or not desktop:
        return None
    return {
        "enabled": bool(desktop.get("enabled")),
        "width": _positive_int(desktop.get("width"), 1440),
        "height": _positive_int(desktop.get("height"), 900),
        "display_backend": desktop.get("display_backend"),
        "preset": desktop.get("preset"),
    }


def _desktop_payload_error(item: dict[str, Any]) -> dict[str, Any]:
    seat_id = _desktop_seat_id(item)
    desktop_id = _desktop_id(item, seat_id=seat_id)
    provider_id = str(item.get("provider_id") or "")
    return {
        "id": desktop_id,
        "seat_id": seat_id,
        "sandbox_id": seat_id,
        "name": item.get("name") or "Desktop",
        "status": "failed",
        "provider_id": provider_id,
        "provider_label": _provider_label(provider_id),
        "template_id": item.get("template_id") or "desktop.ubuntu",
        "startup": None,
        "desktop_spec": None,
        "metadata": {"startup": None, "startup_status": None},
        "resolution": {"width": 1440, "height": 900},
        "frame": None,
        "assigned_agent": item.get("assigned_agent_id"),
        "control": {"holder": "none", "lease_expires_at": None},
        "isolation": _provider_isolation(provider_id, False),
        "network_policy": {"summary": "unknown", "default": "unknown", "allowed": [], "approval_required": False},
        "workspace": {"workspace_id": None, "label": None, "access": "none"},
        "role": None,
        "rules": {"role": None, "instructions": "", "rule_ids": []},
        "access_policy": {"mode": "owner_only", "owner_id": None, "key_required": False, "request_required": False, "key_hint": None, "link_enabled": False},
        "provisioning": {"packages": [], "apps": [], "mcp_servers": [], "status": "unknown"},
        "last_error": "Desktop state could not be serialized.",
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _desktop_input_payload(result: dict[str, Any], *, seat_id: str, actor: str) -> dict[str, Any]:
    rules = mapping_or_empty(result.get("desktop_rules"))
    return {
        "accepted": True,
        "seat_id": seat_id,
        "action": result.get("action"),
        "actor": actor,
        "agent_id": result.get("agent_id"),
        "assigned_agent": result.get("assigned_agent_id"),
        "role": rules.get("role"),
        "rules": {
            "role": rules.get("role"),
            "instructions": rules.get("instructions") or "",
            "rule_ids": rules.get("rule_ids") or [],
        },
    }


def _frame_bytes(screenshot: dict[str, Any]) -> tuple[bytes, str]:
    data = screenshot.get("data")
    if isinstance(data, bytes):
        return data, str(screenshot.get("content_type") or "image/png")
    data_url = str(screenshot.get("data_url") or "")
    if data_url.startswith("data:") and "," in data_url:
        header, payload = data_url.split(",", 1)
        content_type = header[5:].split(";", 1)[0] or "image/png"
        return base64.b64decode(payload), content_type
    raise SandboxContractError("FRAME_NOT_FOUND", "Desktop screenshot did not include frame bytes", status_code=404)


def _persist_desktop_frame_artifact(frame) -> dict[str, Any]:
    extension = _frame_extension(frame.content_type)
    captured_at = datetime.fromtimestamp(frame.captured_at, timezone.utc)
    stamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
    path = f"desktop_frames/{frame.seat_id}/{stamp}-seq{frame.frame_seq}.{extension}"
    artifact = ArtifactStore(_defaultspack_root()).create_binary(
        "desktop_frame",
        f"Desktop frame {frame.seat_id} #{frame.frame_seq}",
        frame.data,
        path=path,
        mime_type=frame.content_type,
        source_task="desktop_frame",
        metadata={
            "seat_id": frame.seat_id,
            "frame_seq": frame.frame_seq,
            "width": frame.width,
            "height": frame.height,
            "captured_at": frame.captured_at,
            "source": frame.source,
        },
    )
    return {
        "artifact_id": artifact["artifact_id"],
        "path": artifact["path"],
        "mime_type": artifact.get("mime_type") or frame.content_type,
        "size": artifact.get("size") or len(frame.data),
        "type": artifact.get("type") or "desktop_frame",
        "title": artifact.get("title") or "Desktop frame",
        "content_ref": artifact.get("content_ref"),
    }


def _frame_extension(content_type: str) -> str:
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized == "image/jpeg":
        return "jpg"
    if normalized == "image/webp":
        return "webp"
    return "png"


def _default_provider_id() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "mac_lima"
    if system == "windows":
        return "windows_wsl"
    return "linux_native"


def _default_create_template_id(*, display: bool, provider_id: Any = None, starter: Any = None) -> str:
    if not display:
        return "tool.ephemeral"
    clean_provider_id = str(provider_id or "auto").strip().lower()
    if clean_provider_id == "linux_native" or (
        clean_provider_id in {"", "auto"} and _default_provider_id() == "linux_native"
    ):
        return "desktop.linux_native"
    if str(starter or "").strip().lower() in {"browser", "browser_url"}:
        return "desktop.browser"
    return "desktop.ubuntu"


def _provider_label(provider_id: str) -> str:
    return {
        "linux_native": "Linux native Xvfb/Openbox",
        "windows_wsl": "RumiUbuntu WSL2",
        "mac_lima": "Rumi-managed Lima Ubuntu",
        "docker": "Docker-compatible runtime",
        "cloudflare_sandbox_bridge": "Cloudflare Sandbox Bridge",
    }.get(provider_id, provider_id or "Unknown provider")


def _provider_isolation(provider_id: str, ready: bool) -> dict[str, Any]:
    if provider_id == "linux_native":
        return {
            "mode": "native_x11" if ready else "native_pending",
            "vm": False,
            "container": False,
            "security_boundary": False,
            "host_process_namespace": True,
            "host_filesystem_shared": True,
            "host_network_shared": True,
            "summary": "Linux native provider uses a Rumi-owned Xvfb/Openbox display. It is not VM isolation.",
            "warnings": [
                "Linux native desktops share host namespaces and are not an untrusted pack boundary.",
                "Use Docker for container-level workspace, process, and network isolation.",
            ],
        }
    if provider_id == "docker":
        return {
            "mode": "container_optional",
            "container": True,
            "sandbox_workspace_shared": False,
            "sandbox_process_namespace_shared": False,
            "sandbox_network_namespace_shared": False,
            "sandbox_cgroup_scope": "docker_container",
            "sandbox_operation_binding": "container_id",
            "summary": "Optional provider only; Docker Desktop is never installed silently.",
        }
    if provider_id == "cloudflare_sandbox_bridge":
        return {
            "mode": "cloudflare_container" if ready else "cloudflare_pending",
            "container": True,
            "sandbox_workspace_shared": False,
            "sandbox_process_namespace_shared": False,
            "sandbox_network_namespace_shared": False,
            "sandbox_cgroup_scope": "cloudflare_container",
            "sandbox_operation_binding": "bridge_sandbox_id",
            "summary": "Runs supported argv/file operations in Cloudflare Containers through the Sandbox Bridge. PC-local desktop, terminal, browser, and workspace tools are not uploaded.",
        }
    if provider_id == "mac_lima":
        return {
            "mode": "lima_vm" if ready else "lima_pending",
            "vm": True,
            "container": False,
            "security_boundary": True,
            "separate_workdirs": True,
            "shared_guest_identity": True,
            "host_process_namespace": False,
            "host_filesystem_shared": False,
            "host_network_shared": False,
            "sandbox_workspace_shared": False,
            "sandbox_process_namespace_shared": False,
            "sandbox_network_namespace_shared": False,
            "sandbox_cgroup_scope": "guest_prlimit",
            "sandbox_operation_binding": "provider_instance_id",
            "process_cleanup": "pid_namespace",
            "untrusted_pack_boundary": True,
            "pack_isolation_boundary": "lima_vm_plus_guest_bubblewrap",
            "desktop_security_boundary": False,
            "summary": "macOS command and Pack execution runs in an attested Lima VZ virtual machine and a per-operation Bubblewrap user, PID, filesystem, and network namespace. Desktop GUI processes still share the managed guest identity.",
            "warnings": [
                "Desktop GUI processes share the managed guest identity and are not an untrusted application boundary.",
                "Guest prlimit enforces memory, process, and CPU-time ceilings; it is not a dedicated per-operation cgroup.",
            ],
        }
    if provider_id == "windows_wsl":
        return {
            "mode": "wsl2_vm" if ready else "wsl2_pending",
            "vm": True,
            "container": False,
            "security_boundary": False,
            "separate_workdirs": True,
            "shared_guest_identity": True,
            "host_process_namespace": False,
            "host_filesystem_shared": False,
            "host_network_shared": False,
            "sandbox_workspace_shared": True,
            "sandbox_process_namespace_shared": True,
            "sandbox_network_namespace_shared": True,
            "sandbox_cgroup_scope": "not_claimed",
            "sandbox_operation_binding": "provider_instance_id",
            "process_cleanup": "best_effort",
            "untrusted_pack_boundary": False,
            "pack_isolation_boundary": "docker_or_profiled_provider_required",
            "summary": "Windows provider uses one Rumi-owned WSL2 Ubuntu distribution as a convenience desktop/runtime. Instances get separate work directories, but they share the guest Unix identity and kernel namespaces; it is not a sandbox security boundary.",
            "warnings": [
                "Managed Ubuntu does not claim cross-sandbox filesystem isolation or read-only mount enforcement.",
                "Managed Ubuntu process cleanup is best effort after crashes or guest-side failures.",
                "Use Docker for container-level workspace, process, and network isolation.",
            ],
        }
    return {"mode": "unavailable", "summary": "Runtime isolation is unavailable until a provider is ready."}


def _placeholder_provider(provider_id: str, *, selected: bool) -> dict[str, Any]:
    missing_code = {
        "mac_lima": "lima_provider_unavailable",
        "windows_wsl": "wsl_provider_unavailable",
        "linux_native": "linux_native_provider_unavailable",
    }.get(provider_id, "runtime_provider_unavailable")
    return {
        "provider_id": provider_id,
        "label": _provider_label(provider_id),
        "status": "needs_setup",
        "available": False,
        "installed": False,
        "ready": False,
        "selected": selected,
        "managed": True,
        "platform": platform.system().lower() or "unknown",
        "version": None,
        "guest_protocol": 1,
        "capabilities": [],
        "missing": [{
            "code": missing_code,
            "severity": "warning",
            "message": "Runtime provider is registered but not available on this host.",
            "remediation": "Install the matching launcher first, or select an available provider for this platform.",
        }],
        "isolation": _provider_isolation(provider_id, False),
        "message": "Provider setup is required before desktops can start.",
    }


def _template_summaries() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for template in sandbox_template_catalog(_defaultspack_root()):
        template_id = str(template.get("id") or template.get("template_id") or "")
        if not template_id:
            continue
        policy = mapping_or_empty(template.get("policy"))
        runtime = mapping_or_empty(template.get("runtime"))
        provisioning = mapping_or_empty(template.get("provisioning"))
        filesystem = mapping_or_empty(policy.get("filesystem"))
        workspace = mapping_or_empty(filesystem.get("workspace"))
        network = mapping_or_empty(policy.get("network"))
        desktop = mapping_or_empty(policy.get("desktop"))
        summaries.append({
            "template_id": template_id,
            "name": template.get("display_name") or template_id,
            "description": template.get("summary") or "",
            "kind": template_id.split(".", 1)[0],
            "trust_level": template.get("trust_level") or "user",
            "source_pack_id": template.get("source_pack_id") or "rumi_sandbox_runtime_pack",
            "source_template_ids": template.get("source_template_ids") or [template_id],
            "default_provider_id": runtime.get("provider") or "auto",
            "provider_requirements": runtime.get("provider_requirements") or [],
            "capabilities": runtime.get("capabilities") or [],
            "network_policy": {
                "summary": str(network.get("mode") or "off"),
                "default": str(network.get("mode") or "off"),
                "allowed": network.get("allowlist") or [],
            },
            "workspace_access": {
                "summary": str(workspace.get("access") or "none"),
                "mode": str(workspace.get("access") or "none"),
            },
            "desktop": {
                "enabled": bool(desktop.get("enabled")),
                "starter": str(desktop.get("starter") or desktop.get("preset") or "empty"),
                "browser_url": desktop.get("browser_url") if isinstance(desktop.get("browser_url"), str) else None,
                "width": _positive_int(desktop.get("width"), 1440),
                "height": _positive_int(desktop.get("height"), 900),
            },
            "provisioning": {
                "packages": runtime.get("packages") or [],
                "apps": provisioning.get("apps") or [],
                "mcp_servers": provisioning.get("mcp_servers") or [],
                "default": bool(provisioning.get("default")),
            },
            "isolation": {
                "mode": "desktop" if desktop.get("enabled") else "sandbox",
                "vm": None,
                "container": None,
                "summary": "Runtime isolation is reported by the selected provider at creation time.",
            },
        })
    return summaries


def _defaultspack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _api_error(message: str, code: str, status_code: int = 400, *, details: Any | None = None) -> dict[str, Any]:
    response = error(message, code)
    response["_http_status"] = status_code
    if details is not None:
        response["error"]["details"] = _jsonable(details)
    return response


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(
            {field_info.name: getattr(value, field_info.name) for field_info in fields(value)}
        )
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _context_has_server_approval(context: dict[str, Any]) -> bool:
    return (
        internal_tool_decision_allows(context)
        or tool_server_approval_context_is_internal(context)
        or (isinstance(context, dict) and context.get("_tool_server_approved") is True)
    )


def _approved_secret_ids_from_context(context: dict[str, Any]) -> list[str]:
    if not isinstance(context, dict):
        return []
    grants: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in grants:
            grants.append(text[:128])

    def visit(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            for part in value.replace(",", "\n").splitlines():
                add(part)
            return
        if isinstance(value, dict):
            for key in ("secret_id", "id", "env_key", "name"):
                add(value.get(key))
            for key in ("secret_ids", "env_keys", "allowed_secret_ids", "allowed_env_keys", "grants"):
                visit(value.get(key))
            env = value.get("env")
            if isinstance(env, dict):
                for key in env:
                    add(key)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                visit(item)

    decision = internal_tool_decision(context) if internal_tool_decision_allows(context) else None
    if decision is not None:
        for key in ("secret_ids", "env_keys", "allowed_secret_ids", "allowed_env_keys", "sandbox_secret_grants"):
            visit(decision.get(key))
        resource = decision.get("resource")
        if isinstance(resource, dict):
            visit(resource)
    if tool_server_approval_context_is_internal(context):
        for key in ("_sandbox_secret_grants", "sandbox_secret_grants"):
            visit(context.get(key))
    return grants


def _destructive_confirmation_error(payload: dict[str, Any], *, action: str, resource: str) -> dict[str, Any] | None:
    if _truthy(payload.get("confirm_destructive")) or _truthy(payload.get("confirmed")):
        return None
    confirmation = str(payload.get("confirmation") or "").strip().lower()
    if confirmation in {action, f"{action}_{resource}", f"{resource}_{action}"}:
        return None
    return _api_error(
        f"{resource.title()} {action} requires explicit confirmation.",
        "DESTRUCTIVE_ACTION_CONFIRMATION_REQUIRED",
        409,
        details={"action": action, "resource": resource, "required": "confirm_destructive"},
    )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "confirmed"}


def _desktop_access_error(
    service: _SandboxApiService,
    seat_id: str,
    operation: str,
    payload: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any] | None:
    # Preserve the backend's owner/local authority path. Session credentials are
    # required only when that trusted principal does not already own the seat.
    owner_result: dict[str, Any] | None = None
    validate_owner = getattr(service.manager, "validate_desktop_access", None)
    if callable(validate_owner):
        owner_result = validate_owner(
            seat_id,
            None,
            owner_id=_desktop_principal_id(context),
        )
        if owner_result.get("ok") is True:
            return None
    binding = _desktop_binding_context(context)
    if binding is None:
        if owner_result is not None:
            return _api_error(
                str(owner_result.get("error") or "Desktop access denied"),
                str(owner_result.get("code") or "DESKTOP_ACCESS_DENIED"),
                int(owner_result.get("status_code") or 403),
            )
        return _api_error("Trusted desktop session context is required.", "DESKTOP_SESSION_CONTEXT_REQUIRED", 403)
    credential = _session_credential(payload)
    if not credential:
        return _api_error("A scoped desktop session credential is required.", "DESKTOP_SESSION_CREDENTIAL_REQUIRED", 403)
    result = _desktop_exchange(service).authorize(
        credential, seat_id=seat_id, operation=operation, context=binding
    )
    if result.get("ok") is True:
        return None
    return _api_error(
        str(result.get("error") or "Desktop access denied"),
        str(result.get("code") or "DESKTOP_ACCESS_DENIED"),
        int(result.get("status_code") or 403),
    )


def _desktop_running_error(service: _SandboxApiService, seat_id: str) -> dict[str, Any] | None:
    status = service.manager.status(seat_id)
    if status.get("ok") is not True:
        return _api_error(
            str(status.get("error") or "Desktop not found"),
            str(status.get("code") or "DESKTOP_NOT_FOUND"),
            int(status.get("status_code") or 404),
        )
    state = str(status.get("state") or status.get("status") or "unknown")
    if state in RUNNING_STATES:
        return None
    return _api_error(
        f"Desktop is not running ({state}): {seat_id}",
        "DESKTOP_NOT_RUNNING",
        409,
        details={"seat_id": seat_id, "state": state},
    )


def _session_credential(payload: dict[str, Any]) -> str | None:
    headers = mapping_or_empty(payload.get("_headers"))
    for value in (
        payload.get("desktop_session_credential"),
        headers.get("X-Rumi-Desktop-Session-Credential"),
        headers.get("x-rumi-desktop-session-credential"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return None


def _has_legacy_desktop_key(payload: dict[str, Any]) -> bool:
    access = mapping_or_empty(payload.get("access"))
    headers = mapping_or_empty(payload.get("_headers"))
    return any(value is not None for value in (
        payload.get("access_key"),
        access.get("access_key"),
        payload.get("desktop_access_key"),
        headers.get("X-Rumi-Desktop-Access-Key"),
        headers.get("x-rumi-desktop-access-key"),
    ))


def _desktop_exchange(service: _SandboxApiService) -> DesktopAccessExchange:
    exchange = getattr(service, "desktop_exchange", None)
    if not isinstance(exchange, DesktopAccessExchange):
        exchange = DesktopAccessExchange(service.manager.state_dir / "desktop_access_exchange.json")
        setattr(service, "desktop_exchange", exchange)
    return exchange


def _desktop_binding_context(context: dict[str, Any]) -> dict[str, str] | None:
    values = {
        "audience": context.get("trusted_audience") or context.get("audience"),
        "origin": context.get("trusted_origin"),
        "principal_id": context.get("authenticated_principal_id") or context.get("principal_id") or context.get("user_id"),
        "device_id": context.get("authenticated_device_id") or context.get("device_id"),
        "session_id": context.get("authenticated_session_id") or context.get("session_id"),
    }
    normalized = {key: str(value or "").strip()[:512] for key, value in values.items()}
    return normalized if all(normalized.values()) else None


def _owner_or_local_ui(service: _SandboxApiService, seat_id: str, context: dict[str, Any]) -> bool:
    if context.get("source") == "defaultspack_local_ui" or context.get("owner_pack") == "defaultspack":
        return True
    principal = _desktop_principal_id(context)
    if not principal:
        return False
    return service.manager.validate_desktop_access(seat_id, owner_id=principal).get("ok") is True


def _desktop_principal_id(context: dict[str, Any]) -> str | None:
    for value in (
        context.get("principal_id"),
        context.get("actor_id"),
        context.get("user_id"),
        context.get("session_id"),
        context.get("client_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text[:160]
    if context.get("flow_id") == "transport_direct" or context.get("owner_pack") == "defaultspack":
        return LOCAL_DESKTOP_PRINCIPAL_ID
    if context.get("source") == "defaultspack_local_ui":
        return LOCAL_DESKTOP_PRINCIPAL_ID
    return None


def _agent_id(payload: dict[str, Any], context: dict[str, Any]) -> str | None:
    del payload
    for value in (
        context.get("authenticated_agent_id"),
        context.get("agent_id"),
        context.get("actor_agent_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text[:160]
    return None


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
