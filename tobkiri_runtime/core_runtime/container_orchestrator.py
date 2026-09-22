"""
container_orchestrator.py - Dockerコンテナオーケストレーション

W18-C: DockerRunBuilder に移行し、セキュリティベースラインを統一。

Packごとのコンテナ管理を行う。
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


from .docker_run_builder import DockerRunBuilder
from .paths import ECOSYSTEM_DIR


@dataclass
class ContainerResult:
    """コンテナ操作結果"""
    success: bool
    container_id: Optional[str] = None
    error: Optional[str] = None


logger = logging.getLogger(__name__)


class ContainerOrchestrator:
    """Dockerコンテナオーケストレータ"""
    
    def __init__(
        self,
        packs_dir: str = ECOSYSTEM_DIR,
        docker_dir: str = "docker/core"
    ):
        self.packs_dir = Path(packs_dir)
        self.docker_dir = Path(docker_dir)
        self._containers: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._docker_available: Optional[bool] = None
    
    def is_docker_available(self) -> bool:
        """Docker利用可能性チェック"""
        if self._docker_available is not None:
            return self._docker_available
        
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10
            )
            self._docker_available = result.returncode == 0
        except Exception:
            self._docker_available = False
        
        return self._docker_available
    
    def start_container(self, pack_id: str, timeout: int = 30) -> ContainerResult:
        """Packのコンテナを起動"""
        if not self.is_docker_available():
            return ContainerResult(success=False, error="Docker not available")
        
        container_name = f"rumi-pack-{pack_id}"
        
        with self._lock:
            if pack_id in self._containers:
                return ContainerResult(success=True, container_id=self._containers[pack_id])
        
        try:
            check = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.ID}}"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if check.stdout.strip():
                subprocess.run(
                    ["docker", "start", container_name],
                    capture_output=True,
                    timeout=timeout
                )
                container_id = check.stdout.strip()
            else:
                # --- DockerRunBuilder でコマンドを構築 ---
                builder = DockerRunBuilder(name=container_name)
                builder.pids_limit(50)
                builder.ulimit("nproc=50:50")
                builder.ulimit("nofile=100:100")
                builder.label("rumi.pack_id", pack_id)
                builder.label("rumi.managed", "true")

                # Egress UDS ソケットマウント
                try:
                    from .egress_proxy import get_uds_egress_proxy_manager
                    _egress_mgr = get_uds_egress_proxy_manager()
                    _ok, _err, _egress_sock = _egress_mgr.ensure_pack_socket(pack_id)
                    # Keep pack containers on DockerRunBuilder's default --network=none.
                    # Windows Docker cannot mount UDS sockets, but enabling bridge
                    # networking for TCP proxy fallback would let pack code bypass
                    # the egress proxy with direct sockets/curl/requests.
                    if _ok and sys.platform != "win32" and _egress_sock:
                        builder.volume(f"{_egress_sock}:/run/rumi/egress.sock:rw")
                        builder.env("RUMI_EGRESS_SOCKET", "/run/rumi/egress.sock")
                except Exception as e:
                    logger.warning("Failed to mount egress socket for %s: %s", pack_id, e)

                # Capability UDS ソケットマウント
                try:
                    from .capability_proxy import get_capability_proxy
                    _cap_proxy = get_capability_proxy()
                    _cap_ok, _cap_err, _cap_sock = _cap_proxy.ensure_principal_socket(pack_id)
                    # Same isolation rule for capability IPC: do not expose a TCP
                    # host path that would require broad container networking.
                    if _cap_ok and sys.platform != "win32" and _cap_sock:
                        builder.volume(f"{_cap_sock}:/run/rumi/capability.sock:rw")
                        builder.env("RUMI_CAPABILITY_SOCKET", "/run/rumi/capability.sock")
                except Exception as e:
                    logger.warning("Failed to mount capability socket for %s: %s", pack_id, e)

                builder.image(f"rumi-pack-{pack_id}:latest")

                docker_cmd = builder.build()
                # --rm を除去し -d を挿入（長寿命コンテナ用）
                docker_cmd = [c for c in docker_cmd if c != "--rm"]
                docker_cmd.insert(2, "-d")

                result = subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                
                if result.returncode != 0:
                    return ContainerResult(success=False, error=result.stderr)
                
                container_id = result.stdout.strip()
            
            with self._lock:
                self._containers[pack_id] = container_id
            
            return ContainerResult(success=True, container_id=container_id)
            
        except subprocess.TimeoutExpired:
            return ContainerResult(success=False, error="Container start timed out")
        except Exception as e:
            return ContainerResult(success=False, error=str(e))
    
    def stop_container(self, pack_id: str) -> ContainerResult:
        """コンテナを停止"""
        container_name = f"rumi-pack-{pack_id}"
        
        try:
            subprocess.run(
                ["docker", "stop", container_name],
                capture_output=True,
                timeout=30
            )
            # 停止後にコンテナを削除（データ残存防止）
            try:
                subprocess.run(
                    ["docker", "rm", container_name],
                    capture_output=True,
                    timeout=10
                )
            except Exception:
                logger.warning("docker rm failed for %s, ignoring", container_name)
            
            with self._lock:
                self._containers.pop(pack_id, None)
            
            return ContainerResult(success=True)
        except Exception as e:
            return ContainerResult(success=False, error=str(e))
    
    def remove_container(self, pack_id: str) -> ContainerResult:
        """コンテナを削除"""
        container_name = f"rumi-pack-{pack_id}"
        
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=30
            )
            
            with self._lock:
                self._containers.pop(pack_id, None)
            
            return ContainerResult(success=True)
        except Exception as e:
            return ContainerResult(success=False, error=str(e))
    
    def list_containers(self) -> List[Dict[str, Any]]:
        """管理中のコンテナ一覧"""
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--filter", "label=rumi.managed=true", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            containers = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    try:
                        containers.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            
            return containers
        except Exception:
            return []



    # ── universal_call container support ──────────────────────
    _UC_DEFAULT_IMAGES = {
        "python": "python:3.11-slim",
        "binary": "alpine:latest",
        "command": "alpine:latest",
    }

    def build_universal_call_command(
        self,
        pack_id: str,
        workspace_dir: str,
        input_file: str,
        filename: str,
        runtime: str = "binary",
        docker_image: str | None = None,
        container_name: str | None = None,
    ) -> list:
        """Build a Docker run command for universal_call execution."""
        import uuid

        image = docker_image or self._UC_DEFAULT_IMAGES.get(runtime, "alpine:latest")
        name = container_name or f"rumi-uc-{pack_id}-{uuid.uuid4().hex[:8]}"

        builder = (
            DockerRunBuilder(name=name)
            .set_pids_limit(100)
            .add_volume(workspace_dir, "/workspace", read_only=True)
            .set_workdir("/workspace")
            .add_label("rumi.pack_id", pack_id)
            .add_label("rumi.type", "universal_call")
            .add_label("rumi.runtime", runtime)
            .set_image(image)
        )

        if runtime == "command":
            builder.command(["sh", filename])
        elif runtime == "python":
            builder.command(["python", filename])
        else:
            builder.command([f"./{filename}"])

        return builder.build()


# グローバル変数（後方互換のため残存。DI コンテナ優先）
_global_orchestrator: Optional[ContainerOrchestrator] = None
_co_lock = threading.Lock()


def get_container_orchestrator() -> ContainerOrchestrator:
    """
    グローバルな ContainerOrchestrator を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        ContainerOrchestrator インスタンス
    """
    from .di_container import get_container
    return get_container().get("container_orchestrator")


def initialize_container_orchestrator() -> ContainerOrchestrator:
    """
    ContainerOrchestrator を初期化する。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Returns:
        初期化済み ContainerOrchestrator インスタンス
    """
    global _global_orchestrator
    with _co_lock:
        _global_orchestrator = ContainerOrchestrator()
    # DI コンテナのキャッシュも更新（_co_lock の外で実行してデッドロック回避）
    from .di_container import get_container
    get_container().set_instance("container_orchestrator", _global_orchestrator)
    return _global_orchestrator


def reset_container_orchestrator() -> "ContainerOrchestrator":
    """
    ContainerOrchestrator をリセットする（テスト用）。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Returns:
        新しい ContainerOrchestrator インスタンス
    """
    global _global_orchestrator
    from .di_container import get_container
    container = get_container()
    new = ContainerOrchestrator()
    with _co_lock:
        _global_orchestrator = new
    container.set_instance("container_orchestrator", new)
    return new
