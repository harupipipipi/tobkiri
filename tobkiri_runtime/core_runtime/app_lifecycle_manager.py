"""
app_lifecycle_manager.py - アプリケーションライフサイクル管理

セットアップ状態の確認・完了を v4 activation に集約する薄いマネージャ。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping

from .pack_api_server import RuntimeCaptureFactory

logger = logging.getLogger(__name__)


_RUNTIME_READINESS_LOCK = threading.Lock()
_RUNTIME_READINESS_STATE: Dict[str, Any] = {
    "panel_ready": False,
    "runtime_ready": False,
    "runtime_status": "starting",
    "runtime_error": None,
}


def reset_runtime_readiness() -> None:
    with _RUNTIME_READINESS_LOCK:
        _RUNTIME_READINESS_STATE.update(
            {
                "panel_ready": False,
                "runtime_ready": False,
                "runtime_status": "starting",
                "runtime_error": None,
            }
        )


def mark_panel_ready() -> None:
    with _RUNTIME_READINESS_LOCK:
        _RUNTIME_READINESS_STATE.update(
            {
                "panel_ready": True,
                "runtime_status": "panel_ready",
                "runtime_error": None,
            }
        )


def mark_runtime_ready() -> None:
    with _RUNTIME_READINESS_LOCK:
        _RUNTIME_READINESS_STATE.update(
            {
                "panel_ready": True,
                "runtime_ready": True,
                "runtime_status": "runtime_ready",
                "runtime_error": None,
            }
        )


def mark_runtime_failed(error: str) -> None:
    with _RUNTIME_READINESS_LOCK:
        _RUNTIME_READINESS_STATE.update(
            {
                "panel_ready": True,
                "runtime_ready": False,
                "runtime_status": "error",
                "runtime_error": error,
            }
        )


def mark_profile_reconfirmation_required(error: str) -> None:
    """Publish a UI-ready state without treating stale authority as active."""

    with _RUNTIME_READINESS_LOCK:
        _RUNTIME_READINESS_STATE.update(
            {
                "panel_ready": True,
                "runtime_ready": False,
                "runtime_status": "profile_reconfirmation_required",
                "runtime_error": error,
            }
        )


def get_runtime_readiness() -> Dict[str, Any]:
    with _RUNTIME_READINESS_LOCK:
        return dict(_RUNTIME_READINESS_STATE)


@dataclass
class AppLifecycleManager:
    """
    アプリケーションライフサイクル管理マネージャ。

    セットアップ状態の確認・完了を提供する。
    legacy ``profile.json`` には触れず、Authority-owned v4 activation のみを
    setup completion として扱う。
    """

    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    packvm_lifecycle: Any | None = field(default=None, repr=False)
    runtime_capture_factory: RuntimeCaptureFactory | None = field(
        default=None,
        repr=False,
    )
    _activation_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def check_setup_status(self) -> Dict[str, Any]:
        """
        セットアップ状態を確認する。

        Returns:
            {"needs_setup": bool, "reason": str}
        """
        from .bootstrap.profile_capture import (
            active_profile_exists,
            capture_active_profile,
            host_profile_catalog,
            runtime_user_data_root,
        )
        from .profile_definition_store_v4 import ProfileDefinitionStore

        if not active_profile_exists(base_dir=self.base_dir):
            try:
                catalog = host_profile_catalog(base_dir=self.base_dir)
                del catalog
                bootstrap = ProfileDefinitionStore(
                    runtime_user_data_root(self.base_dir)
                ).bootstrap_state()
                defaults_bootstrap_required = bootstrap.get("state") == "template_available"
            except Exception as error:
                logger.error("Host Profile catalog verification failed: %s", error)
                result = {
                    "needs_setup": True,
                    "reason": "host_catalog_verification_failed",
                    "setup_state": "host_verification_denied",
                    "host_catalog_verified": False,
                    "profile_ceremony_available": False,
                    "active_profile_ready": False,
                    "launch_ready": False,
                    "defaults_bootstrap_required": False,
                }
                result.update(get_runtime_readiness())
                return result
            result = {
                "needs_setup": defaults_bootstrap_required,
                "reason": (
                    "explicit_bootstrap_confirmation_required"
                    if defaults_bootstrap_required
                    else "profile_activation_required"
                ),
                "setup_state": (
                    "profile_transaction_required"
                    if defaults_bootstrap_required
                    else "profile_activation_required"
                ),
                "host_catalog_verified": True,
                "profile_ceremony_available": not defaults_bootstrap_required,
                "active_profile_ready": False,
                "launch_ready": False,
                "defaults_bootstrap_required": defaults_bootstrap_required,
            }
            result.update(get_runtime_readiness())
            return result
        try:
            active = capture_active_profile(base_dir=self.base_dir)
            result = {
                "needs_setup": False,
                "reason": "canonical_v4_profile_captured",
                "setup_state": "complete",
                "profile_id": active.resolved.profile["profile_id"],
                "plan_digest": active.resolved.plan["plan_digest"],
                "activation_id": active.activation["activation_id"],
                "host_catalog_verified": True,
                "profile_ceremony_available": True,
                "active_profile_ready": True,
                "launch_ready": True,
                "defaults_bootstrap_required": False,
            }
        except Exception as error:
            from .profile_runtime_port import require_profile_runtime

            logger.error("canonical v4 setup status failed: %s", error)
            if require_profile_runtime().is_reconfirmation_required(error):
                result = {
                    "needs_setup": True,
                    "reason": "profile_reconfirmation_required",
                    "setup_state": "profile_reconfirmation_required",
                    "error_type": type(error).__name__,
                    "denial_diagnostic": str(error),
                    "host_catalog_verified": True,
                    "profile_ceremony_available": True,
                    "active_profile_ready": False,
                    "launch_ready": False,
                    "defaults_bootstrap_required": False,
                }
            else:
                result = {
                    "needs_setup": True,
                    "reason": "canonical_v4_profile_unavailable",
                    "setup_state": "profile_transaction_required",
                    "error_type": type(error).__name__,
                    "denial_diagnostic": str(error),
                    "host_catalog_verified": False,
                    "profile_ceremony_available": False,
                    "active_profile_ready": False,
                    "launch_ready": False,
                    "defaults_bootstrap_required": False,
                }

        result.update(get_runtime_readiness())
        return result

    def activate_bootstrap_profile(self, confirmation: Mapping[str, Any]) -> Any:
        """Commit one Pack-selected activation and construct a restart-only check."""

        from .authority.v4 import AuthorityStore
        from .bootstrap.production_v4 import capture_production_dispatch
        from .bootstrap.profile_capture import (
            capture_bootstrap_profile,
            runtime_user_data_root,
        )
        with self._activation_lock:
            active = capture_bootstrap_profile(
                base_dir=self.base_dir,
                confirmation=confirmation,
            )
            user_data = runtime_user_data_root(self.base_dir)
            capture_factory = self.runtime_capture_factory
            if capture_factory is None:
                raise RuntimeError("application runtime capture composition is unavailable")
            inputs = capture_factory(active)
            session = capture_production_dispatch(
                active,
                bundle_root=inputs.bundle_root,
                ecosystem_root=inputs.ecosystem_root,
                authority_store=AuthorityStore(user_data / "authority" / "v4.sqlite3"),
                packvm_provisioner=inputs.packvm_backend_factory,
                packvm_readiness_reader=(
                    self.packvm_lifecycle.readiness_snapshot
                    if self.packvm_lifecycle is not None
                    else None
                ),
                http_contract_bindings=inputs.contract_bindings,
                activation_snapshot_loader=inputs.activation_snapshot_loader,
                runtime_surface_factory=inputs.runtime_surface_factory,
                capability_binding_snapshot_factory=(inputs.capability_binding_snapshot_factory),
                capability_binding_selector=inputs.capability_binding_selector,
                credential_store_factory=inputs.credential_store_factory,
            )
            return active, session

    def activate_default_profile(self, confirmation: Mapping[str, Any]) -> Any:
        """Compatibility alias for the Pack-selected bootstrap activation."""

        return self.activate_bootstrap_profile(confirmation)

    def complete_setup(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        セットアップを完了する。

        既存の v4 activation を検証する。ユーザー属性の旧 Profile writer
        は production setup の一部ではない。

        Args:
            data: {"username": str, "language": str, "icon": optional, "occupation": optional}

        Returns:
            {"success": bool, "errors": list, ...}
        """
        username = str(data.get("username") or "").strip()
        language = str(data.get("language") or "").strip()
        errors = []
        if not username:
            errors.append("username is required")
        if language not in {"en", "ja"}:
            errors.append("language must be en or ja")
        if errors:
            return {
                "success": False,
                "errors": errors,
                "setup_state": "invalid_request",
            }
        from .bootstrap.profile_capture import capture_active_profile

        try:
            active = capture_active_profile(base_dir=self.base_dir)
        except Exception as error:
            logger.error("canonical v4 setup transaction failed: %s", error)
            return {
                "success": False,
                "errors": ["canonical v4 Profile transaction failed"],
                "setup_state": "profile_transaction_failed",
                "error_type": type(error).__name__,
            }
        return {
            "success": True,
            "errors": [],
            "setup_state": "complete",
            "profile_id": active.resolved.profile["profile_id"],
            "plan_digest": active.resolved.plan["plan_digest"],
            "activation_id": active.activation["activation_id"],
            "restart_required": False,
        }

    def get_health(self) -> Dict[str, Any]:
        """
        ヘルスチェック情報を返す。

        Returns:
            {"status": "ok", "needs_setup": bool}
        """
        status = self.check_setup_status()
        return {
            "status": "error" if status.get("runtime_status") == "error" else "ok",
            "needs_setup": status.get("needs_setup", True),
            "panel_ready": status.get("panel_ready", False),
            "runtime_ready": status.get("runtime_ready", False),
            "runtime_status": status.get("runtime_status", "starting"),
            "runtime_error": status.get("runtime_error"),
            "host_catalog_verified": status.get("host_catalog_verified", False),
            "profile_ceremony_available": status.get("profile_ceremony_available", False),
            "active_profile_ready": status.get("active_profile_ready", False),
            "launch_ready": status.get("launch_ready", False),
            "defaults_bootstrap_required": status.get("defaults_bootstrap_required", False),
        }
