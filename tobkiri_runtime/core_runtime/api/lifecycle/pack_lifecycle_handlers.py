"""Pack ライフサイクル ハンドラ Mixin"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .._helpers import _log_internal_error, _SAFE_ERROR_MSG

if TYPE_CHECKING:
    from ...approval_manager import ApprovalManager
    from ...container_orchestrator import ContainerOrchestrator
    from ...host_privilege_manager import HostPrivilegeManager


class PackLifecycleHandlersMixin:
    """Pack アンインストール / インポート / 適用 のハンドラ"""

    approval_manager: ApprovalManager | None
    container_orchestrator: ContainerOrchestrator | None
    host_privilege_manager: HostPrivilegeManager | None

    def _uninstall_pack(self, pack_id: str) -> dict:
        # --- バリデーション ---
        if not isinstance(pack_id, str) or not pack_id.strip():
            return {
                "success": False,
                "pack_id": pack_id if isinstance(pack_id, str) else str(pack_id),
                "steps": {},
                "errors": [{"step": "validation", "error": "pack_id must be a non-empty string"}],
            }

        steps: dict[str, Any] = {
            "container_stop": None,
            "container_remove": None,
            "approval_remove": None,
            "privilege_revoke": None,
        }
        errors: list[dict[str, str]] = []

        # --- container stop ---
        if self.container_orchestrator:
            try:
                self.container_orchestrator.stop_container(pack_id)
                steps["container_stop"] = True
            except Exception as e:
                steps["container_stop"] = False
                errors.append({"step": "container_stop", "error": str(e)})
                _log_internal_error("uninstall_pack.container_stop", e)

        # --- container remove ---
        if self.container_orchestrator:
            try:
                self.container_orchestrator.remove_container(pack_id)
                steps["container_remove"] = True
            except Exception as e:
                steps["container_remove"] = False
                errors.append({"step": "container_remove", "error": str(e)})
                _log_internal_error("uninstall_pack.container_remove", e)

        # --- approval remove ---
        if self.approval_manager:
            try:
                self.approval_manager.remove_approval(pack_id)
                steps["approval_remove"] = True
            except Exception as e:
                steps["approval_remove"] = False
                errors.append({"step": "approval_remove", "error": str(e)})
                _log_internal_error("uninstall_pack.approval_remove", e)

        # --- privilege revoke ---
        if self.host_privilege_manager:
            try:
                self.host_privilege_manager.revoke_all(pack_id)
                steps["privilege_revoke"] = True
            except Exception as e:
                steps["privilege_revoke"] = False
                errors.append({"step": "privilege_revoke", "error": str(e)})
                _log_internal_error("uninstall_pack.privilege_revoke", e)

        success = len(errors) == 0
        return {"success": success, "pack_id": pack_id, "steps": steps, "errors": errors}

    def _pack_import(self, source_path: str, notes: str = "") -> dict:
        try:
            from ...pack_importer import get_pack_importer
            importer = get_pack_importer()
            result = importer.import_pack(source_path, notes=notes)
            return result.to_dict()
        except Exception as e:
            _log_internal_error("pack_import", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _pack_apply_actor(self, actor: str | None = None) -> str:
        if actor:
            return str(actor)
        principal = getattr(self, "_authenticated_principal", None)
        principal_id = getattr(principal, "principal_id", None)
        return str(principal_id or "api_user")

    def _pack_apply(
        self,
        staging_id: str,
        mode: str = "replace",
        actor: str | None = None,
    ) -> dict:
        try:
            from ...pack_importer import get_pack_importer
            importer = get_pack_importer()
            meta = importer.get_staging_meta(staging_id)
            if meta is None:
                return {"success": False, "error": f"Staging not found: {staging_id}"}

            from ...pack_applier import get_pack_applier
            applier = get_pack_applier()
            result = applier.apply(
                staging_id,
                mode=mode,
                actor=self._pack_apply_actor(actor),
            )
            if isinstance(result, dict):
                return result
            return result.to_dict()
        except Exception as e:
            _log_internal_error("pack_apply", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}
