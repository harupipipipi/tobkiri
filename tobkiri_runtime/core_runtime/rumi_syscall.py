"""
rumi_syscall.py - Rumi AI OS システムコールAPI（単一ソース）

Packがコンテナ内から外部通信を行うためのAPI。
UDS経由でEgress Proxyに接続し、HTTPリクエストを実行する。

PR-B: このファイルが唯一の実体。
- コンテナ内では /rumi_syscall.py として注入される
- import rumi_syscall で使用可能

使用例:
    import rumi_syscall
    
    result = rumi_syscall.http_request(
        method="GET",
        url="https://api.example.com/data",
        headers={"Accept": "application/json"},
        timeout_seconds=30.0
    )
    
    if result["success"]:
        print(result["body"])
    else:
        print(f"Error: {result['error']}")
"""

from __future__ import annotations

import json
import os
import socket
import struct
from typing import Any, Dict, Optional

from .host_contract import host_contract_value


# デフォルトのUDSソケットパス（コンテナ内）
DEFAULT_SOCKET_PATH = "/run/rumi/egress.sock"

# 環境変数でオーバーライド可能
SOCKET_PATH = os.getenv("RUMI_EGRESS_SOCKET", DEFAULT_SOCKET_PATH)

# BUG-5-1: TCP fallback environment variables
EGRESS_HOST = os.getenv("RUMI_EGRESS_HOST", "")
EGRESS_PORT = int(os.getenv("RUMI_EGRESS_PORT", "0") or "0")
EGRESS_TOKEN = host_contract_value("egress_token")
_TCP_MODE = bool(EGRESS_HOST and EGRESS_PORT)

# プロトコル定数
MAX_RESPONSE_SIZE = 4 * 1024 * 1024  # 4MB
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 120.0


class SyscallError(Exception):
    """システムコールエラー"""
    pass


def _read_length_prefixed_json(sock: socket.socket, max_size: int) -> Dict[str, Any]:
    """length-prefix JSON を読み取る"""
    # 4バイトの長さプレフィックスを読む
    length_data = b""
    while len(length_data) < 4:
        chunk = sock.recv(4 - len(length_data))
        if not chunk:
            raise SyscallError("Connection closed by proxy")
        length_data += chunk
    
    length = struct.unpack(">I", length_data)[0]
    
    if length > max_size:
        raise SyscallError(f"Response too large: {length} > {max_size}")
    
    if length == 0:
        return {}
    
    # JSONペイロードを読む
    data = b""
    while len(data) < length:
        chunk = sock.recv(min(length - len(data), 65536))
        if not chunk:
            raise SyscallError("Connection closed while reading response")
        data += chunk
    
    return json.loads(data.decode("utf-8"))


def _write_length_prefixed_json(sock: socket.socket, data: Dict[str, Any]) -> None:
    """length-prefix JSON を書き込む"""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    length = struct.pack(">I", len(payload))
    sock.sendall(length + payload)


def http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    socket_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    HTTPリクエストを実行（Egress Proxy経由）
    
    Args:
        method: HTTPメソッド（GET, POST, PUT, DELETE, PATCH, HEAD）
        url: リクエスト先URL
        headers: HTTPヘッダー
        body: リクエストボディ
        timeout_seconds: タイムアウト秒数（最大120秒）
        socket_path: UDSソケットパス（通常は指定不要）
    
    Returns:
        dict:
            success: bool - 成功したか
            status_code: int - HTTPステータスコード（成功時）
            headers: dict - レスポンスヘッダー（成功時）
            body: str - レスポンスボディ（成功時）
            error: str - エラーメッセージ（失敗時）
            error_type: str - エラー種別（失敗時）
            latency_ms: float - 所要時間（ミリ秒）
            redirect_hops: int - リダイレクト回数
            bytes_read: int - 読み取りバイト数
            final_url: str - 最終URL（リダイレクト後）
    """
    sock_path = socket_path or SOCKET_PATH
    timeout = min(float(timeout_seconds), MAX_TIMEOUT)
    
    # リクエストデータを構築
    request = {
        "method": method.upper(),
        "url": url,
        "headers": headers or {},
        "body": body,
        "timeout_seconds": timeout,
    }
    
    sock = None
    try:
        # BUG-5-1: TCP fallback connection
        if _TCP_MODE:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout + 5)
            try:
                sock.connect((EGRESS_HOST, EGRESS_PORT))
            except ConnectionRefusedError:
                return {
                    "success": False,
                    "error": f"Connection refused to egress proxy: {EGRESS_HOST}:{EGRESS_PORT}",
                    "error_type": "connection_refused",
                }
            # TCP 認証ハンドシェイク
            _write_length_prefixed_json(sock, {"auth_token": EGRESS_TOKEN})
            auth_resp = _read_length_prefixed_json(sock, 65536)
            if not auth_resp or not auth_resp.get("auth_ok"):
                auth_err = auth_resp.get("error", "Unknown auth error") if auth_resp else "No auth response"
                return {
                    "success": False,
                    "error": f"Egress proxy auth failed: {auth_err}",
                    "error_type": "auth_failed",
                }
        else:
            # UDSに接続
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout + 5)  # プロキシ処理時間を考慮
            try:
                sock.connect(sock_path)
            except FileNotFoundError:
                return {
                    "success": False,
                    "error": f"Egress proxy socket not found: {sock_path}",
                    "error_type": "socket_not_found",
                }
            except PermissionError:
                return {
                    "success": False,
                    "error": f"Permission denied to egress proxy socket: {sock_path}",
                    "error_type": "permission_denied",
                }
            except ConnectionRefusedError:
                return {
                    "success": False,
                    "error": f"Connection refused to egress proxy: {sock_path}",
                    "error_type": "connection_refused",
                }
        
        # リクエスト送信
        _write_length_prefixed_json(sock, request)
        
        # レスポンス受信
        response = _read_length_prefixed_json(sock, MAX_RESPONSE_SIZE)
        
        return response
        
    except socket.timeout:
        return {
            "success": False,
            "error": f"Request timed out after {timeout}s",
            "error_type": "timeout",
        }
    except SyscallError as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": "syscall_error",
        }
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Invalid JSON response: {e}",
            "error_type": "json_decode_error",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
        }
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """GETリクエストのショートカット"""
    return http_request("GET", url, headers=headers, timeout_seconds=timeout_seconds)


def post(
    url: str,
    body: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """POSTリクエストのショートカット"""
    return http_request("POST", url, headers=headers, body=body, timeout_seconds=timeout_seconds)


def post_json(
    url: str,
    data: Any,
    headers: Optional[Dict[str, str]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """JSON POSTリクエストのショートカット"""
    h = dict(headers or {})
    h["Content-Type"] = "application/json"
    body = json.dumps(data, ensure_ascii=False)
    return http_request("POST", url, headers=h, body=body, timeout_seconds=timeout_seconds)


def put(
    url: str,
    body: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """PUTリクエストのショートカット"""
    return http_request("PUT", url, headers=headers, body=body, timeout_seconds=timeout_seconds)


def delete(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """DELETEリクエストのショートカット"""
    return http_request("DELETE", url, headers=headers, timeout_seconds=timeout_seconds)


def patch(
    url: str,
    body: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """PATCHリクエストのショートカット"""
    return http_request("PATCH", url, headers=headers, body=body, timeout_seconds=timeout_seconds)


def head(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """HEADリクエストのショートカット"""
    return http_request("HEAD", url, headers=headers, timeout_seconds=timeout_seconds)


# 互換性のためのエイリアス
request = http_request
