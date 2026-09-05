"""Container / Docker ハンドラ Mixin"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...approval_manager import ApprovalManager
    from ...container_orchestrator import ContainerOrchestrator


class ContainerHandlersMixin:
    """コンテナ操作 + Docker ステータスのハンドラ"""

    approval_manager: ApprovalManager | None
    container_orchestrator: ContainerOrchestrator | None

    def _get_containers(self) -> list:
        if not self.container_orchestrator:
            return []
        return self.container_orchestrator.list_containers()

    def _start_container(self, pack_id: str) -> dict:
        if not self.container_orchestrator:
            return {"success": False, "error": "ContainerOrchestrator not initialized"}

        if self.approval_manager:
            from ...approval_manager import PackStatus
            status = self.approval_manager.get_status(pack_id)
            if status not in (PackStatus.APPROVED, "approved"):
                return {"success": False, "error": f"Pack not approved: {status}", "status_code": 403}

        result = self.container_orchestrator.start_container(pack_id)
        return {"success": result.success, "container_id": result.container_id, "error": result.error}

    def _stop_container(self, pack_id: str) -> dict:
        if not self.container_orchestrator:
            return {"success": False}
        result = self.container_orchestrator.stop_container(pack_id)
        return {"success": result.success}

    def _remove_container(self, pack_id: str) -> dict:
        if not self.container_orchestrator:
            return {"success": False}
        self.container_orchestrator.stop_container(pack_id)
        self.container_orchestrator.remove_container(pack_id)
        return {"success": True, "pack_id": pack_id}

    def _get_docker_status(self) -> dict:
        if self.container_orchestrator:
            available = self.container_orchestrator.is_docker_available()
        else:
            import subprocess
            try:
                subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=5)
                available = True
            except Exception:
                available = False

        return {"available": available, "required": True}
