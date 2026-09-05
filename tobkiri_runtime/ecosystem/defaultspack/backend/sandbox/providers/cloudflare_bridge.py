from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Mapping
from urllib import error as urllib_error
from urllib import parse, request

from ..errors import RUNTIME_PROVIDER_UNAVAILABLE, SandboxContractError
from ..guest.protocol import GuestExecRequest
from ..models import (
    Diagnostic,
    EnsureRuntimeRequest,
    OperationResult,
    ProgressEvent,
    ProviderInstance,
    ReconcileResult,
    RuntimeProviderStatus,
    RuntimeRequirements,
    SandboxCreateSpec,
    UninstallRuntimeRequest,
    UpdateRuntimeRequest,
    model_to_dict,
)
from ..policy import validate_workspace_relative_path
from core_runtime.host_contract import host_contract_value
from .base import ProgressSink


CLOUDFLARE_BRIDGE_CAPABILITIES = frozenset(
    {
        "sandbox.exec",
        "sandbox.files",
        "sandbox.resource_limits",
        "sandbox.container",
    }
)
MAX_BRIDGE_FILE_BYTES = 32 * 1024 * 1024
WORKSPACE_ROOT = "/workspace"
REDACTED_SECRET = "[REDACTED]"
BEARER_TOKEN_RE = re.compile(r"(?i)(\bbearer\s+)[^\s\"'<>]+")


@dataclass(frozen=True)
class BridgeResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""


BridgeTransport = Callable[[str, str, bytes | None, Mapping[str, str], float | None], BridgeResponse]


class CloudflareSandboxBridgeProvider:
    """Runtime provider for the Cloudflare Sandbox Bridge HTTP API.

    This intentionally exposes only the public bridge surface that can be
    verified today: sandbox lifecycle, argv exec, and bounded file writes.
    Desktop, host workspace, terminal, and PC-local semantics remain PC-only.
    """

    provider_id = "cloudflare_sandbox_bridge"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: BridgeTransport | None = None,
        health_timeout_seconds: float = 2.0,
    ) -> None:
        self._configured_base_url = base_url
        self._configured_api_key = api_key
        self._transport = transport or _urllib_transport
        self._health_timeout_seconds = health_timeout_seconds
        self._instances: dict[str, ProviderInstance] = {}

    def doctor(self, request: RuntimeRequirements) -> RuntimeProviderStatus:
        base_url = self._base_url()
        api_key = self._api_key()
        diagnostics: list[Diagnostic] = []
        missing: list[str] = []
        health_ok = False

        if not base_url:
            missing.append("env:RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL")
            diagnostics.append(
                Diagnostic(
                    code="CLOUDFLARE_SANDBOX_BRIDGE_URL_MISSING",
                    message="Set RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL to a deployed or local Sandbox Bridge endpoint.",
                    severity="info",
                )
            )

        if base_url and not api_key and not _is_local_url(base_url):
            missing.append("env:RUMI_CLOUDFLARE_SANDBOX_API_KEY")
            diagnostics.append(
                Diagnostic(
                    code="CLOUDFLARE_SANDBOX_API_KEY_MISSING",
                    message="Set RUMI_CLOUDFLARE_SANDBOX_API_KEY before using a non-local Sandbox Bridge.",
                    severity="warning",
                )
            )

        if base_url and _is_remote_plain_http_url(base_url):
            missing.append("cloudflare_sandbox_bridge_https")
            diagnostics.append(
                Diagnostic(
                    code="CLOUDFLARE_SANDBOX_BRIDGE_INSECURE_URL",
                    message="Use HTTPS for deployed Cloudflare Sandbox Bridge URLs. Plain HTTP is allowed only for localhost development.",
                    severity="warning",
                    details={"base_url": _redact_url(base_url)},
                )
            )

        if base_url and not missing:
            try:
                response = self._request("GET", "/health", timeout=self._health_timeout_seconds)
                health_ok = 200 <= response.status < 300
                if not health_ok:
                    missing.append("cloudflare_sandbox_bridge_health")
                    diagnostics.append(
                        Diagnostic(
                            code="CLOUDFLARE_SANDBOX_BRIDGE_HEALTH_FAILED",
                            message="Cloudflare Sandbox Bridge health check did not return a success status.",
                            severity="warning",
                            details={"status": response.status},
                        )
                    )
            except SandboxContractError as exc:
                missing.append("cloudflare_sandbox_bridge_health")
                diagnostics.append(
                    Diagnostic(
                        code="CLOUDFLARE_SANDBOX_BRIDGE_UNREACHABLE",
                        message="Cloudflare Sandbox Bridge could not be reached.",
                        severity="warning",
                        details=exc.details,
                    )
                )

        missing_capabilities = sorted(request.required_capabilities - CLOUDFLARE_BRIDGE_CAPABILITIES)
        missing.extend(missing_capabilities)
        if missing_capabilities:
            diagnostics.append(
                Diagnostic(
                    code="CLOUDFLARE_SANDBOX_BRIDGE_CAPABILITY_UNSUPPORTED",
                    message="Cloudflare Sandbox Bridge does not provide every requested runtime capability.",
                    severity="info",
                    details={"missing_capabilities": missing_capabilities},
                )
            )

        ready = bool(base_url) and health_ok and not missing_capabilities and not (
            not api_key and not _is_local_url(base_url)
        ) and not (
            bool(base_url) and _is_remote_plain_http_url(base_url)
        )
        return RuntimeProviderStatus(
            provider_id=self.provider_id,
            platform="cloudflare",
            available=bool(base_url),
            installed=health_ok,
            ready=ready,
            version="bridge-http-v1" if health_ok else None,
            capabilities=CLOUDFLARE_BRIDGE_CAPABILITIES if base_url else frozenset(),
            missing_requirements=tuple(missing),
            requires_user_action=not ready,
            user_action=None if ready else _user_action(missing),
            diagnostics=tuple(diagnostics),
        )

    def ensure(self, request: EnsureRuntimeRequest, progress: ProgressSink) -> OperationResult:
        progress.emit(
            ProgressEvent(
                operation_id="cloudflare-sandbox-bridge-ensure",
                stage="doctor",
                message="Checking Cloudflare Sandbox Bridge",
            )
        )
        status = self.doctor(request.requirements)
        if status.ready:
            progress.emit(
                ProgressEvent(
                    operation_id="cloudflare-sandbox-bridge-ensure",
                    stage="ready",
                    message="Cloudflare Sandbox Bridge is ready",
                    percent=100,
                )
            )
            return OperationResult(
                ok=True,
                provider_id=self.provider_id,
                operation_id="cloudflare-sandbox-bridge-ensure",
                status="completed",
            )
        return OperationResult(
            ok=False,
            provider_id=self.provider_id,
            operation_id="cloudflare-sandbox-bridge-ensure",
            status="failed",
            diagnostics=status.diagnostics,
            requires_user_action=status.requires_user_action,
            user_action=status.user_action,
        )

    def update(self, request: UpdateRuntimeRequest, progress: ProgressSink) -> OperationResult:
        del request
        progress.emit(
            ProgressEvent(
                operation_id="cloudflare-sandbox-bridge-update",
                stage="skipped",
                message="Cloudflare Sandbox Bridge is deployed outside this local runtime",
                percent=100,
            )
        )
        return OperationResult(
            ok=True,
            provider_id=self.provider_id,
            operation_id="cloudflare-sandbox-bridge-update",
            status="skipped",
        )

    def uninstall(self, request: UninstallRuntimeRequest, progress: ProgressSink) -> OperationResult:
        del request
        for instance in list(self._instances.values()):
            self.destroy(instance)
        progress.emit(
            ProgressEvent(
                operation_id="cloudflare-sandbox-bridge-uninstall",
                stage="destroyed",
                message="Destroyed tracked Cloudflare bridge sandboxes",
                percent=100,
            )
        )
        return OperationResult(
            ok=True,
            provider_id=self.provider_id,
            operation_id="cloudflare-sandbox-bridge-uninstall",
            status="completed",
        )

    def create(self, spec: SandboxCreateSpec) -> ProviderInstance:
        if spec.template.desktop is not None and spec.template.desktop.enabled:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Cloudflare Sandbox Bridge does not provide desktop sandboxes.",
                status_code=503,
            )
        missing_capabilities = sorted(spec.template.provider_requirements - CLOUDFLARE_BRIDGE_CAPABILITIES)
        if missing_capabilities:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Cloudflare Sandbox Bridge cannot satisfy this sandbox template.",
                status_code=503,
                details={"missing_capabilities": missing_capabilities},
            )
        self._require_ready(spec.template.provider_requirements)
        response = self._request_json("POST", "/v1/sandbox", body={})
        bridge_id = str(response.get("id") or "").strip()
        if not bridge_id:
            raise SandboxContractError(
                "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_RESPONSE",
                "Cloudflare Sandbox Bridge did not return a sandbox id.",
                status_code=502,
                details={"response": response},
            )
        instance = ProviderInstance(
            provider_id=self.provider_id,
            provider_instance_id=bridge_id,
            sandbox_id=bridge_id,
            runtime_id="cloudflare-sandbox-bridge",
            state="stopped",
            opaque_state={
                "bridge_sandbox_id": bridge_id,
                "bridge_base_url": self._base_url(),
                "template_id": spec.template.template_id,
                "workspace_binding": model_to_dict(spec.workspace_binding),
                "resource_limits": model_to_dict(spec.template.resources),
                "metadata": model_to_dict(dict(spec.metadata)),
            },
        )
        self._instances[bridge_id] = instance
        return instance

    def start(self, instance: ProviderInstance) -> ProviderInstance:
        bridge_id = _bridge_id(instance)
        try:
            self._request_json("GET", f"/v1/sandbox/{_quote_segment(bridge_id)}/running")
        except SandboxContractError as exc:
            raise SandboxContractError(
                "CLOUDFLARE_SANDBOX_BRIDGE_START_FAILED",
                "Cloudflare Sandbox Bridge sandbox is not reachable.",
                status_code=503,
                details=exc.details,
            ) from exc
        started = ProviderInstance(
            provider_id=instance.provider_id,
            provider_instance_id=instance.provider_instance_id,
            sandbox_id=instance.sandbox_id,
            runtime_id=instance.runtime_id,
            state="ready",
            opaque_state=instance.opaque_state,
            generation=instance.generation + 1,
        )
        self._instances[started.provider_instance_id] = started
        return started

    def stop(self, instance: ProviderInstance, *, force: bool = False) -> None:
        del force
        stopped = ProviderInstance(
            provider_id=instance.provider_id,
            provider_instance_id=instance.provider_instance_id,
            sandbox_id=instance.sandbox_id,
            runtime_id=instance.runtime_id,
            state="stopped",
            opaque_state=instance.opaque_state,
            generation=instance.generation + 1,
        )
        self._instances[stopped.provider_instance_id] = stopped

    def destroy(self, instance: ProviderInstance) -> None:
        bridge_id = _bridge_id(instance)
        try:
            self._request("DELETE", f"/v1/sandbox/{_quote_segment(bridge_id)}")
        finally:
            self._instances.pop(instance.provider_instance_id, None)

    def reconcile(self, persisted: ProviderInstance) -> ReconcileResult:
        current = self._instances.get(persisted.provider_instance_id, persisted)
        try:
            payload = self._request_json("GET", f"/v1/sandbox/{_quote_segment(_bridge_id(current))}/running")
        except SandboxContractError as exc:
            failed = ProviderInstance(
                provider_id=current.provider_id,
                provider_instance_id=current.provider_instance_id,
                sandbox_id=current.sandbox_id,
                runtime_id=current.runtime_id,
                state="failed",
                opaque_state={**dict(current.opaque_state), "last_error": exc.message},
                generation=current.generation + 1,
            )
            return ReconcileResult(
                instance=failed,
                changed=True,
                diagnostics=(
                    Diagnostic(
                        code=exc.code,
                        message=exc.message,
                        severity="warning",
                        details=exc.details,
                    ),
                ),
            )
        state = "ready" if bool(payload.get("running")) else current.state
        reconciled = ProviderInstance(
            provider_id=current.provider_id,
            provider_instance_id=current.provider_instance_id,
            sandbox_id=current.sandbox_id,
            runtime_id=current.runtime_id,
            state=state,
            opaque_state=current.opaque_state,
            generation=current.generation,
        )
        return ReconcileResult(instance=reconciled, changed=reconciled != persisted)

    def connect_agent(self, instance: ProviderInstance) -> "CloudflareSandboxBridgeGuestAgent":
        return CloudflareSandboxBridgeGuestAgent(
            transport=self._transport,
            base_url=self._base_url() or "",
            api_key=self._api_key(),
            output_bytes=_output_bytes_from_instance(instance),
        )

    def _require_ready(self, capabilities: frozenset[str]) -> None:
        status = self.doctor(RuntimeRequirements(provider_id=self.provider_id, required_capabilities=capabilities))
        if not status.ready:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Cloudflare Sandbox Bridge is not ready.",
                status_code=503,
                details={
                    "missing_requirements": list(status.missing_requirements),
                    "diagnostics": [model_to_dict(item) for item in status.diagnostics],
                },
            )

    def _request_json(self, method: str, path: str, *, body: Mapping[str, object] | None = None) -> dict[str, object]:
        raw_body = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"content-type": "application/json"} if body is not None else {}
        response = self._request(method, path, raw_body, headers=headers)
        if not response.body:
            return {}
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except Exception as exc:
            raise SandboxContractError(
                "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_JSON",
                "Cloudflare Sandbox Bridge returned invalid JSON.",
                status_code=502,
            ) from exc
        if not isinstance(decoded, dict):
            raise SandboxContractError(
                "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_JSON",
                "Cloudflare Sandbox Bridge returned a non-object JSON payload.",
                status_code=502,
            )
        return decoded

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> BridgeResponse:
        return _request_bridge(
            self._transport,
            self._base_url() or "",
            self._api_key(),
            method,
            path,
            body,
            headers=headers,
            timeout=timeout,
        )

    def _base_url(self) -> str | None:
        value = self._configured_base_url if self._configured_base_url is not None else host_contract_value("cloudflare_sandbox_bridge_url", provider_id="cloudflare")
        return _normalize_base_url(value)

    def _api_key(self) -> str | None:
        value = self._configured_api_key if self._configured_api_key is not None else host_contract_value("cloudflare_sandbox_bridge_token", provider_id="cloudflare")
        clean = str(value or "").strip()
        return clean or None


class CloudflareSandboxBridgeGuestAgent:
    def __init__(
        self,
        *,
        transport: BridgeTransport,
        base_url: str,
        api_key: str | None,
        output_bytes: int | None = None,
    ) -> None:
        self._transport = transport
        self._base_url = base_url
        self._api_key = api_key
        self._output_bytes = output_bytes

    def exec(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        request_payload = GuestExecRequest.from_payload(payload)
        if request_payload.env:
            return {
                "ok": False,
                "sandbox_id": sandbox_id,
                "code": "CLOUDFLARE_SANDBOX_BRIDGE_ENV_UNSUPPORTED",
                "error": "Cloudflare Sandbox Bridge HTTP exec does not support per-call environment variables yet.",
                "status_code": 501,
                "client_request_id": request_payload.client_request_id,
            }
        if request_payload.stdin is not None:
            return {
                "ok": False,
                "sandbox_id": sandbox_id,
                "code": "CLOUDFLARE_SANDBOX_BRIDGE_STDIN_UNSUPPORTED",
                "error": "Cloudflare Sandbox Bridge HTTP exec does not support stdin yet.",
                "status_code": 501,
                "client_request_id": request_payload.client_request_id,
            }
        body = json.dumps(
            {
                "argv": list(request_payload.argv),
                "cwd": _workspace_cwd(request_payload.cwd),
                "timeout_ms": request_payload.timeout_ms,
            }
        ).encode("utf-8")
        try:
            response = _request_bridge(
                self._transport,
                self._base_url,
                self._api_key,
                "POST",
                f"/v1/sandbox/{_quote_segment(sandbox_id)}/exec",
                body,
                headers={"content-type": "application/json", "accept": "text/event-stream"},
                timeout=max(1.0, request_payload.timeout_ms / 1000 + 5),
            )
            stdout, stderr, stdout_truncated, stderr_truncated, exit_code, error_payload = _parse_exec_sse(
                response.body,
                output_bytes=self._output_bytes,
            )
        except SandboxContractError as exc:
            return {
                "ok": False,
                "sandbox_id": sandbox_id,
                "code": exc.code,
                "error": exc.message,
                "status_code": exc.status_code,
                "details": dict(exc.details),
                "client_request_id": request_payload.client_request_id,
                "provider_runtime": "cloudflare_sandbox_bridge",
            }
        if error_payload is not None:
            return {
                "ok": False,
                "sandbox_id": sandbox_id,
                "code": str(error_payload.get("code") or "CLOUDFLARE_SANDBOX_BRIDGE_EXEC_ERROR"),
                "error": _redact_bridge_secret_text(
                    str(error_payload.get("error") or "Cloudflare Sandbox Bridge exec failed."),
                    self._api_key,
                )[:1000],
                "status_code": 502,
                "stdout": stdout,
                "stderr": stderr,
                "client_request_id": request_payload.client_request_id,
                "provider_runtime": "cloudflare_sandbox_bridge",
            }
        if exit_code is None:
            return {
                "ok": False,
                "sandbox_id": sandbox_id,
                "code": "CLOUDFLARE_SANDBOX_BRIDGE_EXEC_INCOMPLETE",
                "error": "Cloudflare Sandbox Bridge exec stream ended before an exit event.",
                "status_code": 502,
                "stdout": stdout,
                "stderr": stderr,
                "client_request_id": request_payload.client_request_id,
                "provider_runtime": "cloudflare_sandbox_bridge",
            }
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "argv": list(request_payload.argv),
            "cwd": request_payload.cwd,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "client_request_id": request_payload.client_request_id,
            "provider_runtime": "cloudflare_sandbox_bridge",
        }

    def apply_file_patch(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        operations = _file_patch_operations(payload)
        applied: list[dict[str, object]] = []
        for operation in operations:
            path = str(operation["path"])
            content = operation["content"]
            if not isinstance(content, bytes):
                raise SandboxContractError(
                    "INVALID_SANDBOX_FILE_PATCH",
                    "Sandbox file patch content must be bytes.",
                    status_code=400,
                )
            _request_bridge(
                self._transport,
                self._base_url,
                self._api_key,
                "PUT",
                f"/v1/sandbox/{_quote_segment(sandbox_id)}/file/{_quote_path(path)}",
                content,
                headers={"content-type": "application/octet-stream"},
                timeout=60,
            )
            applied.append({"path": path, "bytes": len(content)})
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "applied": applied,
            "files_written": len(applied),
            "provider_runtime": "cloudflare_sandbox_bridge",
        }

    def read_file(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        path = validate_workspace_relative_path(payload.get("path"), field="path")
        response = _request_bridge(
            self._transport,
            self._base_url,
            self._api_key,
            "GET",
            f"/v1/sandbox/{_quote_segment(sandbox_id)}/file/{_quote_path(path)}",
            None,
            headers={"accept": "application/octet-stream"},
            timeout=60,
        )
        return _read_file_payload(
            path,
            response.body,
            start_line=payload.get("start_line"),
            end_line=payload.get("end_line"),
            max_chars=payload.get("max_chars") or payload.get("max_output_chars"),
        )

    def expose_port(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        del payload
        return {
            "ok": False,
            "sandbox_id": sandbox_id,
            "code": "CLOUDFLARE_SANDBOX_BRIDGE_PORT_UNSUPPORTED",
            "error": "Cloudflare Sandbox Bridge provider does not expose stable preview ports through this adapter yet.",
            "status_code": 501,
            "provider_runtime": "cloudflare_sandbox_bridge",
        }

    def capture_frame(self, sandbox_id: str, seat_id: str) -> dict[str, object]:
        return {
            "ok": False,
            "sandbox_id": sandbox_id,
            "seat_id": seat_id,
            "code": "SANDBOX_DESKTOP_NOT_AVAILABLE",
            "error": "Cloudflare Sandbox Bridge provider does not expose desktop capture.",
            "status_code": 501,
            "provider_runtime": "cloudflare_sandbox_bridge",
        }

    def desktop_input(
        self,
        sandbox_id: str,
        seat_id: str,
        payload: Mapping[str, object],
        *,
        actor: str = "human",
    ) -> dict[str, object]:
        del payload, actor
        return {
            "ok": False,
            "sandbox_id": sandbox_id,
            "seat_id": seat_id,
            "code": "SANDBOX_DESKTOP_NOT_AVAILABLE",
            "error": "Cloudflare Sandbox Bridge provider does not expose desktop input.",
            "status_code": 501,
            "provider_runtime": "cloudflare_sandbox_bridge",
        }


def _request_bridge(
    transport: BridgeTransport,
    base_url: str,
    api_key: str | None,
    method: str,
    path: str,
    body: bytes | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> BridgeResponse:
    if not base_url:
        raise SandboxContractError(
            "CLOUDFLARE_SANDBOX_BRIDGE_URL_MISSING",
            "Cloudflare Sandbox Bridge URL is not configured.",
            status_code=503,
        )
    if _is_remote_plain_http_url(base_url):
        raise SandboxContractError(
            "CLOUDFLARE_SANDBOX_BRIDGE_INSECURE_URL",
            "Cloudflare Sandbox Bridge requires HTTPS unless the bridge URL is localhost.",
            status_code=400,
            details={"base_url": _redact_url(base_url)},
        )
    request_headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    if api_key:
        request_headers["authorization"] = f"Bearer {api_key}"
    try:
        response = transport(method.upper(), _join_url(base_url, path), body, request_headers, timeout)
    except TimeoutError as exc:
        raise SandboxContractError(
            "CLOUDFLARE_SANDBOX_BRIDGE_TIMEOUT",
            "Cloudflare Sandbox Bridge request timed out.",
            status_code=504,
        ) from exc
    except OSError as exc:
        raise SandboxContractError(
            "CLOUDFLARE_SANDBOX_BRIDGE_UNREACHABLE",
            "Cloudflare Sandbox Bridge request failed.",
            status_code=503,
            details={"error": _redact_bridge_secret_text(str(exc), api_key)[:1000]},
        ) from exc
    if response.status < 200 or response.status >= 300:
        error_body = response.body.decode("utf-8", errors="replace")
        raise SandboxContractError(
            "CLOUDFLARE_SANDBOX_BRIDGE_HTTP_ERROR",
            "Cloudflare Sandbox Bridge returned an error status.",
            status_code=502 if response.status >= 500 else 400,
            details={
                "bridge_status": response.status,
                "body": _redact_bridge_secret_text(error_body, api_key)[:1000],
            },
        )
    return response


def _urllib_transport(method: str, url: str, body: bytes | None, headers: Mapping[str, str], timeout: float | None) -> BridgeResponse:
    req = request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return BridgeResponse(
                status=int(getattr(response, "status", 200)),
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except urllib_error.HTTPError as exc:
        return BridgeResponse(status=int(exc.code), headers=dict(exc.headers.items()), body=exc.read())


def _parse_exec_sse(body: bytes, *, output_bytes: int | None = None) -> tuple[str, str, bool, bool, int | None, dict[str, object] | None]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_bytes = 0
    stderr_bytes = 0
    stdout_truncated = False
    stderr_truncated = False
    exit_code: int | None = None
    error_payload: dict[str, object] | None = None
    text = body.decode("utf-8", errors="replace")
    for raw_event in text.replace("\r\n", "\n").split("\n\n"):
        if not raw_event.strip():
            continue
        event_type = "message"
        data_lines: list[str] = []
        for line in raw_event.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        data = "\n".join(data_lines)
        if event_type in {"stdout", "stderr"}:
            try:
                decoded_bytes = base64.b64decode(data.encode("ascii"), validate=True)
            except (binascii.Error, UnicodeEncodeError) as exc:
                raise SandboxContractError(
                    "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_SSE",
                    "Cloudflare Sandbox Bridge output event was invalid.",
                    status_code=502,
                ) from exc
            decoded = decoded_bytes.decode("utf-8", errors="replace")
            if event_type == "stdout":
                stdout_bytes, truncated = _append_limited_output(
                    stdout_chunks,
                    decoded,
                    current_bytes=stdout_bytes,
                    max_bytes=output_bytes,
                )
                stdout_truncated = stdout_truncated or truncated
            else:
                stderr_bytes, truncated = _append_limited_output(
                    stderr_chunks,
                    decoded,
                    current_bytes=stderr_bytes,
                    max_bytes=output_bytes,
                )
                stderr_truncated = stderr_truncated or truncated
        elif event_type == "exit":
            try:
                payload = json.loads(data or "{}")
            except json.JSONDecodeError as exc:
                raise SandboxContractError(
                    "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_SSE",
                    "Cloudflare Sandbox Bridge exit event was invalid JSON.",
                    status_code=502,
                ) from exc
            if not isinstance(payload, Mapping):
                raise SandboxContractError(
                    "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_SSE",
                    "Cloudflare Sandbox Bridge exit event was invalid.",
                    status_code=502,
                )
            raw_exit_code = payload.get("exit_code")
            if isinstance(raw_exit_code, bool) or not isinstance(
                raw_exit_code, (int, float, str)
            ):
                raise SandboxContractError(
                    "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_SSE",
                    "Cloudflare Sandbox Bridge exit event did not include a numeric exit_code.",
                    status_code=502,
                )
            try:
                exit_code = int(raw_exit_code)
            except (TypeError, ValueError) as exc:
                raise SandboxContractError(
                    "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_SSE",
                    "Cloudflare Sandbox Bridge exit event did not include a numeric exit_code.",
                    status_code=502,
                ) from exc
        elif event_type == "error":
            try:
                payload = json.loads(data or "{}")
            except json.JSONDecodeError as exc:
                raise SandboxContractError(
                    "CLOUDFLARE_SANDBOX_BRIDGE_INVALID_SSE",
                    "Cloudflare Sandbox Bridge error event was invalid JSON.",
                    status_code=502,
                ) from exc
            error_payload = dict(payload) if isinstance(payload, Mapping) else {"error": str(payload)}
    return "".join(stdout_chunks), "".join(stderr_chunks), stdout_truncated, stderr_truncated, exit_code, error_payload


def _append_limited_output(
    chunks: list[str],
    text: str,
    *,
    current_bytes: int,
    max_bytes: int | None,
) -> tuple[int, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if max_bytes is None or max_bytes <= 0:
        chunks.append(text)
        return current_bytes + len(encoded), False
    remaining = max(0, max_bytes - current_bytes)
    if len(encoded) <= remaining:
        chunks.append(text)
        return current_bytes + len(encoded), False
    if remaining > 0:
        chunks.append(encoded[:remaining].decode("utf-8", errors="ignore"))
    return max_bytes, True


def _file_patch_operations(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw_items = payload.get("files")
    if raw_items is None:
        raw_items = payload.get("patch")
    if raw_items is None:
        raw_items = [payload]
    if not isinstance(raw_items, list) or not raw_items:
        raise SandboxContractError(
            "INVALID_SANDBOX_FILE_PATCH",
            "Sandbox file patch requires at least one file operation.",
            status_code=400,
        )
    operations: list[dict[str, object]] = []
    total_bytes = 0
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise SandboxContractError(
                "INVALID_SANDBOX_FILE_PATCH",
                "Sandbox file patch operations must be objects.",
                status_code=400,
            )
        path = validate_workspace_relative_path(raw.get("path"), field="path")
        op = str(raw.get("op") or raw.get("operation") or "write").strip().lower()
        if op not in {"write", "replace", "create", "upsert"}:
            raise SandboxContractError(
                "INVALID_SANDBOX_FILE_PATCH",
                "Sandbox file patch only supports write-style operations.",
                status_code=400,
            )
        content = _patch_content(raw)
        total_bytes += len(content)
        if total_bytes > MAX_BRIDGE_FILE_BYTES:
            raise SandboxContractError(
                "SANDBOX_FILE_PATCH_TOO_LARGE",
                "Cloudflare Sandbox Bridge file patch payload is too large.",
                status_code=413,
            )
        operations.append({"path": path, "content": content})
    return operations


def _patch_content(raw: Mapping[str, object]) -> bytes:
    if "content_base64" in raw:
        value = raw.get("content_base64")
        if not isinstance(value, str):
            raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "content_base64 must be a string.", status_code=400)
        try:
            return base64.b64decode(value, validate=True)
        except Exception as exc:
            raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "content_base64 is invalid.", status_code=400) from exc
    value = raw.get("content")
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "Sandbox file patch requires content or content_base64.", status_code=400)


def _read_file_payload(
    path: str,
    content_bytes: bytes,
    *,
    start_line: object = None,
    end_line: object = None,
    max_chars: object = None,
) -> dict[str, object]:
    if len(content_bytes) > MAX_BRIDGE_FILE_BYTES:
        raise SandboxContractError(
            "SANDBOX_FILE_TOO_LARGE",
            "Cloudflare Sandbox Bridge file read payload is too large.",
            status_code=413,
        )
    content = content_bytes.decode("utf-8", errors="replace")
    payload: dict[str, object] = {
        "ok": True,
        "path": path,
        "content": content,
        "size": len(content_bytes),
        "encoding": "utf-8",
        "provider_runtime": "cloudflare_sandbox_bridge",
    }
    if start_line is not None or end_line is not None:
        window = _line_window(content, start_line=start_line, end_line=end_line)
        payload.update(window)
        payload["content"] = window["content"]
    clipped, truncated, omitted = _clip_chars(str(payload["content"]), _positive_int(max_chars))
    if truncated:
        payload["content"] = clipped
        payload["truncated"] = True
        payload["omitted_chars"] = omitted
    return payload


def _line_window(content: str, *, start_line: object = None, end_line: object = None) -> dict[str, object]:
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    start = _positive_int(start_line) or 1
    end = _positive_int(end_line) or total_lines
    start_index = max(0, start - 1)
    end_index = max(start_index, end)
    actual_end = min(total_lines, end)
    return {
        "content": "".join(lines[start_index:end_index]),
        "start_line": start,
        "end_line": actual_end,
        "total_lines": total_lines,
        "truncated": start > 1 or actual_end < total_lines,
    }


def _clip_chars(content: str, max_chars: int | None) -> tuple[str, bool, int]:
    if max_chars is None or max_chars <= 0 or len(content) <= max_chars:
        return content, False, 0
    return content[:max_chars], True, len(content) - max_chars


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(_numeric_value(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _numeric_value(value: object) -> int | float | str:
    return value if isinstance(value, (int, float, str)) else 0


def _workspace_cwd(cwd: str) -> str:
    if cwd == ".":
        return WORKSPACE_ROOT
    return (PurePosixPath(WORKSPACE_ROOT) / cwd).as_posix()


def _quote_segment(value: str) -> str:
    return parse.quote(value, safe="")


def _quote_path(path: str) -> str:
    return parse.quote(path, safe="/")


def _bridge_id(instance: ProviderInstance) -> str:
    return str(instance.opaque_state.get("bridge_sandbox_id") or instance.provider_instance_id)


def _normalize_base_url(value: str | None) -> str | None:
    clean = str(value or "").strip().rstrip("/")
    if not clean:
        return None
    parsed = parse.urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return clean


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _is_local_url(base_url: str | None) -> bool:
    parsed = parse.urlparse(base_url or "")
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _is_remote_plain_http_url(base_url: str | None) -> bool:
    parsed = parse.urlparse(base_url or "")
    return parsed.scheme == "http" and not _is_local_url(base_url)


def _redact_url(base_url: str) -> str:
    parsed = parse.urlparse(base_url)
    if not parsed.netloc:
        return base_url
    return parse.urlunparse((parsed.scheme, parsed.hostname or "", parsed.path, "", "", ""))


def _redact_bridge_secret_text(text: str, api_key: str | None) -> str:
    redacted = BEARER_TOKEN_RE.sub(r"\1" + REDACTED_SECRET, text)
    key = str(api_key or "")
    if key:
        redacted = redacted.replace(key, REDACTED_SECRET)
    return redacted


def _output_bytes_from_instance(instance: ProviderInstance) -> int | None:
    raw_limits = instance.opaque_state.get("resource_limits")
    if not isinstance(raw_limits, Mapping):
        return None
    try:
        value = int(raw_limits.get("output_bytes") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _user_action(missing: list[str]) -> str:
    if "env:RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL" in missing:
        return "Deploy or run the Cloudflare Sandbox Bridge and set RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL."
    if "env:RUMI_CLOUDFLARE_SANDBOX_API_KEY" in missing:
        return "Set RUMI_CLOUDFLARE_SANDBOX_API_KEY for the deployed Cloudflare Sandbox Bridge."
    if "cloudflare_sandbox_bridge_health" in missing:
        return "Start the Sandbox Bridge Worker or check its deployment, account plan, and Docker/Containers setup."
    return "Choose another runtime provider or reduce the requested sandbox capabilities."
