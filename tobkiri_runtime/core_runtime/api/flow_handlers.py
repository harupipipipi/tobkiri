"""Flow 実行 ハンドラ Mixin"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, TYPE_CHECKING
from urllib.parse import unquote

from ._helpers import _log_internal_error, _SAFE_ERROR_MSG
from .api_response import APIResponse
from ..flow_context_security import reserved_flow_context_keys

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    class _FlowInterfaceRegistry:
        def get(self, key: str, strategy: str = "last") -> object: ...
        def list(self) -> dict[str, object]: ...

    class _FlowKernel:
        interface_registry: _FlowInterfaceRegistry

        def execute_flow_sync(
            self,
            flow_id: str,
            inputs: dict[str, object],
            *,
            timeout: float,
        ) -> object: ...

# ---------------------------------------------------------------------------
# flow_id バリデーション: 英数字・アンダースコア・ドット・ハイフン、1〜128文字
# ---------------------------------------------------------------------------
_RE_FLOW_ID = re.compile(r'^(?=.*[a-zA-Z0-9_])[a-zA-Z0-9_.\-]{1,128}$')

# ---------------------------------------------------------------------------
# パス検出: 3階層以上の Unix/Windows ファイルパスを <path> に置換
# ---------------------------------------------------------------------------
_RE_FILE_PATH = re.compile(r'(?:[A-Za-z]:)?(?:[/\\][\w.\-]+){3,}')

# メモリアドレス (例: at 0x7f3a...)
_RE_MEM_ADDR = re.compile(r'\bat\s+0x[0-9a-fA-F]+')

# トレースバック判定用キーワード
_TRACEBACK_PREFIXES = (
    "Traceback (most recent",
    "Traceback(most recent",
    'File "',
    "  File ",
    "During handling of",
    "The above exception",
)


def _is_json_serializable(value: Any) -> bool:
    """値がJSON直列化可能か簡易判定する"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_json_serializable(v) for v in value)
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _is_json_serializable(v)
            for k, v in value.items()
        )
    return False


def _sanitize_error(error_str: Any) -> str:
    """エラー文字列からスタックトレースやファイルパスを除去する。

    * トレースバック行(``File "..."`` 等)を除去
    * 3階層以上のファイルパスを ``<path>`` に置換
    * メモリアドレスを除去
    * 最初の意味のある行を最大 200 文字で返す
    * 何も残らなければ ``_SAFE_ERROR_MSG`` を返す
    """
    if not isinstance(error_str, str) or not error_str.strip():
        return _SAFE_ERROR_MSG

    lines: list[str] = []
    for raw_line in error_str.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        # トレースバック関連行を除去
        if any(stripped.startswith(p) for p in _TRACEBACK_PREFIXES):
            continue
        # Python キャレット行 (構文エラー表示) を除去
        if stripped.lstrip().startswith("^"):
            continue
        # インデントされたコード行 (traceback 内) を除去
        if raw_line.startswith("    ") and not raw_line.startswith("    " * 2 + " "):
            # トレースバック内のソースコード行は4スペースインデント
            # ただし通常のエラーメッセージがインデントされることは稀
            # lines に既にデータがなく、かつインデント行なら traceback の一部とみなす
            if not lines:
                continue
        lines.append(stripped)

    if not lines:
        return _SAFE_ERROR_MSG

    # 最初の意味のある行を採用
    result = lines[-1]  # 通常、例外メッセージは最後の行

    # ファイルパスを置換
    result = _RE_FILE_PATH.sub("<path>", result)
    # メモリアドレスを除去
    result = _RE_MEM_ADDR.sub("", result)
    # 連続空白を正規化
    result = re.sub(r"\s{2,}", " ", result).strip()

    if not result:
        return _SAFE_ERROR_MSG

    # 200文字で切り詰め
    if len(result) > 200:
        result = result[:197] + "..."

    return result


class FlowHandlersMixin:
    kernel: _FlowKernel | None

    if TYPE_CHECKING:
        def _send_response(
            self,
            response: APIResponse,
            status: int = 200,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None: ...
    """Flow 実行 API のハンドラ"""

    _flow_semaphore = None  # 同時実行制御用Semaphore

    # Flow結果から除外する内部オブジェクトキー
    _CTX_OBJECT_KEYS: frozenset[str] = frozenset({
        "diagnostics", "install_journal", "interface_registry",
        "event_bus", "lifecycle", "mount_manager", "registry",
        "active_ecosystem", "permission_manager",
        "function_alias_registry", "flow_composer",
        "vocab_registry", "approval_manager",
        "container_orchestrator", "host_privilege_manager",
        "pack_api_server",
        "store_registry", "unit_registry",
        "secrets_store",
    })

    # Kernel内部キーのプレフィックス — これにマッチするキーはレスポンスから除外する
    # Pack開発者が返す _ プレフィックスキー（_debug 等）は除外しない
    _KERNEL_INTERNAL_PREFIXES: tuple[str, ...] = (
        "_flow_",
        "_kernel_",
        "_step_out.",
        "_current_step",
        "_total_steps",
        "_parent_flow",
        "_principal_id",
        "_flow_control",
        "_error",
        "_flow_defaults",
    )

    @classmethod
    def _get_flow_semaphore(cls) -> threading.Semaphore:
        """同時実行制限用Semaphoreを取得（遅延初期化）"""
        if cls._flow_semaphore is None:
            max_concurrent = int(os.environ.get("RUMI_MAX_CONCURRENT_FLOWS", "10"))
            cls._flow_semaphore = threading.Semaphore(max_concurrent)
        return cls._flow_semaphore

    def _handle_flow_run(self, path: str, body: dict) -> None:
        """POST /api/flows/{flow_id}/run のハンドラ"""
        # APIResponse は pack_api_server で定義されている — self 経由で利用
        from .api_response import APIResponse

        parts = path.split("/")
        # ["", "api", "flows", "{flow_id}", "run"]
        if len(parts) < 5:
            self._send_response(APIResponse(False, error="Invalid flow path"), 400)
            return
        flow_id = unquote(parts[3])

        # --- T-015: flow_id バリデーション ---
        if not flow_id or not flow_id.strip() or not _RE_FLOW_ID.match(flow_id):
            self._send_response(APIResponse(False, error="Invalid flow_id"), 400)
            return

        inputs = body.get("inputs", {})
        if not isinstance(inputs, dict):
            self._send_response(APIResponse(False, error="'inputs' must be an object"), 400)
            return
        reserved_keys = reserved_flow_context_keys(inputs)
        if reserved_keys:
            self._send_response(
                APIResponse(
                    False,
                    error=f"Reserved flow input key(s) are not allowed: {', '.join(reserved_keys)}",
                ),
                400,
            )
            return

        timeout = body.get("timeout", 300)
        if not isinstance(timeout, (int, float)) or timeout != timeout:
            timeout = 300
        timeout = min(max(timeout, 1), 600)

        result = self._run_flow(flow_id, inputs, timeout)
        if result.get("success"):
            self._send_response(APIResponse(True, result))
        else:
            status_code = result.get("status_code", 500)
            self._send_response(APIResponse(False, error=result.get("error")), status_code)

    def _run_flow(self, flow_id: str, inputs: dict, timeout: float) -> dict:
        """
        Flow を実行し結果を返す（共通メソッド）。

        Flow実行API と Pack独自ルートの両方から呼ばれる。
        """
        # ---- T-015: 防御的バリデーション（Pack独自ルートから直接呼ばれる場合に備える） ----
        if not isinstance(flow_id, str) or not _RE_FLOW_ID.match(flow_id):
            return {"success": False, "error": "Invalid flow_id", "status_code": 400}

        if not isinstance(inputs, dict):
            return {"success": False, "error": "'inputs' must be an object", "status_code": 400}
        reserved_keys = reserved_flow_context_keys(inputs)
        if reserved_keys:
            return {
                "success": False,
                "error": f"Reserved flow input key(s) are not allowed: {', '.join(reserved_keys)}",
                "status_code": 400,
            }

        if not isinstance(timeout, (int, float)) or timeout != timeout:
            timeout = 300
        timeout = min(max(float(timeout), 1), 600)

        if self.kernel is None:
            return {"success": False, "error": "Kernel not initialized", "status_code": 503}

        # Flow 存在チェック（InterfaceRegistry 経由）
        ir = getattr(self.kernel, "interface_registry", None)
        if ir is None:
            return {"success": False, "error": "InterfaceRegistry not available", "status_code": 503}

        flow_def = ir.get(f"flow.{flow_id}", strategy="last")
        if flow_def is None:
            return {
                "success": False,
                "error": f"Flow '{flow_id}' not found",
                "status_code": 404,
            }

        # 同時実行制限
        sem = self._get_flow_semaphore()
        acquired = sem.acquire(blocking=False)
        if not acquired:
            return {
                "success": False,
                "error": "Too many concurrent flow executions. Please retry later.",
                "status_code": 429,
            }

        try:
            start_time = time.monotonic()

            ctx = self.kernel.execute_flow_sync(flow_id, inputs, timeout=timeout)

            elapsed = round(time.monotonic() - start_time, 3)

            # エラーチェック
            if isinstance(ctx, dict) and ctx.get("_error"):
                return {
                    "success": False,
                    "error": _sanitize_error(ctx["_error"]),
                    "flow_id": flow_id,
                    "execution_time": elapsed,
                    "status_code": 408 if ctx.get("_flow_timeout") else 500,
                }

            # 結果から内部キーを除外
            result_data = {}
            _pack_underscore_keys: list[str] = []
            if isinstance(ctx, dict):
                for k, v in ctx.items():
                    if k in self._CTX_OBJECT_KEYS:
                        continue
                    if callable(v):
                        continue
                    if not _is_json_serializable(v):
                        continue
                    if any(k.startswith(p) for p in self._KERNEL_INTERNAL_PREFIXES):
                        continue
                    if k.startswith("_"):
                        _pack_underscore_keys.append(k)
                    result_data[k] = v
                if _pack_underscore_keys:
                    logger.warning(
                        "Pack-defined underscore key(s) included in flow result: %s",
                        ", ".join(sorted(_pack_underscore_keys)),
                    )

            # レスポンスサイズ制限 (デフォルト 4MB)
            max_bytes = int(os.environ.get("RUMI_MAX_RESPONSE_BYTES", str(4 * 1024 * 1024)))
            try:
                result_json = json.dumps(result_data, ensure_ascii=False)
                if len(result_json.encode("utf-8")) > max_bytes:
                    logger.warning(
                        f"Flow '{flow_id}' result exceeds {max_bytes} bytes, "
                        f"truncating to keys only"
                    )
                    result_data = {
                        "_truncated": True,
                        "_reason": f"Result exceeded {max_bytes} byte limit",
                        "_keys": sorted(result_data.keys()),
                    }
            except (TypeError, ValueError):
                result_data = {"_error": "Result not JSON serializable"}

            # 監査ログ
            try:
                from ..audit_logger import get_audit_logger
                audit = get_audit_logger()
                audit.log_system_event(
                    event_type="flow_api_execution",
                    success=True,
                    details={
                        "flow_id": flow_id,
                        "execution_time": elapsed,
                        "source": "api",
                    },
                )
            except Exception:
                pass

            return {
                "success": True,
                "flow_id": flow_id,
                "result": result_data,
                "execution_time": elapsed,
            }
        except Exception as e:
            _log_internal_error("run_flow", e)
            return {
                "success": False,
                "error": _SAFE_ERROR_MSG,
                "flow_id": flow_id,
                "status_code": 500,
            }
        finally:
            sem.release()

    def _get_flow_list(self) -> dict:
        """GET /api/flows — 実行可能なFlow一覧を返す"""
        if self.kernel is None:
            return {"flows": [], "error": "Kernel not initialized"}
        ir = getattr(self.kernel, "interface_registry", None)
        if ir is None:
            return {"flows": [], "error": "InterfaceRegistry not available"}
        all_keys = ir.list() or {}
        flows = [
            k[5:] for k in all_keys.keys()
            if k.startswith("flow.")
            and not k.startswith("flow.hooks")
            and not k.startswith("flow.construct")
        ]
        return {"flows": sorted(flows), "count": len(flows)}
