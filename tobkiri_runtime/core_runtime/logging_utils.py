"""
logging_utils.py - 構造化ログ基盤

Wave 12 T-050: 構造化ログの基盤モジュール。
既存の logging.getLogger() パターンと互換性を保ちつつ、
JSON形式の構造化ログ出力をサポートする。

主要コンポーネント:
- StructuredFormatter: JSON/テキスト形式のログフォーマッタ
- StructuredLogger: logging.Logger のラッパー
- CorrelationContext: スレッドセーフな correlation_id 管理
- get_structured_logger(): キャッシュ付きファクトリ関数
- configure_logging(): グローバルログ設定
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ============================================================
# CorrelationContext
# ============================================================

_correlation_local = threading.local()


class CorrelationContext:
    """
    リクエスト/フロー実行単位で correlation_id を管理する。

    threading.local ベースでスレッドセーフに管理し、
    コンテキストマネージャとして使える。ネスト対応（スタック方式）。

    Usage:
        with CorrelationContext(correlation_id="req-123"):
            logger.info("processing")  # correlation_id="req-123"
            with CorrelationContext(correlation_id="sub-456"):
                logger.info("sub")     # correlation_id="sub-456"
            logger.info("back")        # correlation_id="req-123"
    """

    def __init__(self, correlation_id: Optional[str] = None) -> None:
        self._correlation_id = correlation_id or str(uuid.uuid4())
        self._previous: Optional[str] = None

    def __enter__(self) -> "CorrelationContext":
        stack = getattr(_correlation_local, "stack", None)
        if stack is None:
            _correlation_local.stack = []
        self._previous = get_correlation_id()
        _correlation_local.stack.append(self._correlation_id)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        stack = getattr(_correlation_local, "stack", None)
        if stack:
            stack.pop()

    @property
    def correlation_id(self) -> str:
        return self._correlation_id


def get_correlation_id() -> Optional[str]:
    """現在のスレッドの correlation_id を取得する。"""
    stack = getattr(_correlation_local, "stack", None)
    if stack:
        return stack[-1]
    return None


def set_correlation_id(correlation_id: str) -> None:
    """現在のスレッドの correlation_id を直接設定する（スタックにpush）。"""
    stack = getattr(_correlation_local, "stack", None)
    if stack is None:
        _correlation_local.stack = []
    _correlation_local.stack.append(correlation_id)


def clear_correlation_id() -> None:
    """現在のスレッドの correlation_id をクリアする。"""
    _correlation_local.stack = []


# ============================================================
# StructuredFormatter
# ============================================================

class StructuredFormatter(logging.Formatter):
    """
    logging.Formatter のサブクラス。
    JSON形式またはテキスト形式でログレコードをフォーマットする。

    JSON形式（デフォルト）:
        {"timestamp": "...", "level": "INFO", "module": "...", "message": "...",
         "correlation_id": "...", "pack_id": "...", ...}

    テキスト形式（RUMI_LOG_FORMAT=text or fmt_type="text"）:
        2025-01-01T00:00:00.000000Z [INFO] module - message (correlation_id=...)
    """

    def __init__(self, fmt_type: Optional[str] = None) -> None:
        super().__init__()
        if fmt_type is None:
            fmt_type = os.environ.get("RUMI_LOG_FORMAT", "json").lower()
        self._fmt_type = fmt_type

    @property
    def fmt_type(self) -> str:
        return self._fmt_type

    def format(self, record: logging.LogRecord) -> str:
        # メッセージを確定
        record.message = record.getMessage()

        if self._fmt_type == "text":
            return self._format_text(record)
        return self._format_json(record)

    def _format_json(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

        log_entry: Dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "module": record.name,
            "message": record.message,
            "correlation_id": get_correlation_id(),
        }

        # context_data を追加（extra 経由で渡される）
        context_data = getattr(record, "context_data", None)
        if context_data and isinstance(context_data, dict):
            for key, value in context_data.items():
                if key not in log_entry:
                    log_entry[key] = value

        # exc_info がある場合
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        if record.stack_info:
            log_entry["stack_info"] = record.stack_info

        return json.dumps(log_entry, ensure_ascii=False, default=str)

    def _format_text(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

        correlation_id = get_correlation_id()
        parts = [
            timestamp,
            f"[{record.levelname}]",
            record.name,
            "-",
            record.message,
        ]

        if correlation_id:
            parts.append(f"(correlation_id={correlation_id})")

        # context_data があれば付与
        context_data = getattr(record, "context_data", None)
        if context_data and isinstance(context_data, dict):
            ctx_str = " ".join(f"{k}={v}" for k, v in context_data.items())
            if ctx_str:
                parts.append(f"[{ctx_str}]")

        result = " ".join(parts)

        if record.exc_info and record.exc_info[1] is not None:
            result += "\n" + self.formatException(record.exc_info)

        if record.stack_info:
            result += "\n" + record.stack_info

        return result


# ============================================================
# StructuredLogger
# ============================================================

class StructuredLogger:
    """
    Python 標準の logging モジュールをラップする構造化ログクラス。

    既存の logging.getLogger() と互換性を保ちつつ、
    コンテキスト情報（pack_id, flow_id, step_id 等）を
    各ログエントリに付与可能にする。

    Usage:
        logger = StructuredLogger("rumi.kernel.core")
        logger.info("Starting flow", pack_id="my-pack", flow_id="startup")

        # bind() で共通コンテキストを設定
        ctx_logger = logger.bind(pack_id="my-pack")
        ctx_logger.info("Step 1")  # pack_id が自動付与
        ctx_logger.info("Step 2")  # pack_id が自動付与
    """

    def __init__(self, name: str, **default_context: Any) -> None:
        self._logger = logging.getLogger(name)
        self._name = name
        self._default_context: Dict[str, Any] = dict(default_context)

    @property
    def name(self) -> str:
        return self._name

    @property
    def logger(self) -> logging.Logger:
        """内部の logging.Logger を取得（互換性のため）。"""
        return self._logger

    def bind(self, **kwargs: Any) -> "StructuredLogger":
        """
        コンテキスト情報をバインドした新しい StructuredLogger を返す。
        元のロガーのデフォルトコンテキストと新しいコンテキストをマージする。
        """
        merged = {**self._default_context, **kwargs}
        return StructuredLogger(self._name, **merged)

    def _build_context(self, extra_context: Dict[str, Any]) -> Dict[str, Any]:
        """デフォルトコンテキストと呼び出し時コンテキストをマージする。"""
        ctx = dict(self._default_context)
        ctx.update(extra_context)
        return ctx

    def _log(self, level: int, msg: str, exc_info: Any = None,
             stack_info: bool = False, **kwargs: Any) -> None:
        """内部ログ出力メソッド。"""
        if not self._logger.isEnabledFor(level):
            return

        context_data = self._build_context(kwargs)
        extra = {"context_data": context_data}
        self._logger.log(
            level, msg, exc_info=exc_info, stack_info=stack_info, extra=extra
        )

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, exc_info: Any = None, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, exc_info=exc_info, **kwargs)

    def critical(self, msg: str, exc_info: Any = None, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, msg, exc_info=exc_info, **kwargs)

    def exception(self, msg: str, **kwargs: Any) -> None:
        """error + exc_info=True のショートカット。"""
        self._log(logging.ERROR, msg, exc_info=True, **kwargs)

    def isEnabledFor(self, level: int) -> bool:
        return self._logger.isEnabledFor(level)

    def setLevel(self, level: int) -> None:
        self._logger.setLevel(level)

    def getEffectiveLevel(self) -> int:
        return self._logger.getEffectiveLevel()


# ============================================================
# get_structured_logger (ファクトリ関数)
# ============================================================

_logger_cache: Dict[str, StructuredLogger] = {}
_logger_cache_lock = threading.Lock()


def get_structured_logger(name: str) -> StructuredLogger:
    """
    StructuredLogger のファクトリ関数。
    同じ name に対しては同じインスタンスを返す（キャッシュ）。

    Args:
        name: ロガー名（例: "rumi.kernel.core"）

    Returns:
        StructuredLogger インスタンス
    """
    if name in _logger_cache:
        return _logger_cache[name]

    with _logger_cache_lock:
        # double-checked locking
        if name not in _logger_cache:
            _logger_cache[name] = StructuredLogger(name)
        return _logger_cache[name]


def reset_logger_cache() -> None:
    """ロガーキャッシュをリセットする（テスト用）。"""
    with _logger_cache_lock:
        _logger_cache.clear()


# ============================================================
# configure_logging
# ============================================================

_configured = False
_configure_lock = threading.Lock()


def _close_handlers(logger: logging.Logger) -> None:
    """ロガーの全ハンドラを安全にクローズしてクリアする。"""
    for h in logger.handlers[:]:
        try:
            h.close()
        except Exception:
            pass
    logger.handlers.clear()


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    output: str = "stderr",
) -> None:
    """
    グローバルなログ設定を行う。

    Args:
        level: ログレベル（DEBUG/INFO/WARNING/ERROR/CRITICAL）
        fmt: 出力形式（"json" or "text"）
        output: 出力先（"stderr" or ファイルパス）
    """
    global _configured

    with _configure_lock:
        # ログレベルを解決
        numeric_level = getattr(logging, level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError(f"Invalid log level: {level}")

        # フォーマッタを作成
        formatter = StructuredFormatter(fmt_type=fmt.lower())

        # ハンドラを作成
        handler: logging.Handler
        if output == "stderr":
            handler = logging.StreamHandler(sys.stderr)
        else:
            handler = logging.FileHandler(output, encoding="utf-8")

        handler.setFormatter(formatter)
        handler.setLevel(numeric_level)

        # ルートロガーの "rumi" 名前空間に設定
        rumi_logger = logging.getLogger("rumi")
        # 既存ハンドラを安全にクローズしてクリア
        _close_handlers(rumi_logger)
        rumi_logger.addHandler(handler)
        rumi_logger.setLevel(numeric_level)
        # 親ロガーへの伝播を防止
        rumi_logger.propagate = False

        _configured = True


def is_configured() -> bool:
    """ログが configure_logging() で設定済みかどうかを返す。"""
    return _configured


def reset_configuration() -> None:
    """設定状態をリセットする（テスト用）。"""
    global _configured
    with _configure_lock:
        rumi_logger = logging.getLogger("rumi")
        _close_handlers(rumi_logger)
        rumi_logger.setLevel(logging.WARNING)
        rumi_logger.propagate = True
        _configured = False
