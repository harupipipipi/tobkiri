"""Capability Grant ハンドラ Mixin"""
from __future__ import annotations

from .._helpers import _log_internal_error, _SAFE_ERROR_MSG


class CapabilityGrantHandlersMixin:
    """Capability 権限 Grant / Revoke / Batch (G-1, #63) のハンドラ"""

    def _capability_grants_grant(self, principal_id: str, permission_id: str, config=None) -> dict:
        try:
            from ...capability_grant_manager import get_capability_grant_manager
            gm = get_capability_grant_manager()
            gm.grant_permission(principal_id, permission_id, config)
            try:
                from ...audit_logger import get_audit_logger
                audit = get_audit_logger()
                audit.log_permission_event(
                    pack_id=principal_id,
                    permission_type="capability_grant",
                    action="grant",
                    success=True,
                    details={
                        "principal_id": principal_id,
                        "permission_id": permission_id,
                        "has_config": config is not None,
                        "source": "api",
                    },
                )
            except Exception:
                pass
            return {"success": True, "principal_id": principal_id, "permission_id": permission_id, "granted": True}
        except Exception as e:
            _log_internal_error("capability_grants_grant", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _capability_grants_revoke(self, principal_id: str, permission_id: str) -> dict:
        try:
            from ...capability_grant_manager import get_capability_grant_manager
            gm = get_capability_grant_manager()
            gm.revoke_permission(principal_id, permission_id)
            try:
                from ...audit_logger import get_audit_logger
                audit = get_audit_logger()
                audit.log_permission_event(
                    pack_id=principal_id,
                    permission_type="capability_grant",
                    action="revoke",
                    success=True,
                    details={
                        "principal_id": principal_id,
                        "permission_id": permission_id,
                        "source": "api",
                    },
                )
            except Exception:
                pass
            return {"success": True, "principal_id": principal_id, "permission_id": permission_id, "revoked": True}
        except Exception as e:
            _log_internal_error("capability_grants_revoke", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}

    def _capability_grants_list(self, principal_id: str | None = None) -> dict:
        try:
            from ...capability_grant_manager import get_capability_grant_manager
            gm = get_capability_grant_manager()
            if principal_id:
                grant = gm.get_grant(principal_id)
                if grant is None:
                    return {"grants": {}, "count": 0, "principal_id": principal_id}
                g_dict = grant.to_dict() if hasattr(grant, "to_dict") else grant
                return {"grants": {principal_id: g_dict}, "count": 1, "principal_id": principal_id}
            else:
                all_grants = gm.get_all_grants()
                result = {}
                for pid, g in all_grants.items():
                    result[pid] = g.to_dict() if hasattr(g, "to_dict") else g
                return {"grants": result, "count": len(result)}
        except Exception as e:
            _log_internal_error("capability_grants_list", e)
            return {"grants": {}, "error": _SAFE_ERROR_MSG}

    def _capability_grants_batch(self, grants_list: list) -> dict:
        """POST /api/capability/grants/batch"""
        try:
            from ...capability_grant_manager import get_capability_grant_manager
            gm = get_capability_grant_manager()
            result = gm.batch_grant(grants_list)
            try:
                from ...audit_logger import get_audit_logger
                audit = get_audit_logger()
                audit.log_permission_event(
                    pack_id="batch",
                    permission_type="capability_grant",
                    action="batch_grant",
                    success=True,
                    details={
                        "requested_count": len(grants_list),
                        "granted_count": result.granted_count,
                        "failed_count": result.failed_count,
                        "source": "api",
                    },
                )
            except Exception:
                pass
            return {
                "success": result.success,
                "results": result.results,
                "granted_count": result.granted_count,
                "failed_count": result.failed_count,
            }
        except Exception as e:
            _log_internal_error("capability_grants_batch", e)
            return {"success": False, "error": _SAFE_ERROR_MSG}
