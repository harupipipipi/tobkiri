"""
egress_protocol.py - Egress通信プロトコルユーティリティ

length-prefix JSON プロトコル、リクエストバリデーション、
レスポンス読み取り（B2対応）、監査ログヘルパー。
egress_proxy.py から分離 (W13-T047)。
"""
from __future__ import annotations

import json
import socket
import struct
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse


# ============================================================
# 定数（バリデーション用）
# ============================================================

ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}
MAX_HEADER_COUNT = 100
MAX_HEADER_NAME_LENGTH = 256
MAX_HEADER_VALUE_LENGTH = 8192
MAX_TIMEOUT = 120.0
MAX_CONNECT_TIMEOUT = 60.0
MAX_READ_TIMEOUT = MAX_TIMEOUT  # 120.0
MAX_RESPONSE_READ_CHUNK = 65536


# ============================================================
# length-prefix JSON プロトコル
# ============================================================

def read_length_prefixed_json(sock: socket.socket, max_size: int) -> Optional[Dict[str, Any]]:
    """
    length-prefix JSON を読み取る（4バイトビッグエンディアン + JSON）
    """
    length_data = b""
    while len(length_data) < 4:
        chunk = sock.recv(4 - len(length_data))
        if not chunk:
            return None
        length_data += chunk

    length = struct.unpack(">I", length_data)[0]

    if length > max_size:
        raise ValueError(f"Message too large: {length} > {max_size}")

    if length == 0:
        return {}

    data = b""
    while len(data) < length:
        chunk = sock.recv(min(length - len(data), 65536))
        if not chunk:
            raise ValueError("Connection closed while reading payload")
        data += chunk

    return json.loads(data.decode("utf-8"))


def write_length_prefixed_json(sock: socket.socket, data: Dict[str, Any]) -> None:
    """length-prefix JSON を書き込む"""
    payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    length = struct.pack(">I", len(payload))
    sock.sendall(length + payload)


# ============================================================
# リクエストバリデーション
# ============================================================

def validate_request(request: Dict[str, Any]) -> Tuple[bool, str]:
    """リクエストのバリデーション"""
    method = request.get("method", "").upper()
    if not method:
        return False, "Method is required"
    if method not in ALLOWED_METHODS:
        return False, f"Method not allowed: {method}. Allowed: {ALLOWED_METHODS}"

    url = request.get("url", "")
    if not url:
        return False, "URL is required"

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"Unsupported scheme: {parsed.scheme}"
        if not parsed.hostname:
            return False, "URL must have a hostname"
    except Exception as e:
        return False, f"Invalid URL: {e}"

    headers = request.get("headers", {})
    if not isinstance(headers, dict):
        return False, "Headers must be a dict"
    if len(headers) > MAX_HEADER_COUNT:
        return False, f"Too many headers: {len(headers)} > {MAX_HEADER_COUNT}"

    for name, value in headers.items():
        if len(str(name)) > MAX_HEADER_NAME_LENGTH:
            return False, f"Header name too long: {len(str(name))} > {MAX_HEADER_NAME_LENGTH}"
        if len(str(value)) > MAX_HEADER_VALUE_LENGTH:
            return False, f"Header value too long: {len(str(value))} > {MAX_HEADER_VALUE_LENGTH}"

    timeout = request.get("timeout_seconds")
    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            return False, f"Invalid timeout: {timeout}"
        if timeout <= 0:
            return False, f"Timeout must be positive: {timeout}"
        if timeout > MAX_TIMEOUT:
            return False, f"Timeout too large: {timeout} > {MAX_TIMEOUT}"

    connect_timeout = request.get("connect_timeout_seconds")
    if connect_timeout is not None:
        try:
            connect_timeout = float(connect_timeout)
        except (TypeError, ValueError):
            return False, f"Invalid connect_timeout: {connect_timeout}"
        if connect_timeout <= 0:
            return False, f"Connect timeout must be positive: {connect_timeout}"
        if connect_timeout > MAX_CONNECT_TIMEOUT:
            return False, f"Connect timeout too large: {connect_timeout} > {MAX_CONNECT_TIMEOUT}"

    read_timeout = request.get("read_timeout_seconds")
    if read_timeout is not None:
        try:
            read_timeout = float(read_timeout)
        except (TypeError, ValueError):
            return False, f"Invalid read_timeout: {read_timeout}"
        if read_timeout <= 0:
            return False, f"Read timeout must be positive: {read_timeout}"
        if read_timeout > MAX_READ_TIMEOUT:
            return False, f"Read timeout too large: {read_timeout} > {MAX_READ_TIMEOUT}"

    return True, ""


# ============================================================
# 監査ログヘルパー
# ============================================================

def _log_network_event(
    audit_logger,
    pack_id: str,
    domain: str,
    port: int,
    allowed: bool,
    reason: str | None = None,
    method: str | None = None,
    url: str | None = None,
    final_url: str | None = None,
    latency_ms: float = 0,
    status_code: int | None = None,
    error_type: str | None = None,
    redirect_hops: int = 0,
    bytes_read: int = 0,
    blocked_reason: str | None = None,
    max_response_bytes: int | None = None,
    check_type: str | None = None
) -> None:
    """監査ログにネットワークイベントを記録"""
    if audit_logger is None:
        return

    try:
        request_details = {
            "method": method,
            "url": url,
            "final_url": final_url,
            "latency_ms": latency_ms,
            "redirect_hops": redirect_hops,
            "bytes_read": bytes_read,
        }

        if status_code is not None:
            request_details["status_code"] = status_code
        if error_type is not None:
            request_details["error_type"] = error_type
        if blocked_reason is not None:
            request_details["blocked_reason"] = blocked_reason
        if max_response_bytes is not None:
            request_details["max_response_bytes"] = max_response_bytes
        if check_type is not None:
            request_details["check_type"] = check_type

        audit_logger.log_network_event(
            pack_id=pack_id,
            domain=domain,
            port=port,
            allowed=allowed,
            reason=reason,
            request_details=request_details
        )
    except Exception as e:
        print(f"[EgressProxy] Failed to log network event: {e}")


# ============================================================
# レスポンス読み取りヘルパー（B2対応）
# ============================================================

def read_response_with_limit(resp, max_size: int) -> Tuple[bytes, bool, int]:
    """
    レスポンスボディを上限付きで読み取る

    Returns:
        (data, exceeded, bytes_read)
        - data: 読み取ったデータ（超過時は途中まで）
        - exceeded: 上限を超過したか
        - bytes_read: 実際に読み取ったバイト数
    """
    content_length = resp.getheader("Content-Length")
    if content_length:
        try:
            cl = int(content_length)
            if cl > max_size:
                return b"", True, 0
        except (ValueError, TypeError):
            pass

    data = b""
    bytes_read = 0
    exceeded = False

    while True:
        remaining = max_size - bytes_read + 1
        chunk_size = min(MAX_RESPONSE_READ_CHUNK, remaining)

        try:
            chunk = resp.read(chunk_size)
        except Exception:
            break

        if not chunk:
            break

        bytes_read += len(chunk)

        if bytes_read > max_size:
            exceeded = True
            data += chunk[:max_size - (bytes_read - len(chunk))]
            break

        data += chunk

    return data, exceeded, bytes_read
