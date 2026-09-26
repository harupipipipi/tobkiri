"""
rumi_capability.py - Rumi AI OS Capability Client API（単一ソース）

Pack がコンテナ内（または permissive ホスト実行時）から
capability を呼び出すためのクライアントAPI。

UDS 経由で Host Capability Proxy に接続し、
length-prefix JSON プロトコルで要求を送信する。

PR-B: このファイルが唯一の実体。
- コンテナ内では /rumi_capability.py として注入される
- import rumi_capability で使用可能

使用例:
    import rumi_capability
    result = rumi_capability.call("fs.read", args={"path": "/data/config.json"})

    if result["success"]:
        print(result["output"])
    else:
        print(f"Error: {result['error']}")
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any, Dict, Optional

# デフォルトのUDSソケットパス（コンテナ内）
DEFAULT_SOCKET_PATH = "/run/rumi/capability.sock"
SOCKET_PATH = DEFAULT_SOCKET_PATH

# プロトコル定数
MAX_RESPONSE_SIZE = 4 * 1024 * 1024  # 4MB
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 120.0


class CapabilityError(Exception):
    """Capability 呼び出しエラー"""
    pass


def _read_length_prefixed_json(sock: socket.socket, max_size: int) -> Dict[str, Any]:
    """length-prefix JSON を読み取る"""
    length_data = b""
    while len(length_data) < 4:
        chunk = sock.recv(4 - len(length_data))
        if not chunk:
            raise CapabilityError("Connection closed by proxy")
        length_data += chunk

    length = struct.unpack(">I", length_data)[0]

    if length > max_size:
        raise CapabilityError(f"Response too large: {length} > {max_size}")

    if length == 0:
        return {}

    data = b""
    while len(data) < length:
        chunk = sock.recv(min(length - len(data), 65536))
        if not chunk:
            raise CapabilityError("Connection closed while reading response")
        data += chunk

    return json.loads(data.decode("utf-8"))


def _write_length_prefixed_json(sock: socket.socket, data: Dict[str, Any]) -> None:
    """length-prefix JSON を書き込む"""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    length = struct.pack(">I", len(payload))
    sock.sendall(length + payload)


def call(
    permission_id: str,
    args: Optional[Dict[str, Any]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    socket_path: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Capability を呼び出す

    Args:
        permission_id: 実行する権限ID（例: "fs.read", "ui.autogui"）
        args: ハンドラーに渡す引数
        timeout_seconds: タイムアウト秒数（最大120秒）
        socket_path: UDSソケットパス（通常は指定不要）
        request_id: リクエスト追跡用ID（任意）

    Returns:
        dict:
            success: bool
            output: Any（成功時）
            error: str（失敗時）
            error_type: str（失敗時）
            latency_ms: float
    """
    sock_path = socket_path or SOCKET_PATH
    timeout = min(float(timeout_seconds), MAX_TIMEOUT)

    request = {
        "permission_id": permission_id,
        "args": args or {},
        "timeout_seconds": timeout,
    }
    if request_id:
        request["request_id"] = request_id

    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout + 5)

        try:
            sock.connect(sock_path)
        except FileNotFoundError:
            return {
                "success": False,
                "error": f"Capability proxy socket not found: {sock_path}",
                "error_type": "socket_not_found",
                "output": None,
                "latency_ms": 0,
            }
        except PermissionError:
            return {
                "success": False,
                "error": f"Permission denied to capability proxy socket: {sock_path}",
                "error_type": "permission_denied",
                "output": None,
                "latency_ms": 0,
            }
        except ConnectionRefusedError:
            return {
                "success": False,
                "error": f"Connection refused to capability proxy: {sock_path}",
                "error_type": "connection_refused",
                "output": None,
                "latency_ms": 0,
            }

        _write_length_prefixed_json(sock, request)
        return _read_length_prefixed_json(sock, MAX_RESPONSE_SIZE)

    except socket.timeout:
        return {
            "success": False,
            "error": f"Request timed out after {timeout}s",
            "error_type": "timeout",
            "output": None,
            "latency_ms": 0,
        }
    except CapabilityError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "capability_error",
            "output": None,
            "latency_ms": 0,
        }
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Invalid JSON response: {e}",
            "error_type": "json_decode_error",
            "output": None,
            "latency_ms": 0,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "output": None,
            "latency_ms": 0,
        }
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def get_secret(key: str) -> Optional[str]:
    """
    Retrieve a secret value by key.

    Convenience wrapper around ``rumi_capability.call("secrets.get", {"key": key})``.
    Returns the secret value on success, or ``None`` on failure.

    Args:
        key: Secret key (uppercase, digits, underscores only, max 64 chars)

    Returns:
        Secret value string, or ``None`` if not found / access denied
    """
    try:
        result = call("secrets.get", {"key": key})
        if isinstance(result, dict) and result.get("success"):
            return result.get("value")
        return None
    except Exception:
        return None
