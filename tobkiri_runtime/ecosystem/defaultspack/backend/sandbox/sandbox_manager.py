from __future__ import annotations

import json
import math
import os
import platform
import hashlib
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .errors import RUNTIME_PROVIDER_UNAVAILABLE, SandboxContractError
from .guest.protocol import DesktopInputRequest, GuestExecRequest
from .models import (
    DesktopAccessPolicy,
    DesktopProvisioningPlan,
    DesktopRuleConfig,
    DesktopSpec,
    FilesystemPolicy,
    LifecyclePolicy,
    NetworkPolicy,
    PackageSpec,
    ProviderInstance,
    ResolvedSandboxTemplate,
    ResourceLimits,
    SandboxCreateSpec,
    SandboxInstance,
    RuntimeRequirements,
    SecretsPolicy,
    WorkspaceBinding,
    model_to_dict,
)
from .provider_registry import ProviderRegistry
from .template_catalog import sandbox_template_by_id
from domain.tool.schema_adapter import list_or_empty, mapping_or_empty


REGISTRY_SCHEMA_VERSION = 5
CREATING = "creating"
PROVISIONING = "provisioning"
STARTING = "starting"
READY = "ready"
BUSY = "busy"
STOPPING = "stopping"
STOPPED = "stopped"
FAILED = "failed"
DESTROYING = "destroying"
DESTROYED = "destroyed"
LEGACY_PLACEHOLDER_PROVIDER = "legacy_placeholder"
VALID_STATES = {
    CREATING,
    PROVISIONING,
    STARTING,
    READY,
    BUSY,
    STOPPING,
    STOPPED,
    FAILED,
    DESTROYING,
    DESTROYED,
}
RUNNING_STATES = {READY, BUSY}
TERMINAL_STATES = {DESTROYED, FAILED}
SUPPORTED_MODEL_MODES = {"fast", "heavy"}
STATE_DIR_ENV = "RUMI_DEFAULTSPACK_SANDBOX_STATE_DIR"
DESKTOP_ACCESS_MODES = {"owner_only", "key_required", "request_required", "shared_link"}
DESKTOP_STARTERS = {"empty", "browser", "browser_url", "terminal"}
WORKSPACE_ACCESS_MODES = {"none", "read_only", "overlay"}
DESKTOP_MIN_WIDTH = 640
DESKTOP_MIN_HEIGHT = 480
DESKTOP_MAX_WIDTH = 3840
DESKTOP_MAX_HEIGHT = 2160
DESKTOP_MAX_PIXELS = 3840 * 2160
SANDBOX_MAX_CPU_COUNT = 32.0
SANDBOX_MAX_MEMORY_MB = 64 * 1024
SANDBOX_MAX_PIDS = 32768
SANDBOX_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
SANDBOX_MAX_TIMEOUT_MS = 24 * 60 * 60 * 1000
DESKTOP_INPUT_RATE_WINDOW_SECONDS = 5.0
DESKTOP_INPUT_RATE_MAX_EVENTS = 30
DESKTOP_CONTROL_AUDIT_FILENAME = "desktop_control_audit.jsonl"
GUEST_PROVISIONING_CAPABILITIES = frozenset({"sandbox.exec", "sandbox.files"})
SECRET_ENV_KEY_RE = re.compile(
    r"(^|_)(API_KEY|AUTH|COOKIE|CREDENTIAL|KEY|OAUTH|PASS|PASSWD|PASSWORD|PRIVATE_KEY|SECRET|SESSION|TOKEN)($|_)",
    re.IGNORECASE,
)


class SandboxManager:
    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        registry_path: str | Path | None = None,
        gui_backend: Any | None = None,
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else self._default_state_dir()
        self.registry_path = (
            Path(registry_path) if registry_path is not None else self.state_dir / "sandboxes.json"
        )
        # GUI backend injection is intentionally test-only until a managed provider
        # owns desktop capture/input in production.
        self._gui_backend = gui_backend
        self._provider_registry = provider_registry or ProviderRegistry()
        self._lock = threading.RLock()
        self._instances: Dict[str, SandboxInstance] = {}
        self._desktop_access_requests: dict[str, dict[str, Any]] = {}
        self.audit_path = self.state_dir / DESKTOP_CONTROL_AUDIT_FILENAME
        self._desktop_input_events: dict[tuple[str, str, str], list[float]] = {}
        self._model_mode: str = "fast"
        self._registry_signature: tuple[int, int] | None = None
        self._load_registry()

    def create(
        self,
        image: str = "ubuntu:22.04",
        display: bool = True,
        *,
        provider_id: str | None = None,
        name: str | None = None,
        template_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
        role: str | None = None,
        rules: Any | None = None,
        access_mode: str | None = None,
        access_key: str | None = None,
        access_owner_id: str | None = None,
        access_request_required: bool | None = None,
        provisioning: Any | None = None,
        assigned_agent_id: str | None = None,
        workspace_id: str | None = None,
        workspace_access: str | None = None,
        starter: str | None = None,
        browser_url: str | None = None,
        network_approved: bool = False,
    ) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        image = str(image or "").strip() or "ubuntu:22.04"
        display = bool(display)
        try:
            template = self._template_for_create(
                image=image,
                display=display,
                template_id=template_id,
                provider_id=provider_id,
                width=width,
                height=height,
                starter=starter,
                browser_url=browser_url,
            )
        except SandboxContractError as exc:
            return exc.to_dict()
        instance_name = str(name or f"Sandbox {image}").strip() or f"Sandbox {image}"
        requirements = RuntimeRequirements(
            template_id=template.template_id,
            required_capabilities=template.provider_requirements,
            provider_id=provider_id,
        )
        try:
            access_policy, access_key_hash, returned_access_key = _desktop_access_from_create(
                mode=access_mode,
                access_key=access_key,
                owner_id=access_owner_id,
                request_required=access_request_required,
                require_owner=display,
            )
        except SandboxContractError as exc:
            return exc.to_dict()
        if access_policy.key_required and not access_key_hash:
            return {
                "ok": False,
                "error": "Desktop access mode key_required requires an access_key.",
                "code": "DESKTOP_ACCESS_KEY_MISSING",
                "status_code": 400,
            }
        workspace_binding = _workspace_binding_from_create(
            workspace_id=workspace_id,
            workspace_access=workspace_access,
        )
        if isinstance(workspace_binding, dict):
            return workspace_binding
        rule_config = _desktop_rules_from_create(role=role, rules=rules)
        template_provisioning = _desktop_provisioning_from_create(
            _load_sandbox_template(template.template_id).get("provisioning"),
            default_packages=template.packages,
        )
        provisioning_plan = (
            _desktop_provisioning_from_create(provisioning, default_packages=template_provisioning.packages)
            if provisioning is not None
            else template_provisioning
        )
        provisioning_error = _desktop_provisioning_support_error(template, provisioning)
        if provisioning_error is not None:
            return provisioning_error
        startup = _desktop_startup_from_create(starter=starter, browser_url=browser_url, desktop=template.desktop)
        assigned_agent = _optional_clean_string(assigned_agent_id)
        provider = None
        provider_instance: ProviderInstance | None = None
        started: ProviderInstance | None = None
        try:
            provider = self._provider_registry.resolve(provider_id or "auto", requirements)
            provider_instance = provider.create(
                SandboxCreateSpec(
                    name=instance_name,
                    template=template,
                    provider_id=provider.provider_id,
                    workspace_binding=workspace_binding,
                    metadata={
                        "image": image,
                        "display": display,
                        "startup": startup,
                        "desktop_rules": model_to_dict(rule_config),
                        "desktop_provisioning": model_to_dict(provisioning_plan),
                        "assigned_agent_id": assigned_agent,
                        "network_approved": bool(network_approved),
                    },
                )
            )
            started = provider.start(provider_instance)
        except SandboxContractError as exc:
            if provider is not None and provider_instance is not None:
                self._destroy_provider_instance(provider, provider_instance)
            return exc.to_dict()
        except Exception as exc:
            if provider is not None and provider_instance is not None:
                self._destroy_provider_instance(provider, provider_instance)
            return {
                "ok": False,
                "error": f"Managed runtime provider failed to create sandbox: {exc}",
                "code": RUNTIME_PROVIDER_UNAVAILABLE,
                "status_code": 503,
            }

        assert provider is not None
        assert started is not None
        now = time.time()
        inst = SandboxInstance(
            sandbox_id=started.sandbox_id,
            name=instance_name,
            image=image,
            display=display,
            template_id=template.template_id,
            template_version=template.template_version,
            provider_id=started.provider_id,
            provider_instance_id=started.provider_instance_id,
            provider_opaque_state=dict(started.opaque_state),
            runtime_id=started.runtime_id,
            state=_canonical_state(started.state),
            created_at=now,
            updated_at=now,
            started_at=now if _canonical_state(started.state) in RUNNING_STATES else None,
            capabilities=template.allowed_operations or template.provider_requirements,
            resource_limits=template.resources,
            lifecycle_policy=template.lifecycle,
            filesystem_policy=template.filesystem,
            workspace_binding=workspace_binding,
            network_policy=template.network,
            secrets_policy=template.secrets,
            desktop_spec=template.desktop,
            desktop_rules=rule_config,
            desktop_access=access_policy,
            desktop_provisioning=provisioning_plan,
            assigned_agent_id=assigned_agent,
            generation=max(1, int(started.generation or 1)),
            desktop_access_key_hash=access_key_hash,
        )
        try:
            with self._lock:
                self._instances[inst.sandbox_id] = inst
                self._save_registry()
        except Exception as exc:
            with self._lock:
                self._instances.pop(inst.sandbox_id, None)
            self._destroy_provider_instance(provider, started)
            return {
                "ok": False,
                "error": f"Managed runtime provider sandbox was rolled back after registry save failed: {exc}",
                "code": "SANDBOX_REGISTRY_SAVE_FAILED",
                "status_code": 500,
            }
        result = {
            "ok": True,
            "created": True,
            "sandbox_id": inst.sandbox_id,
            "status": inst.state,
            "state": inst.state,
            "template_id": inst.template_id,
            "provider_id": inst.provider_id,
            "registry_path": str(self.registry_path),
            "desktop_access": model_to_dict(inst.desktop_access),
        }
        if returned_access_key:
            result["access_key"] = returned_access_key
            result["access_key_hint"] = _access_key_hint(returned_access_key)
        return result

    def destroy(self, sandbox_id: str) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None:
                return self._not_found(sandbox_id)
            if inst.state == DESTROYED:
                return {
                    "ok": True,
                    "destroyed": True,
                    "sandbox_id": inst.sandbox_id,
                    "status": inst.state,
                    "state": inst.state,
                }

        teardown_error = self._backend_teardown(inst)
        if teardown_error is not None:
            return self._mark_failed(inst.sandbox_id, teardown_error, code="SANDBOX_BACKEND_DESTROY_FAILED")

        provider_error = self._provider_destroy(inst)
        if provider_error is not None:
            return self._mark_failed(inst.sandbox_id, provider_error, code="SANDBOX_PROVIDER_DESTROY_FAILED")

        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None:
                return self._not_found(sandbox_id)
            requests_changed = self._drop_desktop_access_requests(inst.sandbox_id)
            if inst.state != DESTROYED:
                now = time.time()
                inst.state = DESTROYED
                inst.destroyed_at = now
                inst.stopped_at = now
                inst.updated_at = now
                inst.last_activity_at = now
                inst.last_error = None
                requests_changed = True
            if requests_changed:
                self._save_registry()
            return {
                "ok": True,
                "destroyed": True,
                "sandbox_id": inst.sandbox_id,
                "status": inst.state,
                "state": inst.state,
            }

    def start(self, sandbox_id: str) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None:
                return self._not_found(sandbox_id)
            if inst.state == DESTROYED:
                return self._not_running(inst, "destroyed")
            if inst.state in RUNNING_STATES:
                return {"ok": True, "started": False, **self._instance_to_dict(inst)}
            provider_instance = self._provider_instance(inst)

        try:
            provider = self._provider_registry.get(inst.provider_id)
            started = provider.start(provider_instance)
        except SandboxContractError as exc:
            return self._mark_failed(inst.sandbox_id, exc.message, code=exc.code)
        except Exception as exc:
            return self._mark_failed(
                inst.sandbox_id,
                f"Managed runtime provider start failed: {exc}",
                code="SANDBOX_PROVIDER_START_FAILED",
            )

        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None:
                return self._not_found(sandbox_id)
            self._apply_provider_instance(inst, started)
            now = time.time()
            inst.state = _canonical_state(started.state)
            inst.started_at = now if inst.state in RUNNING_STATES else inst.started_at
            inst.stopped_at = None if inst.state in RUNNING_STATES else inst.stopped_at
            inst.updated_at = now
            inst.last_activity_at = now
            inst.last_error = None
            self._save_registry()
            return {"ok": True, "started": inst.state in RUNNING_STATES, **self._instance_to_dict(inst)}

    def stop(self, sandbox_id: str, *, force: bool = False) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None:
                return self._not_found(sandbox_id)
            if inst.state == DESTROYED:
                return self._not_running(inst, "destroyed")
            if inst.state == STOPPED:
                return {"ok": True, "stopped": False, **self._instance_to_dict(inst)}
            provider_instance = self._provider_instance(inst)

        try:
            provider = self._provider_registry.get(inst.provider_id)
            provider.stop(provider_instance, force=force)
            reconciled = provider.reconcile(provider_instance).instance
        except SandboxContractError as exc:
            return self._mark_failed(inst.sandbox_id, exc.message, code=exc.code)
        except Exception as exc:
            return self._mark_failed(
                inst.sandbox_id,
                f"Managed runtime provider stop failed: {exc}",
                code="SANDBOX_PROVIDER_STOP_FAILED",
            )

        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None:
                return self._not_found(sandbox_id)
            self._apply_provider_instance(inst, reconciled)
            now = time.time()
            inst.state = STOPPED
            inst.stopped_at = now
            inst.updated_at = now
            inst.last_activity_at = now
            inst.last_error = None
            self._save_registry()
            return {"ok": True, "stopped": True, **self._instance_to_dict(inst)}

    def restart(self, sandbox_id: str) -> Dict[str, Any]:
        stopped = self.stop(sandbox_id, force=True)
        if stopped.get("ok") is not True:
            return stopped
        return self.start(sandbox_id)

    def screenshot(self, sandbox_id: str) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self._enforce_lifecycle_for_instance(sandbox_id)
        with self._lock:
            inst, error = self._ready_instance(sandbox_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "desktop.snapshot", "sandbox.snapshot")
            if operation_error is not None:
                return operation_error
            inst.touch()
            self._save_registry()

        backend_result = self._backend_screenshot(inst)
        if backend_result is not None:
            return backend_result

        provider_result = self._provider_screenshot(inst)
        if provider_result is not None:
            return provider_result

        return self._backend_unavailable(inst, "screenshot")

    def click(self, sandbox_id: str, x: int, y: int) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self._enforce_lifecycle_for_instance(sandbox_id)
        with self._lock:
            inst, error = self._ready_instance(sandbox_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "desktop.input.with_lease", "sandbox.desktop_input")
            if operation_error is not None:
                return operation_error
        result = self._backend_input_action(
            inst,
            "click",
            "clicked",
            {"x": x, "y": y},
        )
        if result.get("ok") is True:
            self._touch_ready_instance(inst.sandbox_id)
        return result

    def type_text(self, sandbox_id: str, text: str) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self._enforce_lifecycle_for_instance(sandbox_id)
        with self._lock:
            inst, error = self._ready_instance(sandbox_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "desktop.input.with_lease", "sandbox.desktop_input")
            if operation_error is not None:
                return operation_error
        result = self._backend_input_action(
            inst,
            "type_text",
            "typed",
            {"text": text},
        )
        if result.get("ok") is True:
            self._touch_ready_instance(inst.sandbox_id)
        return result

    def scroll(self, sandbox_id: str, direction: str = "down", amount: int = 3) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self._enforce_lifecycle_for_instance(sandbox_id)
        with self._lock:
            inst, error = self._ready_instance(sandbox_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "desktop.input.with_lease", "sandbox.desktop_input")
            if operation_error is not None:
                return operation_error
        result = self._backend_input_action(
            inst,
            "scroll",
            "scrolled",
            {"direction": direction, "amount": amount},
        )
        if result.get("ok") is True:
            self._touch_ready_instance(inst.sandbox_id)
        return result

    def desktop_input(
        self,
        seat_id: str,
        payload: Dict[str, Any],
        *,
        actor: str = "human",
        authenticated_agent_id: str | None = None,
    ) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self._enforce_lifecycle_for_instance(seat_id)
        normalized_actor = "ai" if actor == "ai" else "human"
        if normalized_actor == "ai":
            agent_id = _optional_clean_string(authenticated_agent_id, max_len=160)
        else:
            agent_id = _optional_clean_string(
                payload.get("agent_id") or payload.get("actor_agent_id") or payload.get("assigned_agent_id"),
                max_len=160,
            )
        action = str(payload.get("action") or "")
        client_action_id = _optional_clean_string(payload.get("client_action_id"), max_len=160)
        normalized_payload: dict[str, Any] | None = None
        with self._lock:
            inst, error = self._ready_instance(seat_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "desktop.input.with_lease", "sandbox.desktop_input")
            if operation_error is not None:
                return operation_error
            try:
                request = DesktopInputRequest.from_payload(
                    payload,
                    width=inst.desktop_spec.width if inst.desktop_spec is not None else None,
                    height=inst.desktop_spec.height if inst.desktop_spec is not None else None,
                    require_lease=normalized_actor != "ai",
                )
            except SandboxContractError as exc:
                self._append_desktop_audit_event(
                    inst,
                    actor=normalized_actor,
                    agent_id=agent_id,
                    action=action,
                    client_action_id=client_action_id,
                    ok=False,
                    code=exc.code,
                )
                return {
                    **exc.to_dict(),
                    "sandbox_id": inst.sandbox_id,
                    "seat_id": inst.sandbox_id,
                    "status": inst.state,
                    "state": inst.state,
                }
            action = request.action
            client_action_id = request.client_action_id
            normalized_payload = request.to_agent_payload()
            auth_error = self._authorize_desktop_actor(inst, normalized_actor, agent_id)
            if auth_error is not None:
                self._append_desktop_audit_event(
                    inst,
                    actor=normalized_actor,
                    agent_id=agent_id,
                    action=action,
                    client_action_id=client_action_id,
                    ok=False,
                    code=str(auth_error.get("code") or "DESKTOP_INPUT_DENIED"),
                )
                return auth_error
            rate_error = self._reserve_desktop_input_slot(inst, normalized_actor, agent_id)
            if rate_error is not None:
                self._append_desktop_audit_event(
                    inst,
                    actor=normalized_actor,
                    agent_id=agent_id,
                    action=action,
                    client_action_id=client_action_id,
                    ok=False,
                    code=str(rate_error.get("code") or "DESKTOP_INPUT_RATE_LIMITED"),
                )
                return rate_error
        try:
            agent = self._provider_agent(inst)
            result = agent.desktop_input(inst.sandbox_id, inst.sandbox_id, normalized_payload or {}, actor=normalized_actor)
        except SandboxContractError as exc:
            result = exc.to_dict()
        except Exception as exc:
            result = {
                "ok": False,
                "error": f"Desktop input failed: {exc}",
                "code": "SANDBOX_BACKEND_ACTION_FAILED",
                "status_code": 502,
            }
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "error": "Desktop input returned an invalid payload",
                "code": "SANDBOX_BACKEND_ACTION_FAILED",
                "status_code": 502,
            }
        result.setdefault("sandbox_id", inst.sandbox_id)
        result.setdefault("seat_id", inst.sandbox_id)
        result.setdefault("status", inst.state)
        result.setdefault("state", inst.state)
        result.setdefault("actor", normalized_actor)
        result.setdefault("desktop_rules", model_to_dict(inst.desktop_rules))
        result.setdefault("assigned_agent_id", inst.assigned_agent_id)
        if agent_id:
            result.setdefault("agent_id", agent_id)
        self._append_desktop_audit_event(
            inst,
            actor=normalized_actor,
            agent_id=agent_id,
            action=action,
            client_action_id=client_action_id,
            ok=result.get("ok") is True,
            code=None if result.get("ok") is True else str(result.get("code") or "DESKTOP_INPUT_FAILED"),
        )
        if result.get("ok") is True:
            self._touch_ready_instance(inst.sandbox_id)
        return result

    def exec(
        self,
        sandbox_id: str,
        payload: Dict[str, Any],
        *,
        approved_secret_ids: Any = None,
    ) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self._enforce_lifecycle_for_instance(sandbox_id)
        with self._lock:
            inst, error = self._ready_instance(sandbox_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "sandbox.exec.argv", "sandbox.exec")
            if operation_error is not None:
                return operation_error
        try:
            request = GuestExecRequest.from_payload(payload)
            resource_policy_error = self._require_exec_resource_policy(inst, request)
            if resource_policy_error is not None:
                return resource_policy_error
            secret_policy_error = self._require_secret_policy(
                inst,
                request,
                approved_secret_ids=approved_secret_ids,
            )
            if secret_policy_error is not None:
                return secret_policy_error
            agent = self._provider_agent(inst)
            result = agent.exec(inst.sandbox_id, request.to_agent_payload())
        except SandboxContractError as exc:
            result = exc.to_dict()
        except Exception as exc:
            result = {
                "ok": False,
                "error": f"Sandbox exec failed: {exc}",
                "code": "SANDBOX_EXEC_FAILED",
                "status_code": 502,
            }
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "error": "Sandbox exec returned an invalid payload",
                "code": "SANDBOX_EXEC_FAILED",
                "status_code": 502,
            }
        result = self._apply_exec_output_policy(inst, result)
        result.setdefault("sandbox_id", inst.sandbox_id)
        result.setdefault("status", inst.state)
        result.setdefault("state", inst.state)
        result.setdefault("provider_id", inst.provider_id)
        if result.get("ok") is True:
            self._touch_ready_instance(inst.sandbox_id)
        return result

    def apply_file_patch(self, sandbox_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self._enforce_lifecycle_for_instance(sandbox_id)
        with self._lock:
            inst, error = self._ready_instance(sandbox_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "sandbox.files.apply_patch")
            if operation_error is not None:
                return operation_error
            filesystem_error = self._require_filesystem_patch_policy(inst)
            if filesystem_error is not None:
                return filesystem_error
        try:
            agent = self._provider_agent(inst)
            apply_patch = getattr(agent, "apply_file_patch", None)
            if callable(apply_patch):
                result = apply_patch(inst.sandbox_id, payload)
            else:
                result = {
                    "ok": False,
                    "error": "Sandbox file patch is not available until a guest file service is implemented.",
                    "code": "SANDBOX_FILES_NOT_READY",
                    "status_code": 501,
                }
        except SandboxContractError as exc:
            result = exc.to_dict()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Sandbox file patch failed: {exc}",
                "code": "SANDBOX_FILES_FAILED",
                "status_code": 502,
                "sandbox_id": inst.sandbox_id,
                "status": inst.state,
                "state": inst.state,
                "provider_id": inst.provider_id,
            }
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "error": "Sandbox file patch returned an invalid payload",
                "code": "SANDBOX_FILES_FAILED",
                "status_code": 502,
            }
        result.setdefault("sandbox_id", inst.sandbox_id)
        result.setdefault("status", inst.state)
        result.setdefault("state", inst.state)
        result.setdefault("provider_id", inst.provider_id)
        if result.get("ok") is True:
            self._touch_ready_instance(inst.sandbox_id)
        return result

    def read_file(self, sandbox_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._enforce_lifecycle_for_instance(sandbox_id)
        with self._lock:
            inst, error = self._ready_instance(sandbox_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "sandbox.files.read", "sandbox.files")
            if operation_error is not None:
                return operation_error
        try:
            agent = self._provider_agent(inst)
            read_file = getattr(agent, "read_file", None)
            if callable(read_file):
                result = read_file(inst.sandbox_id, payload)
            else:
                result = {
                    "ok": False,
                    "error": "Sandbox file read is not available until a guest file service is implemented.",
                    "code": "SANDBOX_FILES_NOT_READY",
                    "status_code": 501,
                }
        except SandboxContractError as exc:
            result = exc.to_dict()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Sandbox file read failed: {exc}",
                "code": "SANDBOX_FILES_FAILED",
                "status_code": 502,
                "sandbox_id": inst.sandbox_id,
                "status": inst.state,
                "state": inst.state,
                "provider_id": inst.provider_id,
            }
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "error": "Sandbox file read returned an invalid payload",
                "code": "SANDBOX_FILES_FAILED",
                "status_code": 502,
            }
        result.setdefault("sandbox_id", inst.sandbox_id)
        result.setdefault("status", inst.state)
        result.setdefault("state", inst.state)
        result.setdefault("provider_id", inst.provider_id)
        if result.get("ok") is True:
            self._touch_ready_instance(inst.sandbox_id)
        return result

    def expose_port(self, sandbox_id: str, payload: Dict[str, Any], *, approved: bool = False) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self._enforce_lifecycle_for_instance(sandbox_id)
        with self._lock:
            inst, error = self._ready_instance(sandbox_id)
            if error is not None:
                return error
            assert inst is not None
            operation_error = self._require_operation(inst, "sandbox.port.expose", "sandbox.port_forward")
            if operation_error is not None:
                return operation_error
            network_error = self._require_network_policy(inst, approved=approved)
            if network_error is not None:
                return network_error
        try:
            agent = self._provider_agent(inst)
            expose_port = getattr(agent, "expose_port", None)
            if callable(expose_port):
                provider_payload = {**payload, "_network_policy_approved": True} if approved else payload
                result = expose_port(inst.sandbox_id, provider_payload)
            else:
                result = {
                    "ok": False,
                    "error": "Sandbox port exposure is not available until a guest port service is implemented.",
                    "code": "SANDBOX_PORTS_NOT_READY",
                    "status_code": 501,
                }
        except SandboxContractError as exc:
            result = exc.to_dict()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Sandbox port exposure failed: {exc}",
                "code": "SANDBOX_PORTS_FAILED",
                "status_code": 502,
                "sandbox_id": inst.sandbox_id,
                "status": inst.state,
                "state": inst.state,
                "provider_id": inst.provider_id,
            }
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "error": "Sandbox port exposure returned an invalid payload",
                "code": "SANDBOX_PORTS_FAILED",
                "status_code": 502,
            }
        result.setdefault("sandbox_id", inst.sandbox_id)
        result.setdefault("status", inst.state)
        result.setdefault("state", inst.state)
        result.setdefault("provider_id", inst.provider_id)
        if result.get("ok") is True:
            self._touch_ready_instance(inst.sandbox_id)
        return result

    def set_model_mode(self, mode: str) -> Dict[str, Any]:
        if mode not in SUPPORTED_MODEL_MODES:
            return {
                "ok": False,
                "error": f"Invalid mode: {mode}",
                "code": "INVALID_MODEL_MODE",
                "status_code": 400,
            }
        self._refresh_registry_if_changed()
        with self._lock:
            self._model_mode = mode
            self._save_registry()
        return {"ok": True, "mode": mode}

    def status(self, sandbox_id: str) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        self.enforce_lifecycle()
        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None:
                return self._not_found(sandbox_id)
            return {"ok": True, **self._instance_to_dict(inst)}

    def list_instances(self) -> List[Dict[str, Any]]:
        self._refresh_registry_if_changed()
        self.enforce_lifecycle()
        with self._lock:
            return [
                self._instance_to_dict(instance)
                for instance in self._instances.values()
                if isinstance(instance, SandboxInstance)
            ]

    def enforce_lifecycle(self, *, now: float | None = None) -> list[dict[str, Any]]:
        self._refresh_registry_if_changed()
        current_time = time.time() if now is None else float(now)
        actions: list[tuple[str, str]] = []
        with self._lock:
            for inst in self._instances.values():
                if not isinstance(inst, SandboxInstance):
                    continue
                action = self._lifecycle_action(inst, current_time)
                if action is not None:
                    actions.append((inst.sandbox_id, action))

        results: list[dict[str, Any]] = []
        for sandbox_id, action in actions:
            if action == "destroy":
                result = self.destroy(sandbox_id)
            else:
                result = self.stop(sandbox_id)
            result["lifecycle_action"] = action
            results.append(result)
        return results

    def _enforce_lifecycle_for_instance(self, sandbox_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
        current_time = time.time() if now is None else float(now)
        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None:
                return []
            action = self._lifecycle_action(inst, current_time)
        if action is None:
            return []
        result = self.destroy(sandbox_id) if action == "destroy" else self.stop(sandbox_id)
        result["lifecycle_action"] = action
        return [result]

    def mark_provider_uninstalled(self, provider_id: str, *, remove_state: bool = False) -> list[str]:
        clean_provider_id = str(provider_id or "").strip()
        affected: list[str] = []
        if not clean_provider_id:
            return affected
        self._refresh_registry_if_changed()
        with self._lock:
            now = time.time()
            for sandbox_id, inst in list(self._instances.items()):
                if inst.provider_id != clean_provider_id:
                    continue
                affected.append(inst.sandbox_id)
                if remove_state:
                    self._instances.pop(sandbox_id, None)
                    self._drop_desktop_access_requests(inst.sandbox_id)
                    continue
                if inst.state == DESTROYED:
                    continue
                inst.state = STOPPED
                inst.stopped_at = now
                inst.updated_at = now
                inst.last_activity_at = now
                inst.last_error = "Runtime provider was uninstalled; managed session is no longer running."
            if affected:
                self._save_registry()
        return affected

    def update_desktop_rules(
        self,
        seat_id: str,
        *,
        role: str | None = None,
        rules: Any | None = None,
        access_mode: str | None = None,
        access_key: str | None = None,
        access_owner_id: str | None = None,
        access_request_required: bool | None = None,
    ) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        with self._lock:
            inst = self._instances.get(str(seat_id))
            if inst is None:
                return self._not_found(seat_id)
            if not inst.display:
                return {
                    "ok": False,
                    "error": f"Sandbox is not a desktop seat: {seat_id}",
                    "code": "DESKTOP_RULES_NOT_SUPPORTED",
                    "status_code": 409,
                    "sandbox_id": str(seat_id),
                }
            if role is not None or rules is not None:
                inst.desktop_rules = _desktop_rules_from_create(
                    role=role if role is not None else inst.desktop_rules.role,
                    rules=rules if rules is not None else inst.desktop_rules.rule_ids,
                    instructions=inst.desktop_rules.instructions,
                )
            returned_access_key: str | None = None
            if access_mode is not None or access_key is not None or access_request_required is not None:
                previous_access = inst.desktop_access
                wants_key_required = str(access_mode or inst.desktop_access.mode or "").strip().lower() == "key_required"
                if wants_key_required and access_key is None and not inst.desktop_access_key_hash:
                    return {
                        "ok": False,
                        "error": "Desktop access mode key_required requires an access_key.",
                        "code": "DESKTOP_ACCESS_KEY_MISSING",
                        "status_code": 400,
                        "sandbox_id": str(seat_id),
                    }
                try:
                    access_policy, access_key_hash, returned_access_key = _desktop_access_from_create(
                        mode=access_mode or inst.desktop_access.mode,
                        access_key=access_key,
                        owner_id=access_owner_id or inst.desktop_access.owner_id,
                        request_required=access_request_required,
                        previous_key_hint=inst.desktop_access.key_hint,
                        previous_key_required=inst.desktop_access.key_required,
                        previous_link_enabled=inst.desktop_access.link_enabled,
                        previous_key_present=bool(inst.desktop_access_key_hash),
                        previous_owner_id=inst.desktop_access.owner_id,
                    )
                except SandboxContractError as exc:
                    return exc.to_dict()
                inst.desktop_access = access_policy
                if access_key is not None:
                    inst.desktop_access_key_hash = access_key_hash
                elif returned_access_key:
                    inst.desktop_access_key_hash = access_key_hash
                elif access_policy.mode not in {"key_required", "shared_link"}:
                    inst.desktop_access_key_hash = None
                if (
                    access_policy.mode != "request_required"
                    or not access_policy.request_required
                    or access_policy.owner_id != previous_access.owner_id
                ):
                    self._drop_desktop_access_requests(inst.sandbox_id)
            inst.touch()
            self._save_registry()
            result = {"ok": True, **self._instance_to_dict(inst)}
            if returned_access_key:
                result["access_key"] = returned_access_key
                result["access_key_hint"] = _access_key_hint(returned_access_key)
            return result

    def validate_desktop_access(
        self,
        seat_id: str,
        access_key: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        with self._lock:
            inst = self._instances.get(str(seat_id))
            if inst is None:
                return self._not_found(seat_id)
            policy = inst.desktop_access
            if policy.mode == "request_required" or policy.request_required:
                if owner_id and policy.owner_id and secrets.compare_digest(owner_id, policy.owner_id):
                    return {"ok": True, "sandbox_id": inst.sandbox_id}
                if self._approved_access_request_matches(inst.sandbox_id, access_key):
                    return {"ok": True, "sandbox_id": inst.sandbox_id}
                return {
                    "ok": False,
                    "error": "Desktop access requires an approved access request.",
                    "code": "DESKTOP_ACCESS_REQUEST_REQUIRED",
                    "status_code": 403,
                    "sandbox_id": inst.sandbox_id,
                }
            if policy.mode == "shared_link" or policy.link_enabled:
                if owner_id and policy.owner_id and secrets.compare_digest(owner_id, policy.owner_id):
                    return {"ok": True, "sandbox_id": inst.sandbox_id}
                if _verify_access_key(inst.desktop_access_key_hash, access_key):
                    return {"ok": True, "sandbox_id": inst.sandbox_id}
                return {
                    "ok": False,
                    "error": "Desktop shared link token is required.",
                    "code": "DESKTOP_SHARED_LINK_TOKEN_REQUIRED",
                    "status_code": 403,
                    "sandbox_id": inst.sandbox_id,
                    "key_hint": policy.key_hint,
                }
            if policy.mode == "owner_only" and not policy.key_required:
                if owner_id and policy.owner_id and secrets.compare_digest(owner_id, policy.owner_id):
                    return {"ok": True, "sandbox_id": inst.sandbox_id}
                return {
                    "ok": False,
                    "error": "Desktop owner identity is required.",
                    "code": "DESKTOP_OWNER_REQUIRED",
                    "status_code": 403,
                    "sandbox_id": inst.sandbox_id,
                }
            if policy.mode != "key_required" and not policy.key_required:
                return {"ok": True, "sandbox_id": inst.sandbox_id}
            if _verify_access_key(inst.desktop_access_key_hash, access_key):
                return {"ok": True, "sandbox_id": inst.sandbox_id}
            return {
                "ok": False,
                "error": "Desktop access key is required.",
                "code": "DESKTOP_ACCESS_KEY_REQUIRED",
                "status_code": 403,
                "sandbox_id": inst.sandbox_id,
                "key_hint": policy.key_hint,
            }

    def create_desktop_access_request(
        self,
        seat_id: str,
        *,
        requester_id: str | None = None,
        reason: str | None = None,
    ) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        with self._lock:
            inst = self._instances.get(str(seat_id))
            if inst is None:
                return self._not_found(seat_id)
            if not inst.display:
                return {
                    "ok": False,
                    "error": f"Sandbox is not a desktop seat: {seat_id}",
                    "code": "DESKTOP_ACCESS_REQUEST_NOT_SUPPORTED",
                    "status_code": 409,
                    "sandbox_id": str(seat_id),
                }
            policy = inst.desktop_access
            clean_requester = _optional_clean_string(requester_id, max_len=160)
            if clean_requester and policy.owner_id and secrets.compare_digest(clean_requester, policy.owner_id):
                return {
                    "ok": True,
                    "seat_id": inst.sandbox_id,
                    "request_id": "",
                    "status": "owner",
                    "approved": True,
                    "message": "Requester is already the desktop owner.",
                }
            if policy.mode != "request_required" and not policy.request_required:
                return {
                    "ok": False,
                    "error": "Desktop is not configured for request-required access.",
                    "code": "DESKTOP_ACCESS_REQUEST_NOT_REQUIRED",
                    "status_code": 409,
                    "sandbox_id": inst.sandbox_id,
                }
            now = time.time()
            request_id = f"dreq-{secrets.token_urlsafe(12)}"
            record = {
                "request_id": request_id,
                "seat_id": inst.sandbox_id,
                "requester_id": clean_requester,
                "reason": str(reason or "").strip()[:1000],
                "status": "pending",
                "requested_at": now,
                "updated_at": now,
                "owner_id": policy.owner_id,
                "access_key_hash": None,
                "access_key_hint": None,
            }
            self._desktop_access_requests[request_id] = record
            self._save_registry()
            return {"ok": True, **_public_access_request(record)}

    def grant_desktop_access_request(
        self,
        seat_id: str,
        request_id: str,
        *,
        owner_id: str | None = None,
        approved: bool = True,
    ) -> Dict[str, Any]:
        self._refresh_registry_if_changed()
        with self._lock:
            inst = self._instances.get(str(seat_id))
            if inst is None:
                return self._not_found(seat_id)
            policy = inst.desktop_access
            if not owner_id or not policy.owner_id or not secrets.compare_digest(owner_id, policy.owner_id):
                return {
                    "ok": False,
                    "error": "Only the desktop owner can grant access requests.",
                    "code": "DESKTOP_OWNER_REQUIRED",
                    "status_code": 403,
                    "sandbox_id": inst.sandbox_id,
                }
            record = self._desktop_access_requests.get(str(request_id))
            if not record or record.get("seat_id") != inst.sandbox_id:
                return {
                    "ok": False,
                    "error": f"Desktop access request not found: {request_id}",
                    "code": "DESKTOP_ACCESS_REQUEST_NOT_FOUND",
                    "status_code": 404,
                    "sandbox_id": inst.sandbox_id,
                    "request_id": str(request_id),
                }
            now = time.time()
            record["updated_at"] = now
            record["decided_at"] = now
            record["decided_by"] = owner_id
            if not approved:
                record["status"] = "denied"
                record["access_key_hash"] = None
                record["access_key_hint"] = None
                self._save_registry()
                return {"ok": True, **_public_access_request(record)}
            access_key = secrets.token_urlsafe(24)
            record["status"] = "approved"
            record["access_key_hash"] = _hash_access_key(access_key)
            record["access_key_hint"] = _access_key_hint(access_key)
            self._save_registry()
            return {"ok": True, **_public_access_request(record), "access_key": access_key}

    @staticmethod
    def _default_state_dir() -> Path:
        override = os.environ.get(STATE_DIR_ENV)
        if override:
            return Path(override).expanduser()

        xdg_state = os.environ.get("XDG_STATE_HOME")
        if xdg_state:
            return Path(xdg_state).expanduser() / "rumi" / "defaultspack" / "sandbox"

        system = platform.system().lower()
        home = Path.home()
        if system == "darwin":
            return home / "Library" / "Application Support" / "Rumi AI" / "defaultspack" / "sandbox"
        if system == "windows":
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                return Path(local_app_data) / "Rumi AI" / "defaultspack" / "sandbox"
        if home:
            return home / ".local" / "state" / "rumi" / "defaultspack" / "sandbox"
        return Path(tempfile.gettempdir()) / "rumi" / "defaultspack" / "sandbox"

    def _load_registry(self) -> None:
        with self._lock:
            if not self.registry_path.is_file():
                self._registry_signature = None
                return
            try:
                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                backup_path = self.registry_path.with_suffix(f".corrupt-{int(time.time())}.json")
                try:
                    self.registry_path.replace(backup_path)
                except OSError:
                    pass
                self._instances = {}
                self._model_mode = "fast"
                self._load_error = str(exc)
                self._registry_signature = self._registry_file_signature()
                return

            if isinstance(data, dict):
                raw_instances = data.get("instances", {})
                raw_access_requests = data.get("desktop_access_requests", {})
                mode = str(data.get("model_mode") or "fast")
                schema_version = int(_float_or_zero(data.get("schema_version") or 0))
                self._model_mode = mode if mode in SUPPORTED_MODEL_MODES else "fast"
            else:
                raw_instances = data
                raw_access_requests = {}
                schema_version = 0

            instances: Dict[str, SandboxInstance] = {}
            if isinstance(raw_instances, dict):
                iterable: list[Any] = list(raw_instances.values())
            elif isinstance(raw_instances, list):
                iterable = raw_instances
            else:
                iterable = []
            for raw in iterable:
                if not isinstance(raw, dict):
                    continue
                inst = self._instance_from_dict(raw, legacy=schema_version < REGISTRY_SCHEMA_VERSION)
                instances[inst.sandbox_id] = inst
            legacy_access_invalidated = "desktop_access_key_hash" in json.dumps(data)
            self._instances = instances
            self._desktop_access_requests = _access_requests_from_registry(raw_access_requests)
            for record in self._desktop_access_requests.values():
                if record.get("access_key_hash"):
                    record["access_key_hash"] = None
                    record["access_key_hint"] = None
                    record["status"] = "revoked"
            reconciled = self._reconcile_loaded_instances()
            if legacy_access_invalidated or reconciled:
                self._save_registry()
            else:
                self._registry_signature = self._registry_file_signature()

    def _save_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.registry_path.parent.chmod(0o700)
        except OSError:
            pass
        payload = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "model_mode": self._model_mode,
            "updated_at": time.time(),
            "desktop_access_requests": {
                request_id: _persisted_access_request(record)
                for request_id, record in sorted(self._desktop_access_requests.items())
            },
            "instances": {
                sandbox_id: self._instance_to_dict(inst)
                for sandbox_id, inst in sorted(self._instances.items())
            },
        }
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.registry_path)
        self._registry_signature = self._registry_file_signature()

    def _refresh_registry_if_changed(self) -> None:
        with self._lock:
            current = self._registry_file_signature()
            if current == self._registry_signature:
                return
            if current is None:
                return
            self._load_registry()

    def _registry_file_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.registry_path.stat()
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def _reconcile_loaded_instances(self) -> bool:
        changed = False
        now = time.time()
        for inst in list(self._instances.values()):
            if not isinstance(inst, SandboxInstance):
                continue
            if inst.state in TERMINAL_STATES or inst.provider_id == LEGACY_PLACEHOLDER_PROVIDER:
                continue
            try:
                provider = self._provider_registry.get(inst.provider_id)
                reconciled = provider.reconcile(self._provider_instance(inst)).instance
            except SandboxContractError as exc:
                if inst.state in RUNNING_STATES:
                    inst.state = STOPPED
                    inst.stopped_at = now
                    inst.updated_at = now
                    inst.last_activity_at = now
                    inst.last_error = f"Managed runtime provider unavailable during startup reconcile: {exc.message}"
                    changed = True
                continue
            except Exception as exc:
                if inst.state in RUNNING_STATES:
                    inst.state = STOPPED
                    inst.stopped_at = now
                    inst.updated_at = now
                    inst.last_activity_at = now
                    inst.last_error = f"Managed runtime startup reconcile failed: {exc}"
                    changed = True
                continue

            if not isinstance(reconciled, ProviderInstance):
                if inst.state in RUNNING_STATES:
                    inst.state = STOPPED
                    inst.stopped_at = now
                    inst.updated_at = now
                    inst.last_activity_at = now
                    inst.last_error = "Managed runtime provider returned no instance during startup reconcile."
                    changed = True
                continue

            reconciled_state = _canonical_state(reconciled.state)
            if (
                reconciled.provider_instance_id != inst.provider_instance_id
                or reconciled.runtime_id != inst.runtime_id
                or reconciled_state != inst.state
                or int(reconciled.generation or 0) != int(inst.generation or 0)
            ):
                was_running = inst.state in RUNNING_STATES
                inst.provider_instance_id = reconciled.provider_instance_id
                inst.runtime_id = reconciled.runtime_id
                inst.state = reconciled_state
                inst.generation = max(1, int(reconciled.generation or inst.generation or 1))
                inst.updated_at = now
                inst.last_activity_at = now
                if was_running and reconciled_state not in RUNNING_STATES:
                    inst.stopped_at = now
                    inst.last_error = "Managed runtime session was not found during startup reconcile."
                elif reconciled_state in RUNNING_STATES:
                    inst.last_error = None
                changed = True
        return changed

    @staticmethod
    def _lifecycle_action(inst: SandboxInstance, now: float) -> str | None:
        if inst.state not in RUNNING_STATES:
            return None
        ttl = inst.lifecycle_policy.ttl_seconds
        if ttl is None or ttl <= 0:
            return None
        idle_base = inst.last_activity_at or inst.started_at or inst.created_at
        if now - idle_base < ttl:
            return None
        return "destroy" if inst.lifecycle_policy.destroy_on_exit else "stop"

    @staticmethod
    def _authorize_desktop_actor(
        inst: SandboxInstance,
        actor: str,
        agent_id: str | None,
    ) -> Dict[str, Any] | None:
        if actor != "ai" or not inst.assigned_agent_id:
            return None
        if agent_id and secrets.compare_digest(agent_id, inst.assigned_agent_id):
            return None
        return {
            "ok": False,
            "error": "AI desktop input is limited to the assigned agent.",
            "code": "DESKTOP_AGENT_NOT_ASSIGNED",
            "status_code": 403,
            "sandbox_id": inst.sandbox_id,
            "seat_id": inst.sandbox_id,
            "assigned_agent_id": inst.assigned_agent_id,
            "agent_id": agent_id,
        }

    def _reserve_desktop_input_slot(
        self,
        inst: SandboxInstance,
        actor: str,
        agent_id: str | None,
    ) -> Dict[str, Any] | None:
        now = time.time()
        key = (inst.sandbox_id, actor, agent_id or actor)
        window_start = now - DESKTOP_INPUT_RATE_WINDOW_SECONDS
        recent = [stamp for stamp in self._desktop_input_events.get(key, []) if stamp >= window_start]
        if len(recent) >= DESKTOP_INPUT_RATE_MAX_EVENTS:
            self._desktop_input_events[key] = recent
            return {
                "ok": False,
                "error": "Desktop input rate limit exceeded.",
                "code": "DESKTOP_INPUT_RATE_LIMITED",
                "status_code": 429,
                "sandbox_id": inst.sandbox_id,
                "seat_id": inst.sandbox_id,
                "actor": actor,
                "agent_id": agent_id,
                "retry_after_seconds": DESKTOP_INPUT_RATE_WINDOW_SECONDS,
            }
        recent.append(now)
        self._desktop_input_events[key] = recent
        return None

    def _append_desktop_audit_event(
        self,
        inst: SandboxInstance,
        *,
        actor: str,
        agent_id: str | None,
        action: str,
        client_action_id: str | None,
        ok: bool,
        code: str | None,
    ) -> None:
        event = {
            "ts": time.time(),
            "sandbox_id": inst.sandbox_id,
            "seat_id": inst.sandbox_id,
            "provider_id": inst.provider_id,
            "template_id": inst.template_id,
            "actor": actor,
            "agent_id": agent_id,
            "assigned_agent_id": inst.assigned_agent_id,
            "action": action,
            "client_action_id": client_action_id,
            "ok": ok,
            "code": code,
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            try:
                self.audit_path.chmod(0o600)
            except OSError:
                pass
        except OSError:
            pass

    def read_desktop_audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines[-max(1, int(limit)):]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _ready_instance(
        self, sandbox_id: str
    ) -> tuple[Optional[SandboxInstance], Optional[Dict[str, Any]]]:
        inst = self._instances.get(str(sandbox_id))
        if inst is None:
            return None, self._not_found(sandbox_id)
        if inst.state in TERMINAL_STATES:
            return None, {
                "ok": False,
                "error": f"Sandbox is {inst.state}: {sandbox_id}",
                "code": "SANDBOX_NOT_RUNNING",
                "status_code": 409,
                "sandbox_id": str(sandbox_id),
                "status": inst.state,
                "state": inst.state,
            }
        if inst.state not in RUNNING_STATES:
            return None, {
                "ok": False,
                "error": f"Sandbox is not running ({inst.state}): {sandbox_id}",
                "code": "SANDBOX_NOT_RUNNING",
                "status_code": 409,
                "sandbox_id": str(sandbox_id),
                "status": inst.state,
                "state": inst.state,
            }
        return inst, None

    @staticmethod
    def _not_found(sandbox_id: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": f"Sandbox not found: {sandbox_id}",
            "code": "SANDBOX_NOT_FOUND",
            "status_code": 404,
            "sandbox_id": str(sandbox_id),
        }

    @staticmethod
    def _not_running(inst: SandboxInstance, reason: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": f"Sandbox is not runnable ({reason}): {inst.sandbox_id}",
            "code": "SANDBOX_NOT_RUNNING",
            "status_code": 409,
            "sandbox_id": inst.sandbox_id,
            "status": inst.state,
            "state": inst.state,
        }

    @staticmethod
    def _apply_provider_instance(inst: SandboxInstance, provider_instance: ProviderInstance) -> None:
        inst.provider_id = provider_instance.provider_id
        inst.provider_instance_id = provider_instance.provider_instance_id
        inst.provider_opaque_state = dict(provider_instance.opaque_state)
        inst.runtime_id = provider_instance.runtime_id
        inst.generation = max(1, int(provider_instance.generation or inst.generation or 1))

    @staticmethod
    def _require_operation(inst: SandboxInstance, *operations: str) -> Dict[str, Any] | None:
        allowed = set(inst.capabilities)
        if any(operation in allowed for operation in operations):
            return None
        return {
            "ok": False,
            "error": f"Sandbox template does not allow operation: {operations[0]}",
            "code": "SANDBOX_OPERATION_NOT_ALLOWED",
            "status_code": 403,
            "sandbox_id": inst.sandbox_id,
            "status": inst.state,
            "state": inst.state,
            "template_id": inst.template_id,
            "allowed_operations": sorted(allowed),
        }

    @staticmethod
    def _require_secret_policy(
        inst: SandboxInstance,
        request: GuestExecRequest,
        *,
        approved_secret_ids: Any = None,
    ) -> Dict[str, Any] | None:
        if not request.env:
            return None
        policy = inst.secrets_policy
        declared_secret_ids = set(policy.secret_ids)
        sensitive_keys = sorted(
            key
            for key in request.env
            if SECRET_ENV_KEY_RE.search(key) or key in declared_secret_ids
        )
        if not sensitive_keys:
            return None
        mode = str(policy.mode or "denied").strip().lower().replace("-", "_")
        base = {
            "ok": False,
            "sandbox_id": inst.sandbox_id,
            "status": inst.state,
            "state": inst.state,
            "template_id": inst.template_id,
            "denied_env_keys": sensitive_keys,
        }
        if mode in {"deny", "denied", "none", "off"}:
            return {
                **base,
                "error": "Sandbox template denies secret-bearing environment variables.",
                "code": "SANDBOX_SECRET_ENV_DENIED",
                "status_code": 403,
            }
        if policy.approval_required or mode in {"explicit", "explicit_read_only", "read_only"}:
            approved = _normalize_secret_grant_ids(approved_secret_ids)
            missing = [key for key in sensitive_keys if key not in approved]
            if not missing:
                return None
            return {
                **base,
                "denied_env_keys": missing,
                "approved_env_keys": sorted(key for key in sensitive_keys if key in approved),
                "error": "Sandbox template requires an approved secret grant before secret-bearing environment variables can be used.",
                "code": "SANDBOX_SECRET_ACCESS_REQUIRES_APPROVAL",
                "status_code": 409,
            }
        if mode in {"allow", "allowed", "explicit_approved"}:
            return None
        return {
            **base,
            "error": f"Unsupported sandbox secret policy mode: {policy.mode}",
            "code": "SANDBOX_SECRET_POLICY_UNSUPPORTED",
            "status_code": 403,
        }

    @staticmethod
    def _require_exec_resource_policy(inst: SandboxInstance, request: GuestExecRequest) -> Dict[str, Any] | None:
        timeout_ms = inst.resource_limits.timeout_ms
        if timeout_ms is None or timeout_ms <= 0 or request.timeout_ms <= timeout_ms:
            return None
        return {
            "ok": False,
            "error": "Sandbox exec timeout exceeds the template resource limit.",
            "code": "SANDBOX_RESOURCE_LIMIT_EXCEEDED",
            "status_code": 400,
            "sandbox_id": inst.sandbox_id,
            "status": inst.state,
            "state": inst.state,
            "template_id": inst.template_id,
            "requested_timeout_ms": request.timeout_ms,
            "max_timeout_ms": timeout_ms,
        }

    @staticmethod
    def _apply_exec_output_policy(inst: SandboxInstance, result: Dict[str, Any]) -> Dict[str, Any]:
        max_bytes = inst.resource_limits.output_bytes
        if max_bytes is None or max_bytes <= 0:
            return result
        bounded = dict(result)
        for field, flag in (("stdout", "stdout_truncated"), ("stderr", "stderr_truncated")):
            value = bounded.get(field)
            if not isinstance(value, str):
                continue
            clipped, truncated = _bounded_text_output(value, max_bytes)
            bounded[field] = clipped
            bounded[flag] = bool(bounded.get(flag)) or truncated
        return bounded

    @staticmethod
    def _require_network_policy(inst: SandboxInstance, *, approved: bool) -> Dict[str, Any] | None:
        policy = inst.network_policy
        mode = str(policy.mode or "off").strip().lower().replace("-", "_")
        base = {
            "ok": False,
            "sandbox_id": inst.sandbox_id,
            "status": inst.state,
            "state": inst.state,
            "template_id": inst.template_id,
            "network_policy": model_to_dict(policy),
        }
        if mode in {"off", "deny", "denied", "none"}:
            return {
                **base,
                "error": "Sandbox template denies network port exposure.",
                "code": "SANDBOX_NETWORK_DENIED",
                "status_code": 403,
            }
        if (policy.approval_required or policy.allowlist) and not approved:
            return {
                **base,
                "error": "Sandbox template requires an approved network grant before exposing ports.",
                "code": "SANDBOX_NETWORK_REQUIRES_APPROVAL",
                "status_code": 409,
            }
        return None

    @staticmethod
    def _require_filesystem_patch_policy(inst: SandboxInstance) -> Dict[str, Any] | None:
        filesystem_mode = str(inst.filesystem_policy.mode or "").strip().lower().replace("-", "_")
        workspace_mode = str(inst.workspace_binding.mode or inst.filesystem_policy.workspace_access or "none").strip().lower()
        if filesystem_mode in {"read_only", "readonly", "immutable"} or workspace_mode == "read_only":
            return {
                "ok": False,
                "error": "Sandbox file patch is blocked by a read-only filesystem or workspace policy.",
                "code": "SANDBOX_FILESYSTEM_READ_ONLY",
                "status_code": 403,
                "sandbox_id": inst.sandbox_id,
                "status": inst.state,
                "state": inst.state,
                "template_id": inst.template_id,
                "filesystem_policy": model_to_dict(inst.filesystem_policy),
                "workspace_binding": model_to_dict(inst.workspace_binding),
            }
        return None

    def _mark_failed(self, sandbox_id: str, message: str, *, code: str) -> Dict[str, Any]:
        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is not None and inst.state != DESTROYED:
                now = time.time()
                inst.state = FAILED
                inst.updated_at = now
                inst.last_activity_at = now
                inst.last_error = message
                self._save_registry()
        return {
            "ok": False,
            "destroyed": False,
            "sandbox_id": str(sandbox_id),
            "status": FAILED,
            "state": FAILED,
            "error": message,
            "code": code,
            "status_code": 502,
            "gui_backend": code == "SANDBOX_BACKEND_DESTROY_FAILED",
        }

    def _provider_destroy(self, inst: SandboxInstance) -> Optional[str]:
        if inst.provider_id == LEGACY_PLACEHOLDER_PROVIDER:
            return None
        try:
            provider = self._provider_registry.get(inst.provider_id)
            provider.destroy(self._provider_instance(inst))
        except SandboxContractError as exc:
            return exc.message
        except Exception as exc:
            return f"Managed runtime provider destroy failed: {exc}"
        return None

    @staticmethod
    def _destroy_provider_instance(provider: Any, instance: ProviderInstance) -> None:
        try:
            provider.destroy(instance)
        except Exception:
            pass

    def _provider_instance(self, inst: SandboxInstance) -> ProviderInstance:
        opaque_state = dict(inst.provider_opaque_state)
        opaque_state.setdefault("template_id", inst.template_id)
        opaque_state.setdefault("image", inst.image)
        opaque_state.setdefault("filesystem_policy", model_to_dict(inst.filesystem_policy))
        opaque_state.setdefault("workspace_binding", model_to_dict(inst.workspace_binding))
        opaque_state.setdefault("network_policy", model_to_dict(inst.network_policy))
        opaque_state.setdefault("secrets_policy", model_to_dict(inst.secrets_policy))
        resource_limits = model_to_dict(inst.resource_limits)
        opaque_state["resource_limits"] = resource_limits
        for key in ("memory_mb", "cpu_count", "pids", "output_bytes", "timeout_ms"):
            value = resource_limits.get(key)
            if value is None:
                opaque_state.pop(key, None)
            else:
                opaque_state[key] = value
        opaque_state.setdefault("desktop_provisioning", model_to_dict(inst.desktop_provisioning))
        opaque_state.setdefault("desktop_rules", model_to_dict(inst.desktop_rules))
        if inst.desktop_spec is not None:
            opaque_state["desktop_spec"] = model_to_dict(inst.desktop_spec)
            opaque_state["width"] = inst.desktop_spec.width
            opaque_state["height"] = inst.desktop_spec.height
        if inst.assigned_agent_id:
            opaque_state.setdefault("assigned_agent_id", inst.assigned_agent_id)
        return ProviderInstance(
            provider_id=inst.provider_id,
            provider_instance_id=inst.provider_instance_id,
            sandbox_id=inst.sandbox_id,
            runtime_id=inst.runtime_id,
            state=inst.state,
            opaque_state=opaque_state,
            generation=inst.generation,
        )

    def _backend_screenshot(self, inst: SandboxInstance) -> Optional[Dict[str, Any]]:
        if self._gui_backend is None or not hasattr(self._gui_backend, "screenshot"):
            return None
        try:
            result = self._gui_backend.screenshot(inst.sandbox_id)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"GUI backend screenshot failed: {exc}",
                "code": "SANDBOX_SCREENSHOT_FAILED",
                "status_code": 502,
                "sandbox_id": inst.sandbox_id,
                "status": inst.state,
                "state": inst.state,
            }
        if not isinstance(result, dict):
            return {
                "ok": False,
                "error": "GUI backend screenshot returned an invalid payload",
                "code": "SANDBOX_SCREENSHOT_FAILED",
                "status_code": 502,
                "sandbox_id": inst.sandbox_id,
                "status": inst.state,
                "state": inst.state,
            }
        result.setdefault("ok", True)
        result.setdefault("sandbox_id", inst.sandbox_id)
        result.setdefault("status", inst.state)
        result.setdefault("state", inst.state)
        result.setdefault("gui_backend", True)
        return result

    def _provider_screenshot(self, inst: SandboxInstance) -> Optional[Dict[str, Any]]:
        try:
            agent = self._provider_agent(inst)
        except SandboxContractError as exc:
            return exc.to_dict()
        except Exception as exc:
            return self._backend_action_failed(inst, "screenshot", f"Sandbox provider screenshot failed: {exc}")
        capture = getattr(agent, "capture_frame", None)
        if not callable(capture):
            return None
        try:
            result = capture(inst.sandbox_id, inst.sandbox_id)
        except SandboxContractError as exc:
            return exc.to_dict()
        except Exception as exc:
            return self._backend_action_failed(inst, "screenshot", f"Sandbox provider screenshot failed: {exc}")
        if not isinstance(result, dict):
            return self._backend_action_failed(inst, "screenshot", "Sandbox provider screenshot returned an invalid payload")
        normalized = dict(result)
        normalized.setdefault("ok", True)
        normalized.setdefault("sandbox_id", inst.sandbox_id)
        normalized.setdefault("seat_id", inst.sandbox_id)
        normalized.setdefault("status", inst.state)
        normalized.setdefault("state", inst.state)
        normalized.setdefault("gui_backend", False)
        normalized.setdefault("provider_id", inst.provider_id)
        normalized.setdefault("action", "screenshot")
        return normalized

    def _backend_teardown(self, inst: SandboxInstance) -> Optional[str]:
        backend = self._gui_backend
        if backend is None:
            return None
        for method_name in ("destroy_session", "teardown_session", "delete_session", "destroy", "teardown"):
            method = getattr(backend, method_name, None)
            if callable(method):
                break
        else:
            return None

        try:
            result = method(inst.sandbox_id)
        except Exception as exc:
            return f"GUI backend teardown failed: {exc}"
        if isinstance(result, dict):
            if result.get("ok", True) is not True:
                return str(result.get("error") or "GUI backend teardown did not complete")
            return None
        if result is False:
            return "GUI backend teardown did not complete"
        return None

    def _backend_input_action(
        self,
        inst: SandboxInstance,
        action: str,
        success_key: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        backend = self._gui_backend
        method = getattr(backend, action, None) if backend is not None else None
        if not callable(method):
            return self._backend_unavailable(inst, action)

        try:
            result = self._call_backend_input_method(method, inst.sandbox_id, action, payload)
        except Exception as exc:
            return self._backend_action_failed(
                inst,
                action,
                f"GUI backend {action} failed: {exc}",
            )

        if not isinstance(result, dict):
            return self._backend_action_failed(
                inst,
                action,
                f"GUI backend {action} returned an invalid payload",
            )

        normalized = dict(result)
        if normalized.get("ok") is not True:
            error = str(normalized.get("error") or f"GUI backend {action} did not execute")
            normalized["ok"] = False
            normalized.setdefault("error", error)
            normalized.setdefault("code", "SANDBOX_BACKEND_ACTION_FAILED")
            normalized.setdefault("status_code", 502)
            normalized.setdefault("sandbox_id", inst.sandbox_id)
            normalized.setdefault("status", inst.state)
            normalized.setdefault("state", inst.state)
            normalized.setdefault("gui_backend", True)
            normalized.setdefault("action", action)
            self._strip_input_success_flags(normalized)
            return normalized

        normalized["ok"] = True
        normalized.setdefault(success_key, True)
        normalized.setdefault("sandbox_id", inst.sandbox_id)
        normalized.setdefault("status", inst.state)
        normalized.setdefault("state", inst.state)
        normalized.setdefault("gui_backend", True)
        normalized.setdefault("action", action)
        for key, value in payload.items():
            normalized.setdefault(key, value)
        return normalized

    def _call_backend_input_method(
        self,
        method: Callable[..., Any],
        sandbox_id: str,
        action: str,
        payload: Dict[str, Any],
    ) -> Any:
        if action == "click":
            if self._accepts_keywords(method, "x", "y"):
                return method(sandbox_id, x=payload["x"], y=payload["y"])
            return method(sandbox_id, payload["x"], payload["y"])
        if action == "type_text":
            if self._accepts_keywords(method, "text"):
                return method(sandbox_id, text=payload["text"])
            return method(sandbox_id, payload["text"])
        if action == "scroll":
            if self._accepts_keywords(method, "direction", "amount"):
                return method(
                    sandbox_id,
                    direction=payload["direction"],
                    amount=payload["amount"],
                )
            if self._accepts_keywords(method, "amount"):
                return method(sandbox_id, amount=payload["amount"])
            return method(sandbox_id, payload["amount"])
        raise ValueError(f"Unsupported sandbox input action: {action}")

    @staticmethod
    def _accepts_keywords(method: Callable[..., Any], *names: str) -> bool:
        import inspect

        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return False
        parameters = signature.parameters.values()
        accepted = set()
        for parameter in parameters:
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            accepted.add(parameter.name)
        return all(name in accepted for name in names)

    def _backend_unavailable(self, inst: SandboxInstance, action: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": f"Sandbox backend unavailable for {action}",
            "code": "SANDBOX_BACKEND_UNAVAILABLE",
            "status_code": 503,
            "sandbox_id": inst.sandbox_id,
            "status": inst.state,
            "state": inst.state,
            "gui_backend": False,
            "action": action,
        }

    def _backend_action_failed(
        self,
        inst: SandboxInstance,
        action: str,
        error: str,
    ) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": error,
            "code": "SANDBOX_BACKEND_ACTION_FAILED",
            "status_code": 502,
            "sandbox_id": inst.sandbox_id,
            "status": inst.state,
            "state": inst.state,
            "gui_backend": True,
            "action": action,
        }

    @staticmethod
    def _strip_input_success_flags(result: Dict[str, Any]) -> None:
        for key in ("clicked", "typed", "scrolled", "recorded"):
            result.pop(key, None)

    def _touch_ready_instance(self, sandbox_id: str) -> None:
        with self._lock:
            inst = self._instances.get(str(sandbox_id))
            if inst is None or inst.state not in RUNNING_STATES:
                return
            inst.touch()
            self._save_registry()

    def _template_for_create(
        self,
        *,
        image: str,
        display: bool,
        template_id: str | None = None,
        provider_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
        starter: str | None = None,
        browser_url: str | None = None,
    ) -> ResolvedSandboxTemplate:
        requested_template_id = str(
            template_id
            or _default_template_id(display=display, provider_id=provider_id, starter=starter)
        ).strip()
        raw_template = _load_sandbox_template(requested_template_id)
        if not raw_template:
            raise SandboxContractError(
                "SANDBOX_TEMPLATE_NOT_FOUND",
                f"Unknown sandbox template: {requested_template_id}",
                status_code=400,
            )
        runtime = mapping_or_empty(raw_template.get("runtime"))
        policy = mapping_or_empty(raw_template.get("policy"))
        filesystem_policy = mapping_or_empty(policy.get("filesystem"))
        workspace_policy = mapping_or_empty(filesystem_policy.get("workspace"))
        network_policy = mapping_or_empty(policy.get("network"))
        secrets_policy = mapping_or_empty(policy.get("secrets"))
        resources_policy = mapping_or_empty(policy.get("resources"))
        lifecycle_policy = mapping_or_empty(policy.get("lifecycle"))
        provider_requirements = set(_clean_string_list(runtime.get("provider_requirements"), max_items=64, max_len=160))
        runtime_capabilities = set(_clean_string_list(runtime.get("capabilities"), max_items=64, max_len=160))
        if not provider_requirements:
            provider_requirements = {"sandbox.exec", "sandbox.files"}
        allowed_operations = set(_clean_string_list(raw_template.get("allowed_operations"), max_items=64, max_len=160))
        if not allowed_operations:
            allowed_operations = runtime_capabilities or provider_requirements
        desktop = None
        resolved_template_id = str(raw_template.get("id") or requested_template_id)
        packages = _packages_from_template_runtime(runtime)
        desktop_policy = mapping_or_empty(policy.get("desktop"))
        template_declares_desktop = bool(desktop_policy.get("enabled"))
        if display and not template_declares_desktop:
            raise SandboxContractError(
                "SANDBOX_TEMPLATE_NOT_DESKTOP",
                f"Sandbox template does not declare a desktop: {resolved_template_id}",
                status_code=400,
            )
        if not display and template_declares_desktop:
            raise SandboxContractError(
                "SANDBOX_TEMPLATE_KIND_MISMATCH",
                f"Desktop template cannot be created via sandbox endpoint: {resolved_template_id}",
                status_code=400,
            )
        if template_declares_desktop:
            resolved_width, resolved_height = _validated_desktop_resolution(
                width=width,
                height=height,
                default_width=int(_float_or_zero(desktop_policy.get("width")) or 1440),
                default_height=int(_float_or_zero(desktop_policy.get("height")) or 900),
            )
            startup = _desktop_startup_from_create(
                starter=starter,
                browser_url=browser_url,
                desktop=DesktopSpec(enabled=True),
            )
            preset = startup.get("starter") if str(starter or "").strip() else desktop_policy.get("starter") or desktop_policy.get("preset")
            desktop = DesktopSpec(
                enabled=True,
                width=resolved_width,
                height=resolved_height,
                display_backend=str(desktop_policy.get("display_backend") or "xvfb_openbox"),
                preset=str(preset or "") or None,
            )
        return ResolvedSandboxTemplate(
            template_id=resolved_template_id,
            template_version=str(raw_template.get("version") or "1"),
            runtime_os=str(runtime.get("os") or "linux"),
            provider_requirements=frozenset(provider_requirements),
            packages=packages,
            desktop=desktop,
            filesystem=FilesystemPolicy(
                mode=str(filesystem_policy.get("mode") or "ephemeral_overlay"),
                workspace_access=_template_workspace_access(workspace_policy.get("access")),
                host_writeback=bool(workspace_policy.get("writeback_requires_approval") is False),
            ),
            network=NetworkPolicy(
                mode=str(network_policy.get("mode") or "off"),
                allowlist=tuple(str(item) for item in (network_policy.get("allowlist") or []) if str(item)),
                approval_required=bool(network_policy.get("requires_approval")),
            ),
            secrets=SecretsPolicy(
                mode=str(secrets_policy.get("mode") or "denied"),
                secret_ids=tuple(_clean_string_list(secrets_policy.get("secret_ids"), max_items=128, max_len=128)),
                approval_required=bool(secrets_policy.get("requires_approval")),
            ),
            resources=_bounded_resource_limits(
                cpu_count=_optional_float(resources_policy.get("cpu")) or 1,
                memory_mb=_float_or_zero(resources_policy.get("memory_mb")) or 2048,
                pids=_float_or_zero(resources_policy.get("pids")),
                output_bytes=_float_or_zero(resources_policy.get("max_output_bytes")),
                timeout_ms=(_float_or_zero(resources_policy.get("timeout_seconds")) or 600) * 1000,
            ),
            lifecycle=LifecyclePolicy(
                ttl_seconds=int(_float_or_zero(lifecycle_policy.get("ttl_seconds")) or 900),
                persistent=str(lifecycle_policy.get("persistence") or "").lower() not in {"", "ephemeral"},
                destroy_on_exit=bool(lifecycle_policy.get("destroy_on_idle", True)),
            ),
            allowed_operations=frozenset(allowed_operations),
            source_template_ids=tuple(
                str(item)
                for item in list_or_empty(raw_template.get("source_template_ids"))
                or [raw_template.get("extends"), resolved_template_id, image]
                if item
            ),
        )

    def _instance_to_dict(self, inst: SandboxInstance) -> Dict[str, Any]:
        payload = model_to_dict(inst)
        payload["status"] = inst.state
        payload["state"] = inst.state
        return payload

    def _instance_from_dict(self, data: Dict[str, Any], *, legacy: bool = False) -> SandboxInstance:
        raw_state = data.get("state", data.get("status", READY))
        state = _canonical_state(raw_state)
        provider_id = str(data.get("provider_id") or "")
        provider_instance_id = str(data.get("provider_instance_id") or "")
        last_error = str(data.get("last_error")) if data.get("last_error") is not None else None
        stopped_at = _optional_float(data.get("stopped_at"))
        if legacy and state == READY and not provider_instance_id:
            state = STOPPED
            provider_id = LEGACY_PLACEHOLDER_PROVIDER
            stopped_at = _optional_float(data.get("updated_at")) or _optional_float(data.get("created_at"))
            last_error = "Migrated prototype sandbox; old fake-ready instances are not treated as live."
        display = bool(data.get("display", True))
        return SandboxInstance(
            sandbox_id=str(data.get("sandbox_id") or ""),
            name=str(data.get("name") or ""),
            image=str(data.get("image") or "ubuntu:22.04"),
            display=display,
            template_id=str(data.get("template_id") or ("desktop.ubuntu" if display else "tool.ephemeral")),
            template_version=str(data.get("template_version") or "compat"),
            provider_id=provider_id,
            provider_instance_id=provider_instance_id,
            provider_opaque_state=dict(data.get("provider_opaque_state") or {}),
            runtime_id=str(data.get("runtime_id") or ""),
            state=state,
            created_at=_float_or_now(data.get("created_at")),
            updated_at=_float_or_now(data.get("updated_at")),
            started_at=_optional_float(data.get("started_at")),
            stopped_at=stopped_at,
            destroyed_at=_optional_float(data.get("destroyed_at")),
            last_activity_at=_optional_float(data.get("last_activity_at")),
            last_error=last_error,
            capabilities=frozenset(_string_tuple(data.get("capabilities"))),
            resource_limits=_resource_limits_from_dict(data.get("resource_limits")),
            lifecycle_policy=_lifecycle_policy_from_dict(data.get("lifecycle_policy")),
            filesystem_policy=_filesystem_policy_from_dict(data.get("filesystem_policy")),
            workspace_binding=_workspace_binding_from_dict(data.get("workspace_binding")),
            network_policy=_network_policy_from_dict(data.get("network_policy")),
            secrets_policy=_secrets_policy_from_dict(data.get("secrets_policy")),
            desktop_spec=_desktop_spec_from_dict(data.get("desktop_spec"), display=display),
            desktop_rules=_desktop_rules_from_dict(data.get("desktop_rules")),
            desktop_access=_desktop_access_from_dict(data.get("desktop_access")),
            desktop_provisioning=_desktop_provisioning_from_dict(data.get("desktop_provisioning")),
            assigned_agent_id=_optional_clean_string(data.get("assigned_agent_id")),
            generation=max(1, int(_float_or_zero(data.get("generation") or 1))),
            recovery_token_hash=str(data.get("recovery_token_hash")) if data.get("recovery_token_hash") is not None else None,
            desktop_access_key_hash=None,
        )

    def _provider_agent(self, inst: SandboxInstance):
        provider = self._provider_registry.get(inst.provider_id)
        return provider.connect_agent(self._provider_instance(inst))

    def _approved_access_request_matches(self, seat_id: str, access_key: str | None) -> bool:
        if not access_key:
            return False
        for record in self._desktop_access_requests.values():
            if record.get("seat_id") != seat_id or record.get("status") != "approved":
                continue
            if _verify_access_key(str(record.get("access_key_hash") or ""), access_key):
                return True
        return False

    def _drop_desktop_access_requests(self, seat_id: str) -> bool:
        original_count = len(self._desktop_access_requests)
        self._desktop_access_requests = {
            request_id: record
            for request_id, record in self._desktop_access_requests.items()
            if record.get("seat_id") != seat_id
        }
        return len(self._desktop_access_requests) != original_count


def _canonical_state(value: Any) -> str:
    state = str(value or READY).strip().lower()
    if state == "error":
        return FAILED
    return state if state in VALID_STATES else FAILED


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _float_or_now(value: Any) -> float:
    parsed = _float_or_zero(value)
    return parsed or time.time()


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item) for item in value if str(item))


def _optional_clean_string(value: Any, *, max_len: int = 512) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_len]


def _clean_string_list(value: Any, *, max_items: int = 24, max_len: int = 512) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [line.strip() for line in value.replace(",", "\n").splitlines()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = [str(item).strip() for item in value]
    else:
        raw = []
    result: list[str] = []
    for item in raw:
        if not item:
            continue
        clipped = item[:max_len]
        if clipped not in result:
            result.append(clipped)
        if len(result) >= max_items:
            break
    return tuple(result)


def _normalize_secret_grant_ids(value: Any) -> set[str]:
    result: set[str] = set()

    def add(item: Any) -> None:
        text = _optional_clean_string(item, max_len=128)
        if text:
            result.add(text)

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            for part in _clean_string_list(item, max_items=128, max_len=128):
                add(part)
            return
        if isinstance(item, dict):
            for key in ("secret_id", "id", "env_key", "name"):
                add(item.get(key))
            for key in ("secret_ids", "env_keys", "allowed_secret_ids", "allowed_env_keys", "grants"):
                visit(item.get(key))
            env = item.get("env")
            if isinstance(env, dict):
                for key in env:
                    add(key)
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return result


def _default_template_id(*, display: bool, provider_id: str | None, starter: str | None = None) -> str:
    if not display:
        return "tool.ephemeral"
    clean_provider_id = str(provider_id or "auto").strip().lower()
    if clean_provider_id == "linux_native" or (
        clean_provider_id in {"", "auto"} and platform.system().lower() == "linux"
    ):
        return "desktop.linux_native"
    if str(starter or "").strip().lower() in {"browser", "browser_url"}:
        return "desktop.browser"
    return "desktop.ubuntu"


def _validated_desktop_resolution(
    *,
    width: int | None,
    height: int | None,
    default_width: int,
    default_height: int,
) -> tuple[int, int]:
    resolved_width = int(width or default_width or 1440)
    resolved_height = int(height or default_height or 900)
    if (
        resolved_width < DESKTOP_MIN_WIDTH
        or resolved_height < DESKTOP_MIN_HEIGHT
        or resolved_width > DESKTOP_MAX_WIDTH
        or resolved_height > DESKTOP_MAX_HEIGHT
        or resolved_width * resolved_height > DESKTOP_MAX_PIXELS
    ):
        raise SandboxContractError(
            "DESKTOP_RESOLUTION_LIMIT_EXCEEDED",
            (
                "Desktop resolution must be between "
                f"{DESKTOP_MIN_WIDTH}x{DESKTOP_MIN_HEIGHT} and "
                f"{DESKTOP_MAX_WIDTH}x{DESKTOP_MAX_HEIGHT}, with at most "
                f"{DESKTOP_MAX_PIXELS} pixels."
            ),
            status_code=400,
            details={
                "width": resolved_width,
                "height": resolved_height,
                "min_width": DESKTOP_MIN_WIDTH,
                "min_height": DESKTOP_MIN_HEIGHT,
                "max_width": DESKTOP_MAX_WIDTH,
                "max_height": DESKTOP_MAX_HEIGHT,
                "max_pixels": DESKTOP_MAX_PIXELS,
            },
        )
    return resolved_width, resolved_height


def _resource_limits_from_dict(value: Any) -> ResourceLimits:
    if not isinstance(value, dict):
        return ResourceLimits()
    return _bounded_resource_limits(
        cpu_count=value.get("cpu_count"),
        memory_mb=value.get("memory_mb"),
        pids=value.get("pids"),
        output_bytes=value.get("output_bytes"),
        timeout_ms=value.get("timeout_ms"),
    )


def _bounded_resource_limits(
    *,
    cpu_count: Any,
    memory_mb: Any,
    pids: Any,
    output_bytes: Any,
    timeout_ms: Any,
) -> ResourceLimits:
    return ResourceLimits(
        cpu_count=_bounded_optional_float(cpu_count, maximum=SANDBOX_MAX_CPU_COUNT),
        memory_mb=_bounded_optional_int(memory_mb, maximum=SANDBOX_MAX_MEMORY_MB),
        pids=_bounded_optional_int(pids, maximum=SANDBOX_MAX_PIDS),
        output_bytes=_bounded_optional_int(output_bytes, maximum=SANDBOX_MAX_OUTPUT_BYTES),
        timeout_ms=_bounded_optional_int(timeout_ms, maximum=SANDBOX_MAX_TIMEOUT_MS),
    )


def _bounded_optional_int(value: Any, *, maximum: int) -> int | None:
    parsed = _finite_float(value)
    if parsed is None or parsed <= 0:
        return None
    return min(int(parsed), maximum)


def _bounded_optional_float(value: Any, *, maximum: float) -> float | None:
    parsed = _finite_float(value)
    if parsed is None or parsed <= 0:
        return None
    return min(parsed, maximum)


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bounded_text_output(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="replace"), True


def _lifecycle_policy_from_dict(value: Any) -> LifecyclePolicy:
    if not isinstance(value, dict):
        return LifecyclePolicy()
    return LifecyclePolicy(
        ttl_seconds=int(_float_or_zero(value.get("ttl_seconds")) or 0) or None,
        persistent=bool(value.get("persistent")),
        destroy_on_exit=bool(value.get("destroy_on_exit", True)),
    )


def _filesystem_policy_from_dict(value: Any) -> FilesystemPolicy:
    if not isinstance(value, dict):
        return FilesystemPolicy()
    return FilesystemPolicy(
        mode=str(value.get("mode") or "ephemeral_overlay")[:80],
        workspace_access=str(value.get("workspace_access") or "none")[:80],
        workspace_paths=_clean_string_list(value.get("workspace_paths"), max_items=128, max_len=256),
        host_writeback=bool(value.get("host_writeback")),
    )


def _network_policy_from_dict(value: Any) -> NetworkPolicy:
    if not isinstance(value, dict):
        return NetworkPolicy()
    return NetworkPolicy(
        mode=str(value.get("mode") or "off")[:80],
        allowlist=_clean_string_list(value.get("allowlist"), max_items=128, max_len=256),
        approval_required=bool(value.get("approval_required")),
    )


def _secrets_policy_from_dict(value: Any) -> SecretsPolicy:
    if not isinstance(value, dict):
        return SecretsPolicy()
    return SecretsPolicy(
        mode=str(value.get("mode") or "denied")[:80],
        secret_ids=_clean_string_list(value.get("secret_ids"), max_items=128, max_len=128),
        approval_required=bool(value.get("approval_required")),
    )


def _desktop_spec_from_dict(value: Any, *, display: bool) -> DesktopSpec | None:
    if not display:
        return None
    if not isinstance(value, dict):
        return DesktopSpec(enabled=True)
    default_width = 1440
    default_height = 900
    try:
        width, height = _validated_desktop_resolution(
            width=int(_float_or_zero(value.get("width")) or default_width),
            height=int(_float_or_zero(value.get("height")) or default_height),
            default_width=default_width,
            default_height=default_height,
        )
    except SandboxContractError:
        width, height = default_width, default_height
    return DesktopSpec(
        enabled=bool(value.get("enabled", True)),
        width=width,
        height=height,
        display_backend=str(value.get("display_backend") or "x11")[:80],
        preset=_optional_clean_string(value.get("preset"), max_len=160),
    )


def _desktop_rules_from_create(
    *,
    role: Any = None,
    rules: Any = None,
    instructions: Any = None,
) -> DesktopRuleConfig:
    instruction_text = str(instructions or "").strip()
    if isinstance(rules, dict):
        role = rules.get("role", role)
        instruction_text = str(rules.get("instructions") or rules.get("prompt") or instruction_text).strip()
        rules = rules.get("rules") or rules.get("rule_ids") or []
    return DesktopRuleConfig(
        role=_optional_clean_string(role, max_len=160),
        instructions=instruction_text[:4000],
        rule_ids=_clean_string_list(rules, max_items=32, max_len=256),
    )


def _desktop_rules_from_dict(value: Any) -> DesktopRuleConfig:
    if not isinstance(value, dict):
        return DesktopRuleConfig()
    return _desktop_rules_from_create(
        role=value.get("role"),
        rules=value.get("rule_ids") or value.get("rules"),
        instructions=value.get("instructions"),
    )


def _desktop_startup_from_create(
    *,
    starter: Any = None,
    browser_url: Any = None,
    desktop: DesktopSpec | None = None,
) -> dict[str, Any]:
    if desktop is None or not desktop.enabled:
        return {}
    normalized_starter = str(starter or "").strip().lower()
    if not normalized_starter:
        normalized_starter = str(desktop.preset or "empty").strip().lower()
    if normalized_starter not in DESKTOP_STARTERS:
        raise SandboxContractError(
            "DESKTOP_STARTER_INVALID",
            "Desktop starter must be empty, browser, browser_url, or terminal.",
            status_code=400,
        )
    startup: dict[str, Any] = {"starter": normalized_starter}
    if normalized_starter == "browser_url":
        parsed_url = _validated_browser_url(browser_url)
        startup["browser_url"] = parsed_url
    return startup


def _validated_browser_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SandboxContractError(
            "DESKTOP_BROWSER_URL_INVALID",
            "Desktop browser_url starter requires an http or https URL.",
            status_code=400,
        )
    return url[:2048]


def _desktop_access_from_create(
    *,
    mode: str | None = None,
    access_key: str | None = None,
    owner_id: str | None = None,
    request_required: bool | None = None,
    require_owner: bool = True,
    previous_key_hint: str | None = None,
    previous_key_required: bool = False,
    previous_link_enabled: bool = False,
    previous_key_present: bool = False,
    previous_owner_id: str | None = None,
) -> tuple[DesktopAccessPolicy, str | None, str | None]:
    normalized_mode = str(mode or "owner_only").strip().lower()
    if normalized_mode == "request_required" or bool(request_required):
        normalized_mode = "request_required"
    if normalized_mode not in DESKTOP_ACCESS_MODES:
        normalized_mode = "owner_only"
    key_text = str(access_key or "")
    key_required = normalized_mode == "key_required" or (previous_key_required and normalized_mode == "key_required")
    link_enabled = normalized_mode == "shared_link"
    returned_access_key: str | None = None
    key_hash: str | None = None
    if key_required:
        key_hash = _hash_access_key(key_text) if key_text else None
        key_hint = _access_key_hint(key_text) if key_text else previous_key_hint
    elif link_enabled:
        if key_text:
            key_hash = _hash_access_key(key_text)
            key_hint = _access_key_hint(key_text)
        elif previous_link_enabled and previous_key_present:
            key_hint = previous_key_hint
        else:
            returned_access_key = secrets.token_urlsafe(24)
            key_hash = _hash_access_key(returned_access_key)
            key_hint = _access_key_hint(returned_access_key)
    else:
        key_hint = None
    clean_owner_id = _optional_clean_string(owner_id, max_len=160) or previous_owner_id
    if require_owner and normalized_mode in {"owner_only", "request_required"} and not clean_owner_id:
        raise SandboxContractError(
            "DESKTOP_OWNER_REQUIRED",
            "Desktop owner-bound access requires an owner_id.",
            status_code=400,
        )
    return (
        DesktopAccessPolicy(
            mode=normalized_mode,
            owner_id=clean_owner_id if normalized_mode in {"owner_only", "request_required", "shared_link"} else None,
            key_required=key_required,
            request_required=normalized_mode == "request_required",
            key_hint=key_hint,
            link_enabled=link_enabled,
        ),
        key_hash,
        returned_access_key,
    )


def _desktop_access_from_dict(value: Any) -> DesktopAccessPolicy:
    if not isinstance(value, dict):
        return DesktopAccessPolicy()
    mode = str(value.get("mode") or "owner_only")
    if mode in {"key_required", "shared_link"}:
        mode = "owner_only"
    if mode not in DESKTOP_ACCESS_MODES:
        mode = "owner_only"
    return DesktopAccessPolicy(
        mode=mode,
        owner_id=_optional_clean_string(value.get("owner_id"), max_len=160),
        key_required=bool(value.get("key_required")) and mode == "key_required",
        request_required=mode == "request_required" or bool(value.get("request_required")),
        key_hint=str(value.get("key_hint")) if value.get("key_hint") is not None else None,
        link_enabled=mode == "shared_link" or bool(value.get("link_enabled")),
    )


def _access_requests_from_registry(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, dict):
        iterable: list[Any] = list(value.values())
    elif isinstance(value, list):
        iterable = value
    else:
        iterable = []
    requests: dict[str, dict[str, Any]] = {}
    for raw in iterable:
        if not isinstance(raw, dict):
            continue
        record = _persisted_access_request(raw)
        request_id = str(record.get("request_id") or "")
        if request_id:
            requests[request_id] = record
    return requests


def _persisted_access_request(record: dict[str, Any]) -> dict[str, Any]:
    status = str(record.get("status") or "pending").strip().lower()
    if status not in {"pending", "approved", "denied"}:
        status = "pending"
    return {
        "request_id": str(record.get("request_id") or "")[:160],
        "seat_id": str(record.get("seat_id") or "")[:160],
        "requester_id": _optional_clean_string(record.get("requester_id"), max_len=160),
        "reason": str(record.get("reason") or "")[:1000],
        "status": status,
        "requested_at": _float_or_zero(record.get("requested_at")) or time.time(),
        "updated_at": _float_or_zero(record.get("updated_at")) or time.time(),
        "owner_id": _optional_clean_string(record.get("owner_id"), max_len=160),
        "decided_at": _optional_float(record.get("decided_at")),
        "decided_by": _optional_clean_string(record.get("decided_by"), max_len=160),
        "access_key_hash": str(record.get("access_key_hash") or "") or None,
        "access_key_hint": str(record.get("access_key_hint") or "") or None,
    }


def _public_access_request(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "seat_id": record.get("seat_id"),
        "request_id": record.get("request_id"),
        "requester_id": record.get("requester_id"),
        "reason": record.get("reason") or "",
        "status": record.get("status") or "pending",
        "requested_at": record.get("requested_at"),
        "updated_at": record.get("updated_at"),
        "decided_at": record.get("decided_at"),
        "decided_by": record.get("decided_by"),
        "access_key_hint": record.get("access_key_hint"),
    }


def _desktop_provisioning_from_create(
    value: Any,
    *,
    default_packages: tuple[PackageSpec, ...] = (),
) -> DesktopProvisioningPlan:
    if not isinstance(value, dict):
        return DesktopProvisioningPlan(packages=default_packages)
    packages = _packages_from_template_runtime({"packages": value.get("packages")})
    return DesktopProvisioningPlan(
        packages=packages or default_packages,
        apps=_clean_string_list(value.get("apps"), max_items=32, max_len=160),
        mcp_servers=_clean_string_list(value.get("mcp_servers") or value.get("tools"), max_items=32, max_len=160),
        status=str(value.get("status") or "declared")[:80],
    )


def _desktop_provisioning_support_error(
    template: ResolvedSandboxTemplate,
    provisioning: Any,
) -> dict[str, Any] | None:
    if not _provisioning_requests_guest_install(provisioning):
        return None
    if GUEST_PROVISIONING_CAPABILITIES.issubset(template.provider_requirements):
        return None
    return {
        "ok": False,
        "error": "Desktop app and MCP provisioning requires a guest runtime template with sandbox exec and file capabilities.",
        "code": "DESKTOP_PROVISIONING_UNSUPPORTED",
        "status_code": 400,
        "template_id": template.template_id,
        "required_capabilities": sorted(GUEST_PROVISIONING_CAPABILITIES),
        "provider_requirements": sorted(template.provider_requirements),
    }


def _provisioning_requests_guest_install(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("apps", "mcp_servers", "tools", "packages"):
        raw = value.get(key)
        if isinstance(raw, (str, bytes)):
            if str(raw).strip():
                return True
            continue
        if not isinstance(raw, (list, tuple, set)):
            continue
        for item in raw:
            if isinstance(item, dict):
                if str(item.get("name") or item.get("id") or "").strip():
                    return True
            elif str(item or "").strip():
                return True
    return False


def _desktop_provisioning_from_dict(value: Any) -> DesktopProvisioningPlan:
    if not isinstance(value, dict):
        return DesktopProvisioningPlan()
    return DesktopProvisioningPlan(
        packages=_packages_from_template_runtime({"packages": value.get("packages")}),
        apps=_clean_string_list(value.get("apps"), max_items=32, max_len=160),
        mcp_servers=_clean_string_list(value.get("mcp_servers"), max_items=32, max_len=160),
        status=str(value.get("status") or "declared")[:80],
    )


def _workspace_binding_from_create(
    *,
    workspace_id: str | None,
    workspace_access: str | None,
) -> WorkspaceBinding | dict[str, Any]:
    clean_workspace_id = _optional_clean_string(workspace_id, max_len=160)
    if not clean_workspace_id:
        return WorkspaceBinding(mode="none")
    mode = str(workspace_access or "read_only").strip().lower()
    if mode in {"read", "readonly"}:
        mode = "read_only"
    if mode in {"write", "read_write", "rw"}:
        return {
            "ok": False,
            "error": "Desktop workspace write access requires a backend policy and explicit approval.",
            "code": "DESKTOP_WORKSPACE_WRITE_REQUIRES_APPROVAL",
            "status_code": 403,
        }
    if mode not in WORKSPACE_ACCESS_MODES:
        mode = "read_only"
    if mode == "none":
        return WorkspaceBinding(workspace_id=clean_workspace_id, mode=mode, root=".")
    try:
        record = _trusted_workspace_record(clean_workspace_id)
    except SandboxContractError as exc:
        return exc.to_dict()
    return WorkspaceBinding(workspace_id=clean_workspace_id, mode=mode, root=str(record["root_path"]))


def _workspace_binding_from_dict(value: Any) -> WorkspaceBinding:
    if not isinstance(value, dict):
        return WorkspaceBinding()
    workspace_id = _optional_clean_string(value.get("workspace_id"), max_len=160)
    mode = str(value.get("mode") or "none").strip().lower()
    if mode not in WORKSPACE_ACCESS_MODES:
        mode = "none"
    return WorkspaceBinding(
        workspace_id=workspace_id,
        mode=mode if workspace_id else "none",
        root=str(value.get("root") or ".")[:512],
    )


def _trusted_workspace_record(workspace_id: str) -> dict[str, Any]:
    try:
        from ecosystem.defaultspack.domain.coding.workspace_store import WorkspaceStore, normalize_workspace_root
    except ModuleNotFoundError:
        from domain.coding.workspace_store import WorkspaceStore, normalize_workspace_root  # type: ignore

    store = WorkspaceStore()
    record = store.get(workspace_id)
    if record is None:
        raise SandboxContractError(
            "SANDBOX_WORKSPACE_NOT_FOUND",
            f"Sandbox workspace is not registered: {workspace_id}",
            status_code=404,
        )
    if not bool(record.get("trusted")):
        raise SandboxContractError(
            "SANDBOX_WORKSPACE_UNTRUSTED",
            f"Sandbox workspace must be trusted before it can be bound: {workspace_id}",
            status_code=403,
        )
    try:
        root_value = record.get("root_path")
        if not isinstance(root_value, (str, Path)):
            raise ValueError("workspace root is missing")
        root_path = normalize_workspace_root(root_value)
    except ValueError as exc:
        raise SandboxContractError(
            "SANDBOX_WORKSPACE_INVALID",
            f"Sandbox workspace root is invalid: {workspace_id}",
            status_code=400,
        ) from exc
    touched = store.touch(workspace_id) or record
    touched["root_path"] = root_path
    return touched


def _template_workspace_access(value: Any) -> str:
    mode = str(value or "none").strip().lower()
    if mode in {"read", "readonly"}:
        return "read_only"
    if mode == "read_only_when_selected":
        return "read_only"
    if mode in {"none_unless_explicit", "explicit"}:
        return "none"
    if mode in {"overlay", "read_only", "none"}:
        return mode
    return "none"


def _hash_access_key(access_key: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{access_key}".encode("utf-8")).hexdigest()
    return f"sha256:{salt}:{digest}"


def _verify_access_key(stored_hash: str | None, access_key: str | None) -> bool:
    if not stored_hash or not access_key:
        return False
    try:
        scheme, salt, digest = stored_hash.split(":", 2)
    except ValueError:
        return False
    if scheme != "sha256":
        return False
    candidate = hashlib.sha256(f"{salt}:{access_key}".encode("utf-8")).hexdigest()
    return secrets.compare_digest(candidate, digest)


def _access_key_hint(access_key: str) -> str | None:
    text = str(access_key or "")
    if not text:
        return None
    return f"ends:{text[-4:]}" if len(text) >= 4 else "set"


def _load_sandbox_template(template_id: str | None) -> dict[str, Any]:
    clean_id = str(template_id or "").strip()
    if not clean_id or "/" in clean_id or "\\" in clean_id or ".." in clean_id:
        return {}
    return sandbox_template_by_id(clean_id)


def _packages_from_template_runtime(runtime: dict[str, Any]) -> tuple[PackageSpec, ...]:
    raw_packages = runtime.get("packages") if isinstance(runtime, dict) else []
    if not isinstance(raw_packages, list):
        return ()
    packages: list[PackageSpec] = []
    for raw in raw_packages:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        packages.append(
            PackageSpec(
                name=name[:160],
                version=str(raw.get("version"))[:80] if raw.get("version") is not None else None,
                source=str(raw.get("manager") or raw.get("source") or "")[:80] or None,
            )
        )
    return tuple(packages)
