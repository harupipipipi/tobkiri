from __future__ import annotations

from typing import Protocol

from ..models import (
    EnsureResult,
    EnsureRuntimeRequest,
    ProgressEvent,
    ProviderInstance,
    ReconcileResult,
    RuntimeProviderStatus,
    RuntimeRequirements,
    SandboxCreateSpec,
    UninstallResult,
    UninstallRuntimeRequest,
    UpdateResult,
    UpdateRuntimeRequest,
)


class ProgressSink(Protocol):
    def emit(self, event: ProgressEvent) -> None: ...


class GuestAgentClient(Protocol):
    def exec(self, sandbox_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    def capture_frame(self, sandbox_id: str, seat_id: str) -> dict[str, object]: ...
    def desktop_input(
        self,
        sandbox_id: str,
        seat_id: str,
        payload: dict[str, object],
        *,
        actor: str = "human",
    ) -> dict[str, object]: ...


class RuntimeProvider(Protocol):
    provider_id: str

    def doctor(self, request: RuntimeRequirements) -> RuntimeProviderStatus: ...
    def ensure(self, request: EnsureRuntimeRequest, progress: ProgressSink) -> EnsureResult: ...
    def update(self, request: UpdateRuntimeRequest, progress: ProgressSink) -> UpdateResult: ...
    def uninstall(self, request: UninstallRuntimeRequest, progress: ProgressSink) -> UninstallResult: ...

    def create(self, spec: SandboxCreateSpec) -> ProviderInstance: ...
    def start(self, instance: ProviderInstance) -> ProviderInstance: ...
    def stop(self, instance: ProviderInstance, *, force: bool = False) -> None: ...
    def destroy(self, instance: ProviderInstance) -> None: ...
    def reconcile(self, persisted: ProviderInstance) -> ReconcileResult: ...
    def connect_agent(self, instance: ProviderInstance) -> GuestAgentClient: ...


class NullProgressSink:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)
