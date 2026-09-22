"""Privilege ハンドラ Mixin"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...host_privilege_manager import HostPrivilegeManager


class PrivilegeHandlersMixin:
    """ホスト特権操作のハンドラ"""

    host_privilege_manager: HostPrivilegeManager | None

    def _get_privileges(self) -> list:
        if not self.host_privilege_manager:
            return []
        return self.host_privilege_manager.list_privileges()

    def _grant_privilege(self, pack_id: str, privilege_id: str) -> dict:
        if not self.host_privilege_manager:
            return {"success": False, "error": "HostPrivilegeManager not initialized"}
        result = self.host_privilege_manager.grant(pack_id, privilege_id)
        return {"success": result.success, "error": result.error}

    def _execute_privilege(self, pack_id: str, privilege_id: str, params: dict) -> dict:
        if not self.host_privilege_manager:
            return {"success": False, "error": "HostPrivilegeManager not initialized"}
        result = self.host_privilege_manager.execute(pack_id, privilege_id, params)
        return {"success": result.success, "result": result.data, "error": result.error}
