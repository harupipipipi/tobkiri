"""
command_call.py - コマンド実行エンジン

シェルコマンドを安全に実行し結果を返す。
shell=False で実行し、コマンドと引数はリスト形式で渡す。

セキュリティ:
- shell=False（シェルインジェクション防止）
- 出力サイズ制限（1MB）
- タイムアウト制御
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 出力サイズ上限（1MB）
MAX_OUTPUT_SIZE: int = 1 * 1024 * 1024

# デフォルトタイムアウト（秒）
DEFAULT_TIMEOUT: float = 30.0

# 最大タイムアウト（秒）
MAX_TIMEOUT: float = 120.0


@dataclass
class CommandCallResult:
    """コマンド実行結果を表すデータクラス。"""

    success: bool
    output: Optional[Any] = None
    error: Optional[str] = None
    error_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """結果を辞書形式で返す。"""
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "error_type": self.error_type,
        }


def run(input_data: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """シェルコマンドを実行し結果を返す。

    shell=False でコマンドを実行する。stdout を JSON として解析する。

    Args:
        input_data: 実行パラメータ。
            - command (str): 実行するコマンド。
            - args (list): コマンド引数のリスト。
            - timeout (float): タイムアウト秒数（デフォルト 30）。
        context: 実行コンテキスト（principal_id 等）。

    Returns:
        実行結果の辞書。success, output, error, error_type を含む。
    """
    command = input_data.get("command", "")
    args = input_data.get("args", [])
    timeout = input_data.get("timeout", DEFAULT_TIMEOUT)

    # --- command 検証 ---
    if not command or not isinstance(command, str):
        return CommandCallResult(
            success=False,
            error="Missing or invalid command",
            error_type="command_not_found",
        ).to_dict()

    # --- args 検証 ---
    if not isinstance(args, list):
        return CommandCallResult(
            success=False,
            error="args must be a list",
            error_type="execution_error",
        ).to_dict()

    # args の各要素を文字列に変換
    str_args = [str(a) for a in args]

    # --- タイムアウト正規化 ---
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1.0, min(timeout, MAX_TIMEOUT))

    # --- コマンド存在確認 ---
    if shutil.which(command) is None:
        return CommandCallResult(
            success=False,
            error=f"Command not found: {command}",
            error_type="command_not_found",
        ).to_dict()

    # --- コマンドリスト構築（shell=False） ---
    cmd_list = [command] + str_args

    # --- サブプロセス実行 ---
    try:
        proc = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Command call timed out: command=%s, timeout=%s",
            command, timeout,
        )
        return CommandCallResult(
            success=False,
            error=f"Command execution timed out after {timeout}s",
            error_type="timeout",
        ).to_dict()
    except FileNotFoundError:
        return CommandCallResult(
            success=False,
            error=f"Command not found at execution time: {command}",
            error_type="command_not_found",
        ).to_dict()
    except OSError as exc:
        logger.error(
            "Command call OS error: command=%s, error=%s",
            command, exc,
        )
        return CommandCallResult(
            success=False,
            error=f"Execution error: {exc}",
            error_type="execution_error",
        ).to_dict()

    # --- 終了コード確認 ---
    if proc.returncode != 0:
        stderr_snippet = (proc.stderr or "").strip()[:500]
        return CommandCallResult(
            success=False,
            error=f"Command exited with code {proc.returncode}: {stderr_snippet}",
            error_type="execution_error",
        ).to_dict()

    # --- 出力サイズ確認 ---
    stdout = proc.stdout or ""
    if len(stdout.encode("utf-8")) > MAX_OUTPUT_SIZE:
        return CommandCallResult(
            success=False,
            error="Output exceeds size limit (1MB)",
            error_type="execution_error",
        ).to_dict()

    # --- 出力 JSON パース ---
    stdout_stripped = stdout.strip()
    if not stdout_stripped:
        return CommandCallResult(success=True, output=None).to_dict()

    try:
        parsed = json.loads(stdout_stripped)
    except json.JSONDecodeError as exc:
        return CommandCallResult(
            success=False,
            error=f"Command output is not valid JSON: {exc}",
            error_type="invalid_json_output",
        ).to_dict()

    return CommandCallResult(success=True, output=parsed).to_dict()
