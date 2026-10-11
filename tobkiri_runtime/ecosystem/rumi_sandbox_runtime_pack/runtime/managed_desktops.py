"""Finite Profile-captured reads of actual managed sandbox state.

Never construct SandboxManager for a read: construction reconciles state and
list_instances enforces lifecycle. Writes/control require separate authority
operations and intentionally have no entry point here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

PACK_ID = "rumi_sandbox_runtime_pack"
FUNCTION_ID = "tobkiri.managed-desktops.read"
CONTRACT_ID = "tobkiri.resource.managed-desktops.v1"
OPERATIONS = {
    "rumi_sandbox_runtime_pack.desktops-list": "desktops",
    "rumi_sandbox_runtime_pack.runtime-providers-read": "providers",
    "rumi_sandbox_runtime_pack.runtime-doctor-read": "doctor",
    "rumi_sandbox_runtime_pack.sandbox-templates-read": "templates",
}


def registry_path_for_profile(root: Path, profile_id: str) -> Path:
    """Return the explicit canonical per-Profile managed registry location."""
    from core_runtime.profile_workspace import ProfileWorkspaceManager

    paths = ProfileWorkspaceManager(root).paths_for_profile(profile_id)
    return paths.state_dir / "sandbox" / "sandboxes.json"


def read_desktops(path: Path) -> dict[str, Any]:
    """Read current persisted desktops without repair, lifecycle or credentials."""
    # A captured owner root must not be redirected by a state-file symlink.
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise PermissionError("managed desktop registry path is redirected")
    if not path.exists():
        return {"desktops": []}
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("managed desktop registry exceeds the read limit")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 5:
        raise ValueError("managed desktop registry requires schema version 5")
    instances = document.get("instances")
    if not isinstance(instances, dict):
        raise ValueError("managed desktop registry instances are invalid")
    desktops = []
    for item in instances.values():
        if not isinstance(item, dict):
            raise ValueError("managed desktop registry record is invalid")
        if item.get("display") is not True:
            continue
        seat = item.get("sandbox_id")
        if not isinstance(seat, str) or not seat:
            raise ValueError("managed desktop registry identity is invalid")
        spec = item.get("desktop_spec") or {}
        if not isinstance(spec, dict):
            raise ValueError("managed desktop specification is invalid")
        state = item.get("state") or item.get("status") or "unknown"
        desktops.append(
            {
                "seat_id": seat,
                "sandbox_id": seat,
                "name": item.get("name") or "Ubuntu Desktop",
                "status": "running" if state in {"ready", "busy"} else state,
                "provider_id": item.get("provider_id"),
                "template_id": item.get("template_id"),
                "assigned_agent": item.get("assigned_agent_id"),
                "resolution": {
                    "width": spec.get("width", 1440),
                    "height": spec.get("height", 900),
                },
            }
        )
    return {"desktops": sorted(desktops, key=lambda item: item["seat_id"])}


class ManagedDesktopReadHostFactoryV4:
    """Register finite read operations only after exact Host binding capture."""

    function_id = FUNCTION_ID

    def __init__(
        self, *, captured_legacy_defaults_registry: Path | None = None
    ) -> None:
        """Accept an optional Host-owned legacy Defaults registry binding.

        The composition owner must establish legacy ownership before supplying
        this path. It is never discovered from HOME/environment/request data.
        Non-Defaults profiles cannot consume it. No migration/write is done.
        """
        self._legacy_defaults_registry = captured_legacy_defaults_registry

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture immutable Profile/root and reject incomplete bindings."""
        if not context.profile_id or context.user_data_root is None:
            raise PermissionError("managed desktop capture is incomplete")
        if not context.provider_bindings:
            raise PermissionError("managed desktop bindings are unavailable")
        seen: set[str] = set()
        for binding in context.provider_bindings:
            operation = binding.operation
            if (
                binding.function.function_id != FUNCTION_ID
                or operation.contract_id != CONTRACT_ID
                or operation.operation_id not in OPERATIONS
                or operation.operation_id in seen
            ):
                raise PermissionError("managed desktop binding is invalid")
            seen.add(operation.operation_id)
            key = (CONTRACT_ID, operation.operation_id, binding.principal_ref.value)
            if key not in context.domain_ids:
                raise PermissionError("managed desktop domain is unavailable")
        path = registry_path_for_profile(context.user_data_root, context.profile_id)
        legacy_path = None
        if context.profile_id == "defaults":
            legacy_path = self._legacy_defaults_registry
            if legacy_path is None:
                legacy_path = legacy_defaults_registry_path()

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if (
                operation_id not in seen
                or payload.get("profile_id") != context.profile_id
                or set(payload) - {"profile_id", "_session_id"}
            ):
                raise PermissionError("managed desktop read request is invalid")
            kind = OPERATIONS[operation_id]
            if kind == "desktops":
                # Prefer actual scoped data. The explicit Host-owned legacy
                # Defaults snapshot bridges pre-migration data without copying,
                # manager construction, caller paths, or cross-profile fallback.
                if path.is_symlink() or any(
                    parent.is_symlink() for parent in path.parents
                ):
                    raise PermissionError("managed desktop scoped path is redirected")
                if not path.exists() and legacy_path is not None:
                    return read_desktops(legacy_path)
                return read_desktops(path)
            result = read_runtime_metadata(kind)
            invocation.assert_current()
            return result

        return CapturedHostProviderV4(
            tuple(
                HostProviderContributionV4(
                    contract_id=CONTRACT_ID,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=context.domain_ids[
                        (
                            CONTRACT_ID,
                            binding.operation.operation_id,
                            binding.principal_ref.value,
                        )
                    ],
                    invoke=invoke,
                )
                for binding in context.provider_bindings
            ),
            lambda: None,
        )


def read_runtime_metadata(kind: str) -> dict[str, Any]:
    """Describe real implementations without credential/network/process probes."""
    import platform
    from ecosystem.defaultspack.backend.sandbox.providers.cloudflare_bridge import (
        CLOUDFLARE_BRIDGE_CAPABILITIES,
    )
    from ecosystem.defaultspack.backend.sandbox.providers.docker_provider import (
        DOCKER_CAPABILITIES,
    )
    from ecosystem.defaultspack.backend.sandbox.providers.linux_native import (
        DESKTOP_CAPABILITIES,
    )
    from ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu import (
        MANAGED_UBUNTU_CAPABILITIES,
    )
    from ecosystem.defaultspack.blocks.sandbox.api import _template_summaries

    if kind == "templates":
        return {"templates": _template_summaries()}
    system = platform.system().lower()
    default = {"darwin": "mac_lima", "windows": "windows_wsl"}.get(
        system, "linux_native"
    )
    definitions = (
        ("linux_native", "Linux native", "linux", DESKTOP_CAPABILITIES),
        ("mac_lima", "Lima", "darwin", MANAGED_UBUNTU_CAPABILITIES),
        ("windows_wsl", "WSL", "windows", MANAGED_UBUNTU_CAPABILITIES),
        ("docker", "Docker", None, DOCKER_CAPABILITIES),
        (
            "cloudflare_sandbox_bridge",
            "Cloudflare Sandbox Bridge",
            None,
            CLOUDFLARE_BRIDGE_CAPABILITIES,
        ),
    )
    providers = []
    for identifier, label, supported_platform, capabilities in definitions:
        remote = identifier == "cloudflare_sandbox_bridge"
        platform_supported = supported_platform in {None, system}
        message = (
            "この接続先はデスクトップに対応していません。"
            if remote
            else (
                "このOSは対象外です。"
                if not platform_supported
                else "登録済みです。インストール状態とゲストの稼働は未確認です。"
            )
        )
        providers.append(
            {
                "provider_id": identifier,
                "label": label,
                "status": (
                    "available"
                    if supported_platform in {None, system}
                    else "unavailable"
                ),
                "selected": identifier == default,
                "registered": True,
                "host_platform_supported": platform_supported,
                "ready": False,
                "platform": supported_platform or "cross-platform",
                "capabilities": sorted(capabilities),
                "diagnostics": {
                    "probe_status": "not_run",
                    "source": "canonical-provider-implementation",
                },
                "message": message,
            }
        )
    result = {
        "providers": providers,
        "selected_provider_id": default,
        "default_provider_id": default,
        "runtime_version": None,
        "guest_protocol": 1,
        "diagnostics": {"probe_status": "not_run"},
        "operation_support": {
            "create": False,
            "setup": False,
            "lifecycle": False,
            "delete": False,
            "access": False,
            "control": False,
            "frame": False,
            "doctor": False,
        },
    }
    if kind == "providers":
        return result
    if kind == "doctor":
        return {
            "status": "unavailable",
            "providers": providers,
            "selected_provider_id": default,
            "missing": [],
            "diagnostics": {"probe_status": "not_run"},
            "operation_support": dict(result["operation_support"]),
            "message": (
                "MacではLimaを使う実装があります。"
                if system == "darwin"
                else "このOS向けのデスクトップ実装があります。"
            )
            + "現在のアプリでは、ゲストの診断・作成・操作が未接続です。"
            "一覧は確認できます。対応する更新を待ってから再読み込みしてください。"
            "Tobkiri本体の実行環境とは別の機能です。",
        }
    raise ValueError("managed runtime metadata operation is invalid")


def legacy_defaults_registry_path() -> Path:
    """Resolve the historic global Defaults namespace without manager creation.

    One-way compatibility ownership is limited to captured Profile `defaults`.
    The legacy Defaultspack namespace historically belongs to Defaults. This
    Host-side static resolver may honor trusted process environment; no browser
    path/profile/approval input reaches it and no registry is created or repaired.
    """
    from ecosystem.defaultspack.backend.sandbox.sandbox_manager import SandboxManager

    return SandboxManager._default_state_dir() / "sandboxes.json"


HOST_PROVIDER_FACTORY = {FUNCTION_ID: ManagedDesktopReadHostFactoryV4()}
