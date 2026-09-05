"""Pack ライフサイクル ハンドラ Mixin"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING
from ._helpers import _log_internal_error, _SAFE_ERROR_MSG

if TYPE_CHECKING:
    from ..approval_manager import ApprovalManager
    from ..container_orchestrator import ContainerOrchestrator
    from ..host_privilege_manager import HostPrivilegeManager

logger = logging.getLogger(__name__)


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

        # --- IR cleanup (W17-C) ---
        try:
            ir = getattr(self, "kernel", None)
            if ir is not None:
                ir = getattr(ir, "interface_registry", None)
            if ir is None:
                ir = getattr(self, "interface_registry", None)
            if ir is not None:
                steps["ir_cleanup"] = None
                all_keys = ir.list(include_meta=True)
                removed_count = 0
                for key, info in all_keys.items():
                    meta = info.get("last_meta") or {}
                    owner = (
                        meta.get("owner_pack")
                        or meta.get("pack_id")
                        or meta.get("source")
                        or meta.get("_source_pack_id")
                        or meta.get("registered_by")
                    )
                    if owner == pack_id:
                        ir.unregister(
                            key,
                            lambda entry, _pid=pack_id: (
                                entry.get("meta", {}).get("owner_pack") == _pid
                                or entry.get("meta", {}).get("pack_id") == _pid
                                or entry.get("meta", {}).get("source") == _pid
                                or entry.get("meta", {}).get("_source_pack_id") == _pid
                                or entry.get("meta", {}).get("registered_by") == _pid
                            ),
                        )
                        removed_count += 1
                steps["ir_cleanup"] = True
                if removed_count > 0:
                    logger.info(
                        "Uninstall %s: removed %d IR key(s)", pack_id, removed_count
                    )
            else:
                steps["ir_cleanup"] = None  # IR not available
        except Exception as e:
            steps["ir_cleanup"] = False
            errors.append({"step": "ir_cleanup", "error": str(e)})
            _log_internal_error("uninstall_pack.ir_cleanup", e)

        # --- Network Grant revoke (W17-C) ---
        try:
            from ..network_grant_manager import get_network_grant_manager
            ngm = get_network_grant_manager()
            ngm.revoke_network_access(pack_id, reason=f"Pack {pack_id} uninstalled")
            steps["network_grant_revoke"] = True
        except Exception as e:
            steps["network_grant_revoke"] = False
            errors.append({"step": "network_grant_revoke", "error": str(e)})
            _log_internal_error("uninstall_pack.network_grant_revoke", e)

        # --- Capability Handler cleanup (W17-C) ---
        try:
            from ..capability_installer import get_capability_installer
            ci = get_capability_installer()
            if hasattr(ci, "remove_handlers_for_pack"):
                ci.remove_handlers_for_pack(pack_id)
                steps["capability_handler_cleanup"] = True
            else:
                # Method not yet implemented — mark installed candidates
                # belonging to this pack as failed so they are re-scanned.
                removed = 0
                with ci._lock:
                    for _key, item in list(ci._index_items.items()):
                        if item.candidate and item.candidate.pack_id == pack_id:
                            from ..capability_models import CandidateStatus
                            item.status = CandidateStatus.FAILED
                            item.last_error = f"Pack {pack_id} uninstalled"
                            item.last_event_ts = ci._now_ts()
                            removed += 1
                    if removed > 0:
                        ci._save_index()
                steps["capability_handler_cleanup"] = True
        except Exception as e:
            steps["capability_handler_cleanup"] = False
            errors.append({"step": "capability_handler_cleanup", "error": str(e)})
            _log_internal_error("uninstall_pack.capability_handler_cleanup", e)

        success = len(errors) == 0
        return {"success": success, "pack_id": pack_id, "steps": steps, "errors": errors}

    def _pack_import(self, source_path: str, notes: str = "") -> dict:
        try:
            from ..pack_importer import get_pack_importer
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
            from ..pack_importer import get_pack_importer
            importer = get_pack_importer()
            meta = importer.get_staging_meta(staging_id)
            if meta is None:
                return {"success": False, "error": f"Staging not found: {staging_id}"}

            from ..pack_applier import get_pack_applier
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
