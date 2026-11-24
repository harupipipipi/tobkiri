"""
docker_capability.py - DockerCapabilityHandler

Pack が Docker コンテナを実行できる Capability を提供する。
Grant config に基づくセキュリティ制約を適用した上で
DockerRunBuilder 経由でコンテナを実行し、結果を返す。

セキュリティ不変条件（Grant でも緩和不可）:
  --cap-drop=ALL, --security-opt=no-new-privileges:true,
  --privileged 禁止, --cap-add 禁止,
  /var/run/docker.sock マウント禁止,
  ホスト PID/IPC/NET namespace 共有禁止

Grant config で制御可能:
  allowed_images, max_memory, max_cpus, max_pids,
  network_allowed, max_containers, max_execution_time, env_blacklist
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
import threading
import uuid
from typing import Any, Dict, List, Literal, Optional

_this_module = sys.modules.get(__name__)
if _this_module is not None:
    if __name__.startswith("tobkiri_runtime."):
        sys.modules.setdefault(__name__.removeprefix("tobkiri_runtime."), _this_module)
    else:
        sys.modules.setdefault(f"tobkiri_runtime.{__name__}", _this_module)

def _docker_run_builder_class():
    modules = []
    for module_name in (
        "core_runtime.docker_run_builder",
        "tobkiri_runtime.core_runtime.docker_run_builder",
    ):
        module = sys.modules.get(module_name)
        if module is not None and module not in modules:
            modules.append(module)
    for module in modules:
        builder_cls = getattr(module, "DockerRunBuilder", None)
        original_build = getattr(module, "_ORIGINAL_DOCKER_RUN_BUILD", None)
        if (
            builder_cls is not None
            and original_build is not None
            and getattr(builder_cls, "build", None) is not original_build
        ):
            return builder_cls
    for module in modules:
        if hasattr(module, "DockerRunBuilder"):
            return module.DockerRunBuilder
    from .docker_run_builder import DockerRunBuilder

    return DockerRunBuilder

class DockerCapabilityHandler:
    """Pack からの docker.run リクエストを検証・実行するハンドラ。"""

    # ------------------------------------------------------------------ #
    # 絶対上限（Grant config でも超えられない）
    # ------------------------------------------------------------------ #
    ABSOLUTE_MAX_MEMORY = "1g"
    ABSOLUTE_MAX_CPUS = "2.0"
    ABSOLUTE_MAX_PIDS = 200
    ABSOLUTE_MAX_CONTAINERS = 5
    ABSOLUTE_MAX_EXECUTION_TIME = 600

    # ------------------------------------------------------------------ #
    # デフォルト値（Grant config で指定がない場合）
    # ------------------------------------------------------------------ #
    DEFAULT_MEMORY = "256m"
    DEFAULT_CPUS = "0.5"
    DEFAULT_PIDS = 50
    DEFAULT_MAX_CONTAINERS = 3
    DEFAULT_EXECUTION_TIME = 60

    # ------------------------------------------------------------------ #
    # ハードコード禁止環境変数
    # ------------------------------------------------------------------ #
    HARDCODED_ENV_BLACKLIST_PATTERNS: List[str] = [
        "RUMI_*",
        "AWS_*",
        "DOCKER_*",
    ]
    HARDCODED_ENV_EXACT_BLOCK: set = {"HOME", "PATH"}

    # ------------------------------------------------------------------ #
    # Post-build assertion 禁止パターン (W23-A)
    # ------------------------------------------------------------------ #
    FORBIDDEN_CMD_PATTERNS: List[str] = [
        "--privileged",
        "--cap-add",
        "/var/run/docker.sock",
        "//./pipe/docker_engine",
        "--pid=host",
        "--ipc=host",
        "--net=host",
        "--network=host",
    ]

    _IMAGE_NAME_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    _IMAGE_DOMAIN_COMPONENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
    _IMAGE_DOMAIN = (
        rf"{_IMAGE_DOMAIN_COMPONENT}(?:\.{_IMAGE_DOMAIN_COMPONENT})*"
        r"(?::[0-9]+)?"
    )
    _IMAGE_TAG = r"[\w][\w.-]{0,127}"
    _IMAGE_DIGEST = (
        r"[A-Za-z][A-Za-z0-9]*(?:[+._-][A-Za-z][A-Za-z0-9]*)*:"
        r"[0-9A-Fa-f]{32,}"
    )
    _IMAGE_REFERENCE_RE = re.compile(
        rf"^(?=.{{1,255}}(?::(?:{_IMAGE_TAG}))?(?:@(?:{_IMAGE_DIGEST}))?$)"
        rf"(?:{_IMAGE_DOMAIN}/)?"
        rf"{_IMAGE_NAME_COMPONENT}(?:/{_IMAGE_NAME_COMPONENT})*"
        rf"(?::{_IMAGE_TAG})?(?:@{_IMAGE_DIGEST})?$"
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_containers: Dict[str, str] = {}  # name -> principal_id

    # ================================================================== #
    # ユーティリティ
    # ================================================================== #

    @staticmethod
    def _parse_memory_bytes(mem_str: str) -> int:
        """メモリ文字列をバイト数に変換する。

        対応形式: "256m", "1g", "512k", "1024" (バイト)
        """
        mem_str = mem_str.strip().lower()
        match = re.fullmatch(r"(\d+)\s*([kmg])?", mem_str)
        if not match:
            raise ValueError(f"Invalid memory format: {mem_str}")
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "k":
            return value * 1024
        if unit == "m":
            return value * 1024 * 1024
        if unit == "g":
            return value * 1024 * 1024 * 1024
        return value

    @staticmethod
    def _format_memory(nbytes: int) -> str:
        """バイト数をメモリ文字列に変換する。"""
        if nbytes >= 1024 * 1024 * 1024 and nbytes % (1024 * 1024 * 1024) == 0:
            return f"{nbytes // (1024 * 1024 * 1024)}g"
        if nbytes >= 1024 * 1024 and nbytes % (1024 * 1024) == 0:
            return f"{nbytes // (1024 * 1024)}m"
        if nbytes >= 1024 and nbytes % 1024 == 0:
            return f"{nbytes // 1024}k"
        return str(nbytes)

    @classmethod
    def _is_valid_image_reference(cls, image: Any) -> bool:
        """Docker image referenceとして安全な形式か検証する。

        Docker CLI は ``docker run`` の image 位置でも先頭 ``-`` の値を
        オプションとして解釈し得るため、grant の glob 許可より前に
        正規の image reference だけを受け付ける。
        """
        if not isinstance(image, str):
            return False
        if not image or image.startswith("-") or image.strip() != image:
            return False
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in image):
            return False
        return cls._IMAGE_REFERENCE_RE.fullmatch(image) is not None

    @staticmethod
    def _is_image_allowed(image: str, allowed_patterns: List[str]) -> bool:
        """イメージ名が許可パターンリストにマッチするか判定する。"""
        for pattern in allowed_patterns:
            if fnmatch.fnmatch(image, pattern):
                return True
        return False

    def _filter_env(
        self, env: Optional[Dict[str, str]], grant_config: dict
    ) -> Dict[str, str]:
        """環境変数をフィルタリングする。

        ハードコード禁止パターン + grant_config の env_blacklist を適用。
        """
        if not env:
            return {}

        blacklist_patterns = list(self.HARDCODED_ENV_BLACKLIST_PATTERNS)
        extra = grant_config.get("env_blacklist", [])
        if extra:
            blacklist_patterns.extend(extra)

        filtered: Dict[str, str] = {}
        for key, value in env.items():
            if key in self.HARDCODED_ENV_EXACT_BLOCK:
                continue
            blocked = False
            for pattern in blacklist_patterns:
                if fnmatch.fnmatch(key, pattern):
                    blocked = True
                    break
            if not blocked:
                filtered[key] = value
        return filtered

    def _effective_memory(
        self, args_memory: Optional[str], grant_config: dict
    ) -> str:
        """実効メモリ値を計算する。

        min(requested_or_default, grant_max, absolute_max)
        """
        absolute_max = self._parse_memory_bytes(self.ABSOLUTE_MAX_MEMORY)
        grant_max_str = grant_config.get("max_memory", self.DEFAULT_MEMORY)
        grant_max = min(self._parse_memory_bytes(grant_max_str), absolute_max)

        if args_memory:
            requested = self._parse_memory_bytes(args_memory)
            effective = min(requested, grant_max)
        else:
            effective = min(
                self._parse_memory_bytes(self.DEFAULT_MEMORY), grant_max
            )

        return self._format_memory(effective)

    def _effective_cpus(self, grant_config: dict) -> str:
        """実効 CPU 値を計算する。"""
        absolute_max = float(self.ABSOLUTE_MAX_CPUS)
        grant_max = float(grant_config.get("max_cpus", self.DEFAULT_CPUS))
        return str(min(grant_max, absolute_max))

    def _effective_pids(self, grant_config: dict) -> int:
        """実効 PID 上限を計算する。"""
        grant_max = int(grant_config.get("max_pids", self.DEFAULT_PIDS))
        return min(grant_max, self.ABSOLUTE_MAX_PIDS)

    def _effective_timeout(
        self, args_timeout: Optional[int], grant_config: dict
    ) -> int:
        """実効タイムアウトを計算する。"""
        absolute_max = self.ABSOLUTE_MAX_EXECUTION_TIME
        grant_max = min(
            int(grant_config.get("max_execution_time", self.DEFAULT_EXECUTION_TIME)),
            absolute_max,
        )
        if args_timeout is not None:
            return min(int(args_timeout), grant_max)
        return grant_max

    def _max_containers(self, grant_config: dict) -> int:
        """実効同時コンテナ上限を計算する。"""
        grant_max = int(
            grant_config.get("max_containers", self.DEFAULT_MAX_CONTAINERS)
        )
        return min(grant_max, self.ABSOLUTE_MAX_CONTAINERS)

    def _generate_container_name(self, principal_id: str) -> str:
        """コンテナ名を生成する。

        形式: rumi-cap-{principal_id[:20]}-{uuid[:12]}
        """
        short_principal = principal_id[:20]
        short_uuid = uuid.uuid4().hex[:12]
        return f"rumi-cap-{short_principal}-{short_uuid}"

    def _count_active(self, principal_id: str) -> int:
        """指定 principal のアクティブコンテナ数を返す。"""
        return sum(
            1 for pid in self._active_containers.values() if pid == principal_id
        )

    def _verify_ownership(
        self, principal_id: str, container_name: str
    ) -> Optional[str]:
        """コンテナの所有権を検証する。

        Returns:
            None: 検証成功
            str: エラーメッセージ（検証失敗）
        """
        with self._lock:
            owner = self._active_containers.get(container_name)
        if owner is None:
            return f"Container not found: {container_name}"
        if owner != principal_id:
            return f"Access denied: container {container_name} is not owned by {principal_id}"
        return None

    def _check_post_build_assertions(
        self, cmd: List[str], principal_id: str, container_name: str
    ) -> Optional[dict]:
        """build() 結果に禁止パターンが含まれていないか検証する。

        多重防御: DockerRunBuilder がこれらを生成しないことは
        分かっているが、将来の変更に対する安全策。
        """
        for token in cmd:
            for pattern in self.FORBIDDEN_CMD_PATTERNS:
                if pattern in token:
                    self._audit_log(
                        "critical",
                        "docker.run.post_build_assertion_failed",
                        False,
                        principal_id,
                        {
                            "forbidden_pattern": pattern,
                            "token": token,
                            "container_name": container_name,
                        },
                    )
                    return {
                        "error": (
                            "Post-build assertion failed: "
                            f"forbidden pattern '{pattern}' detected"
                        )
                    }
        return None

    def _audit_log(
        self,
        severity: Literal["info", "warning", "error", "critical"],
        action: str,
        success: bool,
        principal_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """監査ログを記録する（audit_logger がなくても動作する）。"""
        try:
            from .di_container import get_container

            audit = get_container().get_or_none("audit_logger")
            if audit is None:
                return
            from .audit_logger import AuditEntry

            entry = AuditEntry(
                ts=audit._now_ts(),
                category="security",
                severity=severity,
                action=action,
                success=success,
                owner_pack=principal_id,
                details=details or {},
            )
            audit.log(entry)
        except Exception:
            pass

    # ================================================================== #
    # メインハンドラ
    # ================================================================== #

    def handle_run(
        self, principal_id: str, args: dict, grant_config: dict
    ) -> dict:
        """Pack からの docker.run リクエストを処理する。

        Args:
            principal_id: リクエスト元 Pack の識別子
            args: Pack からのリクエスト引数
                - image (str): 必須
                - command (list[str]): 必須
                - memory (str): オプション
                - timeout (int): オプション
                - env (dict): オプション
                - working_dir (str): オプション
            grant_config: Grant config（セキュリティ制約）

        Returns:
            dict: exit_code, stdout, stderr, container_name (, error)
        """
        # ------------------------------------------------------------ #
        # 1. 入力バリデーション
        # ------------------------------------------------------------ #
        image = args.get("image")
        command = args.get("command")
        if not image or not command:
            self._audit_log(
                "warning",
                "docker.run.validation_failed",
                False,
                principal_id,
                {"reason": "image and command are required"},
            )
            return {"error": "image and command are required"}

        # ------------------------------------------------------------ #
        # 2. イメージ許可チェック
        # ------------------------------------------------------------ #
        if not self._is_valid_image_reference(image):
            self._audit_log(
                "warning",
                "docker.run.image_rejected",
                False,
                principal_id,
                {"image": image, "reason": "invalid_image_reference"},
            )
            return {"error": f"Invalid image reference: {image}"}

        allowed_images = grant_config.get("allowed_images", [])
        if not allowed_images or not self._is_image_allowed(image, allowed_images):
            self._audit_log(
                "warning",
                "docker.run.image_rejected",
                False,
                principal_id,
                {"image": image, "allowed_images": allowed_images},
            )
            return {"error": f"Image not allowed: {image}"}

        # ------------------------------------------------------------ #
        # 3. 同時コンテナ数チェック
        # ------------------------------------------------------------ #
        max_cont = self._max_containers(grant_config)
        container_name = self._generate_container_name(principal_id)

        with self._lock:
            current_count = self._count_active(principal_id)
            if current_count >= max_cont:
                self._audit_log(
                    "warning",
                    "docker.run.container_limit",
                    False,
                    principal_id,
                    {"count": current_count, "max": max_cont},
                )
                return {
                    "error": (
                        f"Container limit reached: {current_count}/{max_cont}"
                    )
                }
            self._active_containers[container_name] = principal_id

        try:
            # -------------------------------------------------------- #
            # 4. リソース制限の計算
            # -------------------------------------------------------- #
            eff_memory = self._effective_memory(args.get("memory"), grant_config)
            eff_cpus = self._effective_cpus(grant_config)
            eff_pids = self._effective_pids(grant_config)
            eff_timeout = self._effective_timeout(
                args.get("timeout"), grant_config
            )

            # -------------------------------------------------------- #
            # 5. 環境変数フィルタ
            # -------------------------------------------------------- #
            filtered_env = self._filter_env(args.get("env"), grant_config)

            # -------------------------------------------------------- #
            # 6. DockerRunBuilder でコマンド構築
            # -------------------------------------------------------- #
            DockerRunBuilder = _docker_run_builder_class()
            builder = DockerRunBuilder(name=container_name)

            # メモリ/CPU をインスタンス属性で上書き
            builder.DEFAULT_MEMORY = eff_memory
            builder.DEFAULT_MEMORY_SWAP = eff_memory
            builder.DEFAULT_CPUS = eff_cpus

            builder.pids_limit(eff_pids)

            # ネットワーク
            if grant_config.get("network_allowed", False):
                builder.network("bridge")

            # 環境変数
            for key, value in filtered_env.items():
                builder.env(key, str(value))

            # ワーキングディレクトリ
            working_dir = args.get("working_dir")
            if working_dir:
                builder.workdir(working_dir)

            # ラベル
            builder.label("rumi.capability", "docker")
            builder.label("rumi.principal", principal_id[:64])

            # イメージ + コマンド
            builder.image(image)
            builder.command(list(command))

            cmd = builder.build()

            # -------------------------------------------------------- #
            # 6.5 Post-build assertion (W23-A)
            # -------------------------------------------------------- #
            assertion_error = self._check_post_build_assertions(
                cmd, principal_id, container_name
            )
            if assertion_error:
                return assertion_error


            # -------------------------------------------------------- #
            # 7. 監査ログ（実行前）
            # -------------------------------------------------------- #
            self._audit_log(
                "info",
                "docker.run",
                True,
                principal_id,
                {
                    "image": image,
                    "command": command,
                    "container_name": container_name,
                },
            )

            # -------------------------------------------------------- #
            # 8. コンテナ実行
            # -------------------------------------------------------- #
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=eff_timeout,
            )

            return {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "container_name": container_name,
            }

        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "Execution timed out",
                "container_name": container_name,
                "error": "timeout",
            }
        except Exception as e:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "container_name": container_name,
                "error": str(e),
            }
        finally:
            with self._lock:
                self._active_containers.pop(container_name, None)

    # ================================================================== #
    # handle_exec (W23-A)
    # ================================================================== #

    def handle_exec(
        self, principal_id: str, args: dict, grant_config: dict
    ) -> dict:
        """実行中コンテナ内でコマンドを実行する。

        Args:
            principal_id: リクエスト元の識別子
            args:
                - container_name (str): 必須
                - command (list[str]): 必須
                - timeout (int): オプション (デフォルト 30)
                - working_dir (str): オプション
            grant_config: Grant config

        Returns:
            dict: exit_code, stdout, stderr (, error)
        """
        container_name = args.get("container_name")
        command = args.get("command")
        if not container_name or not command:
            self._audit_log(
                "warning",
                "docker.exec.validation_failed",
                False,
                principal_id,
                {"reason": "container_name and command are required"},
            )
            return {"error": "container_name and command are required"}

        ownership_error = self._verify_ownership(principal_id, container_name)
        if ownership_error:
            self._audit_log(
                "warning",
                "docker.exec.ownership_denied",
                False,
                principal_id,
                {"container_name": container_name, "reason": ownership_error},
            )
            return {"error": ownership_error}

        try:
            timeout = int(args.get("timeout", 30))
            cmd = ["docker", "exec"]
            working_dir = args.get("working_dir")
            if working_dir:
                cmd.extend(["-w", working_dir])
            cmd.append(container_name)
            cmd.extend(list(command))

            self._audit_log(
                "info",
                "docker.exec",
                True,
                principal_id,
                {
                    "container_name": container_name,
                    "command": command,
                },
            )

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            return {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "Execution timed out",
                "error": "timeout",
            }
        except Exception as e:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "error": str(e),
            }

    # ================================================================== #
    # handle_stop (W23-A)
    # ================================================================== #

    def handle_stop(
        self, principal_id: str, args: dict, grant_config: dict
    ) -> dict:
        """コンテナを停止する。

        Args:
            principal_id: リクエスト元の識別子
            args:
                - container_name (str): 必須
                - timeout (int): オプション (デフォルト 10)
            grant_config: Grant config

        Returns:
            dict: stopped, container_name (, error)
        """
        container_name = args.get("container_name")
        if not container_name:
            self._audit_log(
                "warning",
                "docker.stop.validation_failed",
                False,
                principal_id,
                {"reason": "container_name is required"},
            )
            return {"error": "container_name is required"}

        ownership_error = self._verify_ownership(principal_id, container_name)
        if ownership_error:
            self._audit_log(
                "warning",
                "docker.stop.ownership_denied",
                False,
                principal_id,
                {"container_name": container_name, "reason": ownership_error},
            )
            return {"error": ownership_error}

        try:
            timeout = int(args.get("timeout", 10))
            cmd = ["docker", "stop", f"--time={timeout}", container_name]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 30,
            )

            if result.returncode != 0:
                error = result.stderr.strip() or "docker stop failed"
                self._audit_log(
                    "warning",
                    "docker.stop.failed",
                    False,
                    principal_id,
                    {"container_name": container_name, "error": error},
                )
                return {
                    "stopped": False,
                    "container_name": container_name,
                    "error": error,
                }

            with self._lock:
                self._active_containers.pop(container_name, None)

            self._audit_log(
                "info",
                "docker.stop",
                True,
                principal_id,
                {"container_name": container_name},
            )

            return {
                "stopped": True,
                "container_name": container_name,
            }

        except subprocess.TimeoutExpired:
            return {
                "stopped": False,
                "container_name": container_name,
                "error": "timeout",
            }
        except Exception as e:
            return {
                "stopped": False,
                "container_name": container_name,
                "error": str(e),
            }

    # ================================================================== #
    # handle_logs (W23-A)
    # ================================================================== #

    def handle_logs(
        self, principal_id: str, args: dict, grant_config: dict
    ) -> dict:
        """コンテナのログを取得する。

        Args:
            principal_id: リクエスト元の識別子
            args:
                - container_name (str): 必須
                - tail (int): オプション (デフォルト 100)
                - since (str): オプション (Docker --since 形式)
            grant_config: Grant config

        Returns:
            dict: stdout, stderr (, error)
        """
        container_name = args.get("container_name")
        if not container_name:
            self._audit_log(
                "warning",
                "docker.logs.validation_failed",
                False,
                principal_id,
                {"reason": "container_name is required"},
            )
            return {"error": "container_name is required"}

        ownership_error = self._verify_ownership(principal_id, container_name)
        if ownership_error:
            self._audit_log(
                "warning",
                "docker.logs.ownership_denied",
                False,
                principal_id,
                {"container_name": container_name, "reason": ownership_error},
            )
            return {"error": ownership_error}

        try:
            tail = int(args.get("tail", 100))
            cmd = ["docker", "logs", f"--tail={tail}"]

            since = args.get("since")
            if since:
                cmd.append(f"--since={since}")

            cmd.append(container_name)

            self._audit_log(
                "info",
                "docker.logs",
                True,
                principal_id,
                {"container_name": container_name, "tail": tail},
            )

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": "Log retrieval timed out",
                "error": "timeout",
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "error": str(e),
            }

    # ================================================================== #
    # handle_list (W23-A)
    # ================================================================== #

    def handle_list(
        self, principal_id: str, args: dict, grant_config: dict
    ) -> dict:
        """principal が所有するコンテナ一覧を返す。

        Args:
            principal_id: リクエスト元の識別子
            args: 不要（空 dict で OK）
            grant_config: Grant config

        Returns:
            dict: containers (list of {name, status})
        """
        with self._lock:
            containers = [
                {"name": name, "status": "running"}
                for name, owner in self._active_containers.items()
                if owner == principal_id
            ]

        self._audit_log(
            "info",
            "docker.list",
            True,
            principal_id,
            {"count": len(containers)},
        )

        return {"containers": containers}
