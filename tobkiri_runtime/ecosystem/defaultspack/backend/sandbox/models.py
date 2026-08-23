from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Mapping


RUNTIME_CAPABILITIES = frozenset(
    {
        "sandbox.exec",
        "sandbox.files",
        "sandbox.terminal.exec",
        "sandbox.workspace.read",
        "sandbox.workspace.write",
        "sandbox.workspace.diff",
        "sandbox.network.request",
        "sandbox.artifact.export",
        "sandbox.overlay_workspace",
        "sandbox.port_forward",
        "sandbox.network_policy",
        "sandbox.resource_limits",
        "sandbox.desktop",
        "sandbox.desktop_input",
        "sandbox.snapshot",
        "sandbox.container",
        "runtime.managed_install",
        "runtime.update",
        "runtime.uninstall",
    }
)

SANDBOX_STATES = frozenset(
    {
        "creating",
        "provisioning",
        "starting",
        "ready",
        "busy",
        "stopping",
        "stopped",
        "failed",
        "destroying",
        "destroyed",
    }
)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: str = "info"
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeProviderStatus:
    provider_id: str
    platform: str
    available: bool
    installed: bool
    ready: bool
    version: str | None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    missing_requirements: tuple[str, ...] = ()
    requires_user_action: bool = False
    user_action: str | None = None
    reboot_required: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "missing_requirements", tuple(self.missing_requirements))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True)
class RuntimeRequirements:
    template_id: str | None = None
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    provider_id: str | None = None
    platform: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_capabilities", frozenset(self.required_capabilities))


@dataclass(frozen=True)
class ProgressEvent:
    operation_id: str
    stage: str
    message: str
    percent: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnsureRuntimeRequest:
    provider_id: str
    requirements: RuntimeRequirements = field(default_factory=RuntimeRequirements)
    approval_reference: str | None = None
    operation_id: str | None = None
    target_revision: str | None = None


@dataclass(frozen=True)
class UpdateRuntimeRequest:
    provider_id: str
    approval_reference: str | None = None
    operation_id: str | None = None
    target_revision: str | None = None


@dataclass(frozen=True)
class UninstallRuntimeRequest:
    provider_id: str
    remove_state: bool = False
    approval_reference: str | None = None
    operation_id: str | None = None
    target_revision: str | None = None


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    provider_id: str
    operation_id: str
    status: str
    diagnostics: tuple[Diagnostic, ...] = ()
    requires_user_action: bool = False
    user_action: str | None = None
    reboot_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


EnsureResult = OperationResult
UpdateResult = OperationResult
UninstallResult = OperationResult


@dataclass(frozen=True)
class PackageSpec:
    name: str
    version: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class DesktopSpec:
    enabled: bool = False
    width: int = 1440
    height: int = 900
    display_backend: str = "x11"
    preset: str | None = None


@dataclass(frozen=True)
class FilesystemPolicy:
    mode: str = "ephemeral_overlay"
    workspace_access: str = "none"
    workspace_paths: tuple[str, ...] = ()
    host_writeback: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_paths", tuple(self.workspace_paths))


@dataclass(frozen=True)
class NetworkPolicy:
    mode: str = "off"
    allowlist: tuple[str, ...] = ()
    approval_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowlist", tuple(self.allowlist))


@dataclass(frozen=True)
class SecretsPolicy:
    mode: str = "denied"
    secret_ids: tuple[str, ...] = ()
    approval_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "secret_ids", tuple(self.secret_ids))


@dataclass(frozen=True)
class ResourceLimits:
    cpu_count: float | None = None
    memory_mb: int | None = None
    pids: int | None = None
    output_bytes: int | None = None
    timeout_ms: int | None = None


@dataclass(frozen=True)
class LifecyclePolicy:
    ttl_seconds: int | None = 900
    persistent: bool = False
    destroy_on_exit: bool = True


@dataclass(frozen=True)
class DesktopRuleConfig:
    role: str | None = None
    instructions: str = ""
    rule_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_ids", tuple(self.rule_ids))


@dataclass(frozen=True)
class DesktopAccessPolicy:
    mode: str = "owner_only"
    owner_id: str | None = None
    key_required: bool = False
    request_required: bool = False
    key_hint: str | None = None
    link_enabled: bool = False


@dataclass(frozen=True)
class DesktopProvisioningPlan:
    packages: tuple[PackageSpec, ...] = ()
    apps: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    status: str = "declared"

    def __post_init__(self) -> None:
        object.__setattr__(self, "packages", tuple(self.packages))
        object.__setattr__(self, "apps", tuple(self.apps))
        object.__setattr__(self, "mcp_servers", tuple(self.mcp_servers))


@dataclass(frozen=True)
class ResolvedSandboxTemplate:
    template_id: str
    template_version: str
    runtime_os: str
    provider_requirements: frozenset[str]
    packages: tuple[PackageSpec, ...]
    desktop: DesktopSpec | None
    filesystem: FilesystemPolicy
    network: NetworkPolicy
    secrets: SecretsPolicy
    resources: ResourceLimits
    lifecycle: LifecyclePolicy
    allowed_operations: frozenset[str]
    source_template_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_requirements", frozenset(self.provider_requirements))
        object.__setattr__(self, "packages", tuple(self.packages))
        object.__setattr__(self, "allowed_operations", frozenset(self.allowed_operations))
        object.__setattr__(self, "source_template_ids", tuple(self.source_template_ids))


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace_id: str | None = None
    mode: str = "none"
    root: str = "."


@dataclass
class SandboxInstance:
    sandbox_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    image: str = "ubuntu:22.04"
    display: bool = False
    template_id: str = "pack.safe"
    template_version: str = "0"
    provider_id: str = ""
    provider_instance_id: str = ""
    provider_opaque_state: Mapping[str, Any] = field(default_factory=dict)
    runtime_id: str = ""
    state: str = "creating"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    stopped_at: float | None = None
    destroyed_at: float | None = None
    last_activity_at: float | None = None
    last_error: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    lifecycle_policy: LifecyclePolicy = field(default_factory=LifecyclePolicy)
    filesystem_policy: FilesystemPolicy = field(default_factory=FilesystemPolicy)
    workspace_binding: WorkspaceBinding = field(default_factory=WorkspaceBinding)
    network_policy: NetworkPolicy = field(default_factory=NetworkPolicy)
    secrets_policy: SecretsPolicy = field(default_factory=SecretsPolicy)
    desktop_spec: DesktopSpec | None = None
    desktop_rules: DesktopRuleConfig = field(default_factory=DesktopRuleConfig)
    desktop_access: DesktopAccessPolicy = field(default_factory=DesktopAccessPolicy)
    desktop_provisioning: DesktopProvisioningPlan = field(default_factory=DesktopProvisioningPlan)
    assigned_agent_id: str | None = None
    generation: int = 0
    recovery_token_hash: str | None = None
    desktop_access_key_hash: str | None = None

    def __post_init__(self) -> None:
        self.capabilities = frozenset(self.capabilities)
        self.provider_opaque_state = dict(self.provider_opaque_state or {})
        if self.state not in SANDBOX_STATES:
            self.state = "failed"
            self.last_error = "Unknown sandbox state in persisted instance"

    def touch(self, *, state: str | None = None, error: str | None = None) -> None:
        self.updated_at = time.time()
        self.last_activity_at = self.updated_at
        if state is not None:
            if state not in SANDBOX_STATES:
                raise ValueError(f"Unknown sandbox state: {state}")
            self.state = state
        if error is not None:
            self.last_error = error


@dataclass(frozen=True)
class SandboxCreateSpec:
    name: str
    template: ResolvedSandboxTemplate
    provider_id: str | None = None
    workspace_binding: WorkspaceBinding = field(default_factory=WorkspaceBinding)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderInstance:
    provider_id: str
    provider_instance_id: str
    sandbox_id: str
    runtime_id: str
    state: str
    opaque_state: Mapping[str, Any] = field(default_factory=dict)
    generation: int = 0


@dataclass(frozen=True)
class ReconcileResult:
    instance: ProviderInstance
    changed: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True)
class DesktopSeatView:
    sandbox_id: str
    seat_id: str
    name: str
    status: str
    width: int
    height: int
    display_backend: str
    frame_seq: int
    last_frame_at: float | None
    control_owner: str | None
    assigned_agent_id: str | None
    isolation_summary: str


def model_to_dict(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field_info.name: model_to_dict(getattr(value, field_info.name))
            for field_info in fields(value)
        }
    if isinstance(value, frozenset):
        return sorted(value)
    if isinstance(value, tuple):
        return [model_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: model_to_dict(item) for key, item in value.items()}
    return value
