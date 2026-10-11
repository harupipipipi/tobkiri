"""
egress_proxy.py - UDS Egress Proxy サーバー

Packからの外部ネットワーク通信を仲介するプロキシサーバー。
Pack別UDSソケットでpack_idを確定し（payloadは無視）、
network grant に基づいて allow/deny を判定し、監査ログに記録する。

セキュリティ防御:
- 内部IP禁止（localhost/private/link-local/CGNAT/multicast等）
- DNS rebinding対策（解決結果が内部IPなら拒否）
- リダイレクト上限（3ホップ、各ホップでgrant再チェック）
- リクエスト/レスポンスサイズ制限
- タイムアウト制限
- ヘッダー数/サイズ制限
- メソッド制限
- Pack別レート制限（W12-T046追加）
- ドメインホワイトリスト/ブラックリスト（W12-T046追加）
- 細粒度タイムアウト制御（W12-T046追加）

PR-B変更:
- 内部宛て禁止をgrant判定より前に（B1）
- 巨大レスポンスは必ず失敗化（B2）
- Packへの返却は汎用理由、auditに詳細

PR-C変更:
- ソケットファイル名をsha256(pack_id)[:32]ベースで衝突回避
- chmod 0666固定を廃止、デフォルト0660 + envで0666緩和可能
- gidをenvで指定可能（best-effort）
- capability_proxy.pyのパーミッション方式に統一

W12-T046変更:
- PackRateLimiter: Pack別レート制限（60 req/min デフォルト）
- DomainController: ecosystem.json ベースのドメイン制御
- 細粒度タイムアウト: connect_timeout / read_timeout 分離
W13-T047変更:
- egress_ip.py / egress_protocol.py / egress_rate_limiter.py / egress_domain_controller.py に分割
- セキュリティチェック順序修正: 内部IP→DNS→ドメイン制御→レート制限→Grant
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import concurrent.futures
import time
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin


# W13-T047: サブモジュールからの re-import（後方互換維持）
from .egress_ip import (  # noqa: F401 — re-export
    BLOCKED_IPV4_NETWORKS,
    BLOCKED_IPV6_NETWORKS,
    BLOCKED_IPV4_ADDRESSES,
    is_internal_ip,
    _is_ip_literal,
    resolve_and_check_ip,
)
from .egress_protocol import (  # noqa: F401 — re-export
    read_length_prefixed_json,
    write_length_prefixed_json,
    validate_request,
    read_response_with_limit,
    _log_network_event,
    ALLOWED_METHODS,
    MAX_HEADER_COUNT,
    MAX_HEADER_NAME_LENGTH,
    MAX_HEADER_VALUE_LENGTH,
    MAX_RESPONSE_READ_CHUNK,
)
from .egress_rate_limiter import (  # noqa: F401 — re-export
    PackRateLimiter,
    DEFAULT_RATE_LIMIT_PER_MIN,
    RATE_LIMIT_WINDOW_SECONDS,
)
from .egress_domain_controller import (  # noqa: F401 — re-export
    DomainController,
)
# BUG-5-1: IPC 認証 (Windows TCP フォールバック)
from .ipc_auth import IpcAuthManager, perform_server_auth  # noqa: F401




# ============================================================
# 定数
# ============================================================

# サイズ制限
MAX_REQUEST_SIZE = 1 * 1024 * 1024   # 1MB
MAX_RESPONSE_SIZE = 4 * 1024 * 1024  # 4MB

# タイムアウト
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 120.0

# リダイレクト
MAX_REDIRECTS = 3


# セキュリティブロック用の汎用エラーメッセージ（Pack向け）
GENERIC_SECURITY_BLOCK_MESSAGE = "Request blocked by security policy"
GENERIC_SECURITY_BLOCK_TYPE = "blocked"

# Egress ソケットパーミッション定数
_EGRESS_DEFAULT_SOCKET_MODE = 0o660
_EGRESS_DEFAULT_DIR_MODE = 0o750
_EGRESS_RELAXED_SOCKET_MODE = 0o666

_IS_WINDOWS = sys.platform == "win32"


# 細粒度タイムアウト (W12-T046)
try:
    DEFAULT_CONNECT_TIMEOUT = float(os.environ.get("RUMI_EGRESS_CONNECT_TIMEOUT", "10.0"))
except (ValueError, TypeError):
    DEFAULT_CONNECT_TIMEOUT = 10.0
try:
    DEFAULT_READ_TIMEOUT = float(os.environ.get("RUMI_EGRESS_READ_TIMEOUT", "30.0"))
except (ValueError, TypeError):
    DEFAULT_READ_TIMEOUT = 30.0
MAX_CONNECT_TIMEOUT = 60.0
MAX_READ_TIMEOUT = MAX_TIMEOUT  # 120.0


# ============================================================
# ソケット命名ユーティリティ
# ============================================================

def _pack_socket_name(pack_id: str) -> str:
    """pack_id から衝突しないソケットファイル名を生成"""
    h = hashlib.sha256(pack_id.encode("utf-8")).hexdigest()[:32]
    return f"{h}.sock"


# ============================================================
# Egress パーミッション ユーティリティ
# ============================================================

def _get_egress_socket_mode() -> int:
    """環境変数からソケットパーミッションモードを取得"""
    raw = os.environ.get("RUMI_EGRESS_SOCKET_MODE", "").strip()
    if raw == "0666":
        return _EGRESS_RELAXED_SOCKET_MODE
    return _EGRESS_DEFAULT_SOCKET_MODE


def _get_egress_socket_gid() -> Optional[int]:
    """環境変数からソケットGIDを取得"""
    raw = os.environ.get("RUMI_EGRESS_SOCKET_GID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _apply_egress_dir_permissions(dir_path: Path) -> None:
    """
    ディレクトリにパーミッションを適用（best-effort）

    - chmod 0750
    - RUMI_EGRESS_SOCKET_GID 指定時は chown で group を合わせる
    - 全て失敗しても例外を出さず、audit に警告を残す
    """
    if _IS_WINDOWS:
        return  # Windows: skip Unix permissions
    try:
        os.chmod(dir_path, _EGRESS_DEFAULT_DIR_MODE)
    except (OSError, PermissionError) as e:
        _audit_egress_permission_warning("dir_chmod_failed", str(dir_path),
                                          f"Failed to chmod directory {dir_path} to 0750: {e}")

    gid = _get_egress_socket_gid()
    if gid is not None and hasattr(os, "chown"):
        try:
            os.chown(dir_path, -1, gid)
        except (OSError, PermissionError) as e:
            _audit_egress_permission_warning("dir_chown_failed", str(dir_path),
                                              f"Failed to chown directory {dir_path} to gid {gid}: {e}")


def _apply_egress_socket_permissions(sock_path: Path) -> None:
    """
    ソケットファイルにパーミッションを適用（best-effort）

    - デフォルト chmod 0660
    - RUMI_EGRESS_SOCKET_MODE=0666 の場合のみ 0666（audit に記録）
    - RUMI_EGRESS_SOCKET_GID 指定時は chown で group を合わせる
    """
    if _IS_WINDOWS:
        return  # Windows: skip Unix permissions
    mode = _get_egress_socket_mode()

    if mode == _EGRESS_RELAXED_SOCKET_MODE:
        _audit_egress_permission_warning("relaxed_socket_mode", str(sock_path),
            f"SECURITY WARNING: Egress socket {sock_path} using relaxed mode 0666 "
            f"(RUMI_EGRESS_SOCKET_MODE=0666). This is less secure.")

    try:
        os.chmod(sock_path, mode)
    except (OSError, PermissionError) as e:
        _audit_egress_permission_warning("socket_chmod_failed", str(sock_path),
                                          f"Failed to chmod socket {sock_path} to {oct(mode)}: {e}")

    gid = _get_egress_socket_gid()
    if gid is not None and hasattr(os, "chown"):
        try:
            os.chown(sock_path, -1, gid)
        except (OSError, PermissionError) as e:
            _audit_egress_permission_warning("socket_chown_failed", str(sock_path),
                                              f"Failed to chown socket {sock_path} to gid {gid}: {e}")


def _audit_egress_permission_warning(event_type: str, path: str, message: str) -> None:
    """パーミッション設定の警告を監査ログに記録"""
    try:
        from .audit_logger import get_audit_logger
        audit = get_audit_logger()
        audit.log_security_event(
            event_type=f"egress_proxy_{event_type}",
            severity="warning",
            description=message,
            details={"path": path},
        )
    except Exception:
        pass


# ============================================================
# HTTPリクエスト実行
# ============================================================

def execute_http_request(
    pack_id: str,
    request: Dict[str, Any],
    network_grant_manager,
    audit_logger,
    rate_limiter: "PackRateLimiter | None" = None,
    domain_controller: "DomainController | None" = None,
) -> Dict[str, Any]:
    """
    HTTPリクエストを実行（リダイレクト、セキュリティチェック込み）

    pack_id はUDSソケットから確定済み（payloadのowner_packは無視）

    PR-B変更:
    - B1: 内部宛て禁止をgrant判定より前に
    - B2: 巨大レスポンスは必ず失敗化
    - Packへの返却は汎用理由、auditに詳細

    W12-T046変更:
    - ドメイン制御チェック（DNS解決後、Grant前）
    - レート制限チェック（ドメイン制御後、Grant前、初回のみ）
    - 細粒度タイムアウト（connect_timeout / read_timeout 分離）
    """
    start_time = time.time()

    method = request.get("method", "GET").upper()
    original_url = request.get("url", "")
    headers = request.get("headers", {})
    body = request.get("body")

    # ================================================================
    # 細粒度タイムアウト解決 (W12-T046)
    # 後方互換: timeout_seconds が指定されていれば両方に使う
    # ================================================================
    legacy_timeout = request.get("timeout_seconds")
    if legacy_timeout is not None:
        legacy_timeout = min(float(legacy_timeout), MAX_TIMEOUT)
        connect_timeout = legacy_timeout
        read_timeout = legacy_timeout
    else:
        connect_timeout = DEFAULT_CONNECT_TIMEOUT
        read_timeout = DEFAULT_READ_TIMEOUT

    # 個別指定があれば上書き
    req_connect = request.get("connect_timeout_seconds")
    if req_connect is not None:
        connect_timeout = min(float(req_connect), MAX_CONNECT_TIMEOUT)
    req_read = request.get("read_timeout_seconds")
    if req_read is not None:
        read_timeout = min(float(req_read), MAX_READ_TIMEOUT)

    current_url = original_url
    redirect_hops = 0
    bytes_read = 0
    final_url = original_url
    last_domain = ""
    last_port = 0

    result = {
        "success": False,
        "status_code": 0,
        "headers": {},
        "body": "",
        "error": None,
        "error_type": None,
        "latency_ms": 0,
        "redirect_hops": 0,
        "bytes_read": 0,
        "final_url": original_url,
    }

    try:
        rate_limit_checked = False  # W13-T047: レート制限は初回のみ
        while redirect_hops <= MAX_REDIRECTS:
            parsed = urlparse(current_url)
            domain = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            last_domain = domain
            last_port = port

            # ============================================================
            # B1: 内部宛て禁止を最優先判定（grantより前）
            # ============================================================

            # 1. IPリテラルチェック（内部IP禁止）
            if _is_ip_literal(domain):
                is_blocked, reason = is_internal_ip(domain)
                if is_blocked:
                    # Pack向けは汎用メッセージ
                    result["error"] = GENERIC_SECURITY_BLOCK_MESSAGE
                    result["error_type"] = GENERIC_SECURITY_BLOCK_TYPE
                    result["latency_ms"] = (time.time() - start_time) * 1000
                    result["redirect_hops"] = redirect_hops
                    result["final_url"] = current_url

                    # auditには詳細を記録
                    _log_network_event(
                        audit_logger, pack_id, domain, port, False,
                        reason=reason,
                        method=method, url=original_url, final_url=current_url,
                        latency_ms=result["latency_ms"],
                        redirect_hops=redirect_hops,
                        blocked_reason="internal_ip_blocked",
                        check_type="proxy_request"
                    )
                    return result

            # 2. DNS解決 & 内部IP検証（DNS rebinding対策）
            is_blocked, dns_reason, resolved_ips = resolve_and_check_ip(domain)
            if is_blocked:
                # Pack向けは汎用メッセージ
                result["error"] = GENERIC_SECURITY_BLOCK_MESSAGE
                result["error_type"] = GENERIC_SECURITY_BLOCK_TYPE
                result["latency_ms"] = (time.time() - start_time) * 1000
                result["redirect_hops"] = redirect_hops
                result["final_url"] = current_url

                # blocked_reasonを判定（dns_rebindingかdns_blockedか）
                blocked_reason = "dns_blocked"
                if "rebinding" in dns_reason.lower():
                    blocked_reason = "dns_rebinding_blocked"

                # auditには詳細を記録
                _log_network_event(
                    audit_logger, pack_id, domain, port, False,
                    reason=dns_reason,
                    method=method, url=original_url, final_url=current_url,
                    latency_ms=result["latency_ms"],
                    redirect_hops=redirect_hops,
                    blocked_reason=blocked_reason,
                    check_type="proxy_request"
                )
                return result

            # ============================================================
            # 2.5. ドメイン制御チェック（DNS解決後、Grant前）(W12-T046)
            # ============================================================
            if domain_controller:
                dc_allowed, dc_reason = domain_controller.check_domain(pack_id, domain)
                if not dc_allowed:
                    result["error"] = GENERIC_SECURITY_BLOCK_MESSAGE
                    result["error_type"] = GENERIC_SECURITY_BLOCK_TYPE
                    result["latency_ms"] = (time.time() - start_time) * 1000
                    result["redirect_hops"] = redirect_hops
                    result["final_url"] = current_url

                    _log_network_event(
                        audit_logger, pack_id, domain, port, False,
                        reason=dc_reason,
                        method=method, url=original_url, final_url=current_url,
                        latency_ms=result["latency_ms"],
                        redirect_hops=redirect_hops,
                        blocked_reason="domain_control_denied",
                        check_type="proxy_request"
                    )
                    return result


            # ============================================================
            # 2.7. レート制限チェック（ドメイン制御後、Grant前、初回のみ）
            # W13-T047: セキュリティチェック順序修正
            # ============================================================
            if rate_limiter and not rate_limit_checked:
                rate_limit_checked = True
                rl_allowed, rl_reason = rate_limiter.check_rate_limit(pack_id)
                if not rl_allowed:
                    result["error"] = "Rate limit exceeded"
                    result["error_type"] = "rate_limited"
                    result["latency_ms"] = (time.time() - start_time) * 1000
                    result["redirect_hops"] = redirect_hops
                    result["final_url"] = current_url

                    _log_network_event(
                        audit_logger, pack_id, domain, port, False,
                        reason=rl_reason,
                        method=method, url=original_url, final_url=current_url,
                        latency_ms=result["latency_ms"],
                        redirect_hops=redirect_hops,
                        blocked_reason="rate_limited",
                        check_type="proxy_request"
                    )
                    return result

            # ============================================================
            # 3. Grant チェック（内部宛て判定の後）
            # ============================================================
            if network_grant_manager is None:
                reason = "network grant manager unavailable"
                result["error"] = f"Network access denied: {reason}"
                result["error_type"] = "grant_denied"
                result["latency_ms"] = (time.time() - start_time) * 1000
                result["redirect_hops"] = redirect_hops
                result["final_url"] = current_url

                _log_network_event(
                    audit_logger, pack_id, domain, port, False,
                    reason=reason,
                    method=method, url=original_url, final_url=current_url,
                    latency_ms=result["latency_ms"],
                    redirect_hops=redirect_hops,
                    blocked_reason="grant_manager_unavailable",
                    check_type="proxy_request"
                )
                return result

            grant_result = network_grant_manager.check_access(pack_id, domain, port)
            if not grant_result.allowed:
                result["error"] = f"Network access denied: {grant_result.reason}"
                result["error_type"] = "grant_denied"
                result["latency_ms"] = (time.time() - start_time) * 1000
                result["redirect_hops"] = redirect_hops
                result["final_url"] = current_url

                _log_network_event(
                    audit_logger, pack_id, domain, port, False,
                    reason=grant_result.reason,
                    method=method, url=original_url, final_url=current_url,
                    latency_ms=result["latency_ms"],
                    redirect_hops=redirect_hops,
                    blocked_reason="grant_denied",
                    check_type="proxy_request"
                )
                return result

            # ============================================================
            # 4. HTTP接続 & リクエスト実行
            # (#13: DNS rebinding対策 — resolved IP に直接接続)
            # (W12-T046: 細粒度タイムアウト適用)
            # ============================================================
            conn: http.client.HTTPConnection | None = None
            # resolved_ips は resolve_and_check_ip() で取得済み (TOCTOU回避)
            connect_ip = resolved_ips[0] if resolved_ips else domain
            try:
                raw_sock = socket.create_connection((connect_ip, port), timeout=connect_timeout)
                raw_sock.settimeout(read_timeout)
                if parsed.scheme == "https":
                    context = ssl.create_default_context()
                    ssl_sock = context.wrap_socket(raw_sock, server_hostname=domain)
                    conn = http.client.HTTPSConnection(domain, port, timeout=read_timeout, context=context)
                    conn.sock = ssl_sock
                else:
                    conn = http.client.HTTPConnection(domain, port, timeout=read_timeout)
                    conn.sock = raw_sock

                path = parsed.path or "/"
                if parsed.query:
                    path = f"{path}?{parsed.query}"

                req_headers = dict(headers)
                if "Host" not in req_headers and "host" not in req_headers:
                    req_headers["Host"] = domain

                req_body = None
                if body is not None:
                    req_body = body.encode("utf-8") if isinstance(body, str) else body

                conn.request(method, path, body=req_body, headers=req_headers)
                resp = conn.getresponse()

                resp_headers = {k: v for k, v in resp.getheaders()}

                # ============================================================
                # B2: 巨大レスポンスは必ず失敗化
                # ============================================================
                resp_body, size_exceeded, read_bytes = read_response_with_limit(resp, MAX_RESPONSE_SIZE)
                bytes_read += read_bytes

                if size_exceeded:
                    result["error"] = f"Response too large (max: {MAX_RESPONSE_SIZE} bytes)"
                    result["error_type"] = "response_too_large"
                    result["latency_ms"] = (time.time() - start_time) * 1000
                    result["redirect_hops"] = redirect_hops
                    result["bytes_read"] = bytes_read
                    result["final_url"] = current_url

                    _log_network_event(
                        audit_logger, pack_id, domain, port, False,
                        reason=f"Response exceeded size limit: {read_bytes} bytes read, max {MAX_RESPONSE_SIZE}",
                        method=method, url=original_url, final_url=current_url,
                        latency_ms=result["latency_ms"],
                        status_code=resp.status,
                        redirect_hops=redirect_hops,
                        bytes_read=bytes_read,
                        error_type="response_too_large",
                        max_response_bytes=MAX_RESPONSE_SIZE,
                        check_type="proxy_request"
                    )
                    return result

                # 5. リダイレクト判定（GET/HEADのみfollow）
                if resp.status in (301, 302, 303, 307, 308) and method in ("GET", "HEAD"):
                    location = resp_headers.get("Location") or resp_headers.get("location")
                    if location:
                        redirect_hops += 1
                        if redirect_hops > MAX_REDIRECTS:
                            result["error"] = f"Too many redirects: {redirect_hops} > {MAX_REDIRECTS}"
                            result["error_type"] = "too_many_redirects"
                            result["latency_ms"] = (time.time() - start_time) * 1000
                            result["redirect_hops"] = redirect_hops
                            result["bytes_read"] = bytes_read
                            result["final_url"] = current_url

                            _log_network_event(
                                audit_logger, pack_id, domain, port, False,
                                reason=result["error"],
                                method=method, url=original_url, final_url=current_url,
                                latency_ms=result["latency_ms"],
                                error_type="too_many_redirects",
                                redirect_hops=redirect_hops,
                                bytes_read=bytes_read,
                                check_type="proxy_request"
                            )
                            return result

                        current_url = urljoin(current_url, location)
                        final_url = current_url
                        continue

                # 6. 成功
                try:
                    body_str = resp_body.decode("utf-8")
                except UnicodeDecodeError:
                    body_str = base64.b64encode(resp_body).decode("ascii")
                    resp_headers["X-Proxy-Body-Encoding"] = "base64"

                result["success"] = True
                result["status_code"] = resp.status
                result["headers"] = resp_headers
                result["body"] = body_str
                result["final_url"] = final_url
                result["redirect_hops"] = redirect_hops
                result["bytes_read"] = bytes_read
                result["latency_ms"] = (time.time() - start_time) * 1000

                _log_network_event(
                    audit_logger, pack_id, domain, port, True,
                    method=method, url=original_url, final_url=final_url,
                    latency_ms=result["latency_ms"],
                    status_code=resp.status,
                    redirect_hops=redirect_hops,
                    bytes_read=bytes_read,
                    check_type="proxy_request"
                )

                return result

            except socket.timeout:
                result["error"] = f"Request timed out (connect={connect_timeout}s, read={read_timeout}s)"
                result["error_type"] = "timeout"
                break
            except ssl.SSLError as e:
                result["error"] = f"SSL error: {e}"
                result["error_type"] = "ssl_error"
                break
            except ConnectionRefusedError:
                result["error"] = f"Connection refused to {domain}:{port}"
                result["error_type"] = "connection_refused"
                break
            except Exception as e:
                result["error"] = str(e)
                result["error_type"] = type(e).__name__
                break
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = type(e).__name__

    # エラー終了時の共通処理
    result["latency_ms"] = (time.time() - start_time) * 1000
    result["redirect_hops"] = redirect_hops
    result["bytes_read"] = bytes_read
    result["final_url"] = final_url

    _log_network_event(
        audit_logger, pack_id, last_domain, last_port, False,
        reason=str(result.get("error") or ""),
        method=method, url=original_url, final_url=final_url,
        latency_ms=result["latency_ms"],
        error_type=str(result.get("error_type") or ""),
        redirect_hops=redirect_hops,
        bytes_read=bytes_read,
        check_type="proxy_request"
    )

    return result


# ============================================================
# UDSソケット管理
# ============================================================

class UDSSocketManager:
    """Pack別UDSソケット管理"""

    if sys.platform == "win32":
        _win_base = str(Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")))
                        / "rumi" / "egress" / "packs")
        DEFAULT_BASE_DIR = _win_base
        FALLBACK_BASE_DIR = _win_base
    else:
        DEFAULT_BASE_DIR = "/run/rumi/egress/packs"
        FALLBACK_BASE_DIR = "/tmp/rumi/egress/packs"

    def __init__(self):
        self._base_dir: Optional[Path] = None
        self._active_sockets: Dict[str, Path] = {}
        # BUG-5-1: TCP port management
        self._tcp_ports: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _get_base_dir(self) -> Path:
        """ベースディレクトリを取得（環境変数 > デフォルト > フォールバック）"""
        if self._base_dir is not None:
            return self._base_dir

        # 環境変数
        env_dir = os.environ.get("RUMI_EGRESS_SOCK_DIR")
        if env_dir:
            path = Path(env_dir)
            try:
                path.mkdir(parents=True, exist_ok=True)
                _apply_egress_dir_permissions(path)
                self._base_dir = path
                return path
            except Exception:
                pass

        # デフォルト試行
        default = Path(self.DEFAULT_BASE_DIR)
        try:
            default.mkdir(parents=True, exist_ok=True)
            _apply_egress_dir_permissions(default)
            self._base_dir = default
            return default
        except Exception:
            pass

        # フォールバック
        fallback = Path(self.FALLBACK_BASE_DIR)
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            _apply_egress_dir_permissions(fallback)
            self._base_dir = fallback
            return fallback
        except Exception as e:
            raise RuntimeError(f"Failed to create socket directory: {e}")

    def get_socket_path(self, pack_id: str) -> Path:
        """Pack IDからソケットパスを取得"""
        return self._get_base_dir() / _pack_socket_name(pack_id)

    def ensure_socket(self, pack_id: str) -> Tuple[bool, str, Optional[Path]]:
        """ソケットファイルを確保（docker run前に必須）"""
        with self._lock:
            try:
                sock_path = self.get_socket_path(pack_id)

                # symlink 検出（TOCTOU 完全対策ではないが、攻撃痕跡の検出に有効）
                if sock_path.is_symlink():
                    _audit_egress_permission_warning(
                        "symlink_detected",
                        str(sock_path),
                        f"Symlink detected at socket path, possible attack: {sock_path}"
                    )
                    try:
                        sock_path.unlink()
                    except Exception as e:
                        return False, f"Failed to remove symlink at socket path: {e}", None

                # 既存ソケットを削除
                if sock_path.exists():
                    try:
                        sock_path.unlink()
                    except Exception as e:
                        return False, f"Failed to remove existing socket: {e}", None

                # 親ディレクトリ確保
                try:
                    sock_path.parent.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    return False, f"Failed to create socket directory: {e}", None

                # ディレクトリパーミッション強化
                _apply_egress_dir_permissions(sock_path.parent)

                self._active_sockets[pack_id] = sock_path
                return True, "", sock_path
            except Exception as e:
                return False, f"Failed to ensure socket: {e}", None

    def cleanup_socket(self, pack_id: str) -> None:
        """ソケットをクリーンアップ"""
        with self._lock:
            if pack_id in self._active_sockets:
                sock_path = self._active_sockets.pop(pack_id)
                try:
                    if sock_path.exists():
                        sock_path.unlink()
                except Exception:
                    pass

    # BUG-5-1: TCP port management methods
    def get_port(self, pack_id: str) -> Optional[int]:
        """Pack の TCP ポート番号を取得"""
        with self._lock:
            return self._tcp_ports.get(pack_id)

    def set_port(self, pack_id: str, port: int) -> None:
        """Pack の TCP ポート番号を設定"""
        with self._lock:
            self._tcp_ports[pack_id] = port

    def clear_port(self, pack_id: str) -> None:
        """Pack の TCP ポート番号をクリア"""
        with self._lock:
            self._tcp_ports.pop(pack_id, None)

    def get_base_dir_path(self) -> Path:
        """ベースディレクトリパスを取得"""
        return self._get_base_dir()


# ============================================================
# UDSサーバー（Pack別）
# ============================================================

class UDSEgressServer:
    """Pack別UDSソケットでリッスンするEgressサーバー"""

    def __init__(self, pack_id: str, socket_path: Path, network_grant_manager, audit_logger,
                 rate_limiter: PackRateLimiter | None = None,
                 domain_controller: DomainController | None = None):
        self.pack_id = pack_id
        self.socket_path = socket_path
        self._network_grant_manager = network_grant_manager
        self._audit_logger = audit_logger
        self._rate_limiter = rate_limiter
        self._domain_controller = domain_controller
        self._server_socket: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # #33: スレッドプール化
        self._max_workers = int(os.environ.get("RUMI_EGRESS_MAX_WORKERS", "20"))
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._worker_semaphore: Optional[threading.Semaphore] = None
        # BUG-5-1: TCP fallback params
        self._is_tcp = False
        self._tcp_port: Optional[int] = None
        self._ipc_auth: Optional["IpcAuthManager"] = None

    def start(self) -> bool:
        """サーバーを起動"""
        if _IS_WINDOWS:
            # BUG-5-1: TCP fallback for Windows
            return self._start_tcp()
        with self._lock:
            if self._running:
                return True

            try:
                # 既存ソケットファイルを削除
                if self.socket_path.exists():
                    self.socket_path.unlink()

                # UDSソケット作成
                self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._server_socket.bind(str(self.socket_path))
                self._server_socket.listen(5)
                self._server_socket.settimeout(1.0)  # accept用タイムアウト

                # パーミッション設定（best-effort、capability_proxyと同方式）
                _apply_egress_socket_permissions(self.socket_path)

                self._running = True
                # #33: スレッドプール初期化
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix=f"egress-{self.pack_id[:16]}",
                )
                self._worker_semaphore = threading.Semaphore(self._max_workers)
                self._thread = threading.Thread(target=self._serve_forever, daemon=True)
                self._thread.start()

                print(f"[UDSEgressServer] Started for pack '{self.pack_id}' at {self.socket_path}")
                return True
            except Exception as e:
                print(f"[UDSEgressServer] Failed to start for {self.pack_id}: {e}")
                self._cleanup()
                return False

    def stop(self) -> None:
        """サーバーを停止"""
        with self._lock:
            self._running = False
            if self._server_socket:
                try:
                    self._server_socket.close()
                except Exception:
                    pass
            # #33: スレッドプールをシャットダウン
            if self._executor:
                try:
                    self._executor.shutdown(wait=False)
                except Exception:
                    pass
                self._executor = None
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5)
            self._cleanup()
            print(f"[UDSEgressServer] Stopped for pack '{self.pack_id}'")

    def _cleanup(self) -> None:
        """クリーンアップ"""
        try:
            if self.socket_path and self.socket_path.exists():
                self.socket_path.unlink()
        except Exception:
            pass
        self._server_socket = None
        self._thread = None

    def _serve_forever(self) -> None:
        """リクエストを処理し続ける (#33: ThreadPoolExecutor方式)"""
        while self._running:
            try:
                server_socket = self._server_socket
                worker_semaphore = self._worker_semaphore
                executor = self._executor
                if server_socket is None or worker_semaphore is None or executor is None:
                    return
                client_sock, _ = server_socket.accept()
                # #33: セマフォで枯渇検知
                if not worker_semaphore.acquire(blocking=False):
                    # プール枯渇: 接続を拒否
                    print(
                        f"[UDSEgressServer] Thread pool exhausted for "
                        f"pack '{self.pack_id}' (max_workers={self._max_workers}). "
                        f"Rejecting connection."
                    )
                    try:
                        response = {
                            "success": False,
                            "error": "Server busy: thread pool exhausted",
                            "error_type": "pool_exhausted",
                        }
                        write_length_prefixed_json(client_sock, response)
                    except Exception:
                        pass
                    finally:
                        try:
                            client_sock.close()
                        except Exception:
                            pass
                    continue

                # スレッドプールに投入
                try:
                    executor.submit(self._handle_client_pooled, client_sock)
                except RuntimeError:
                    # executor が shutdown 済み
                    worker_semaphore.release()
                    try:
                        client_sock.close()
                    except Exception:
                        pass
                    if self._running:
                        break
            except socket.timeout:
                continue
            except OSError:
                # ソケットがクローズされた
                if self._running:
                    break
            except Exception as e:
                if self._running:
                    print(f"[UDSEgressServer] Accept error for {self.pack_id}: {e}")

    def _start_tcp(self) -> bool:
        """Windows TCP フォールバック: TCP localhost サーバーを起動 (BUG-5-1)"""
        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind(("127.0.0.1", 0))
            self._tcp_port = self._server_socket.getsockname()[1]
            self._server_socket.listen(5)
            self._server_socket.settimeout(1.0)
            self._is_tcp = True

            self._running = True
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix=f"egress-tcp-{self.pack_id[:16]}",
            )
            self._worker_semaphore = threading.Semaphore(self._max_workers)
            self._thread = threading.Thread(target=self._serve_forever, daemon=True)
            self._thread.start()

            print(f"[UDSEgressServer] TCP started for pack '{self.pack_id}' on port {self._tcp_port}")
            return True
        except Exception as e:
            print(f"[UDSEgressServer] TCP start failed for {self.pack_id}: {e}")
            self._cleanup()
            return False

    def _handle_client_tcp(self, client_sock: socket.socket) -> None:
        """TCP クライアント: 認証後に _handle_client に委譲 (BUG-5-1)"""
        try:
            if self._ipc_auth is None:
                try:
                    client_sock.close()
                except Exception:
                    pass
                return

            authed_pack_id = perform_server_auth(client_sock, self._ipc_auth)
            if authed_pack_id is None:
                try:
                    client_sock.close()
                except Exception:
                    pass
                return

            if authed_pack_id != self.pack_id:
                try:
                    write_length_prefixed_json(client_sock, {
                        "success": False,
                        "error": "Pack ID mismatch",
                        "error_type": "auth_error",
                    })
                except Exception:
                    pass
                try:
                    client_sock.close()
                except Exception:
                    pass
                return

            self._handle_client(client_sock)
        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass

    def _handle_client_pooled(self, client_sock: socket.socket) -> None:
        """スレッドプール用クライアントハンドラ (セマフォ解放付き) (#33)"""
        try:
            # BUG-5-1: TCP auth delegation
            if self._is_tcp:
                self._handle_client_tcp(client_sock)
            else:
                self._handle_client(client_sock)
        finally:
            worker_semaphore = self._worker_semaphore
            if worker_semaphore is not None:
                worker_semaphore.release()

    def _handle_client(self, client_sock: socket.socket) -> None:
        """クライアント接続を処理"""
        try:
            client_sock.settimeout(DEFAULT_TIMEOUT)

            # リクエスト読み取り
            request = read_length_prefixed_json(client_sock, MAX_REQUEST_SIZE)
            if request is None:
                return

            # バリデーション
            valid, reason = validate_request(request)
            if not valid:
                response = {
                    "success": False,
                    "error": reason,
                    "error_type": "validation_error"
                }
                write_length_prefixed_json(client_sock, response)
                return

            # pack_id はソケットパスから確定済み（payloadのowner_packは無視）
            response = execute_http_request(
                pack_id=self.pack_id,
                request=request,
                network_grant_manager=self._network_grant_manager,
                audit_logger=self._audit_logger,
                rate_limiter=self._rate_limiter,
                domain_controller=self._domain_controller,
            )

            write_length_prefixed_json(client_sock, response)

        except ValueError as e:
            # サイズ超過などのプロトコルエラー
            try:
                response = {
                    "success": False,
                    "error": str(e),
                    "error_type": "protocol_error"
                }
                write_length_prefixed_json(client_sock, response)
            except Exception:
                pass
        except Exception as e:
            try:
                response = {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__
                }
                write_length_prefixed_json(client_sock, response)
            except Exception:
                pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass


# ============================================================
# UDS Egress Proxy Manager
# ============================================================

class UDSEgressProxyManager:
    """Pack別UDSサーバーを管理するマネージャ"""

    def __init__(self, network_grant_manager=None, audit_logger=None):
        self._socket_manager = UDSSocketManager()
        self._servers: Dict[str, UDSEgressServer] = {}
        self._network_grant_manager = network_grant_manager
        self._audit_logger = audit_logger
        self._lock = threading.Lock()
        self._rate_limiter = PackRateLimiter()
        # Deny until the selected Profile supplies an immutable Pack v4 policy.
        self._domain_controller = DomainController({})
        # BUG-5-1: IPC auth manager
        self._ipc_auth = IpcAuthManager() if _IS_WINDOWS else None

    def set_network_grant_manager(self, manager) -> None:
        """NetworkGrantManagerを設定"""
        self._network_grant_manager = manager
        with self._lock:
            for server in self._servers.values():
                server._network_grant_manager = manager

    def set_audit_logger(self, logger) -> None:
        """AuditLoggerを設定"""
        self._audit_logger = logger
        with self._lock:
            for server in self._servers.values():
                server._audit_logger = logger

    def set_rate_limiter(self, limiter: PackRateLimiter) -> None:
        """PackRateLimiterを設定"""
        self._rate_limiter = limiter
        with self._lock:
            for server in self._servers.values():
                server._rate_limiter = limiter

    def set_domain_controller(self, controller: DomainController) -> None:
        """DomainControllerを設定"""
        self._domain_controller = controller
        with self._lock:
            for server in self._servers.values():
                server._domain_controller = controller

    def ensure_pack_socket(self, pack_id: str) -> Tuple[bool, str, Optional[Path]]:
        """Pack用のソケットを確保しサーバーを起動"""
        with self._lock:
            # BUG-5-1: Windows TCP fallback
            if _IS_WINDOWS:
                return self._ensure_pack_tcp(pack_id)

            # 既に起動済みならそのまま返す
            if pack_id in self._servers:
                server = self._servers[pack_id]
                return True, "", server.socket_path

            # ソケットパス確保
            success, error, sock_path = self._socket_manager.ensure_socket(pack_id)
            if not success or sock_path is None:
                return False, error, None

            # サーバー起動
            server = UDSEgressServer(
                pack_id=pack_id,
                socket_path=sock_path,
                network_grant_manager=self._network_grant_manager,
                audit_logger=self._audit_logger,
                rate_limiter=self._rate_limiter,
                domain_controller=self._domain_controller,
            )

            if not server.start():
                self._socket_manager.cleanup_socket(pack_id)
                return False, f"Failed to start UDS server for {pack_id}", None

            self._servers[pack_id] = server
            return True, "", sock_path

    def _ensure_pack_tcp(self, pack_id: str) -> Tuple[bool, str, Optional[Path]]:
        """Windows TCP フォールバック: TCP サーバーを起動 (BUG-5-1)"""
        if pack_id in self._servers:
            server = self._servers[pack_id]
            if server._tcp_port is not None:
                return True, "", None
            self._servers[pack_id].stop()
            del self._servers[pack_id]

        if self._ipc_auth is None:
            self._ipc_auth = IpcAuthManager()
        self._ipc_auth.generate_token(pack_id)

        success, error, sock_path = self._socket_manager.ensure_socket(pack_id)
        # Windows ではソケットファイル不要。失敗しても続行。

        server = UDSEgressServer(
            pack_id=pack_id,
            socket_path=sock_path or Path("."),
            network_grant_manager=self._network_grant_manager,
            audit_logger=self._audit_logger,
            rate_limiter=self._rate_limiter,
            domain_controller=self._domain_controller,
        )
        server._ipc_auth = self._ipc_auth

        if not server.start():
            return False, f"Failed to start TCP server for {pack_id}", None

        tcp_port = server._tcp_port
        if tcp_port is None:
            server.stop()
            return False, f"TCP server did not allocate a port for {pack_id}", None

        self._servers[pack_id] = server
        self._socket_manager.set_port(pack_id, tcp_port)
        return True, "", None

    def get_auth_token(self, pack_id: str) -> Optional[str]:
        """Pack の認証トークンを取得 (BUG-5-1)"""
        if self._ipc_auth is None:
            return None
        return self._ipc_auth.get_token(pack_id)

    def get_tcp_port(self, pack_id: str) -> Optional[int]:
        """Pack の TCP ポート番号を取得 (BUG-5-1)"""
        with self._lock:
            if pack_id in self._servers:
                return self._servers[pack_id]._tcp_port
            return None

    def get_socket_path(self, pack_id: str) -> Optional[Path]:
        """Pack用ソケットパスを取得（存在する場合）"""
        with self._lock:
            if pack_id in self._servers:
                return self._servers[pack_id].socket_path
            return None

    def stop_pack_server(self, pack_id: str) -> None:
        """Pack用サーバーを停止"""
        with self._lock:
            if pack_id in self._servers:
                self._servers[pack_id].stop()
                del self._servers[pack_id]
            self._socket_manager.cleanup_socket(pack_id)
            # BUG-5-1: TCP cleanup
            self._socket_manager.clear_port(pack_id)
            if self._ipc_auth is not None:
                self._ipc_auth.revoke_token(pack_id)

    def stop_all(self) -> None:
        """全サーバーを停止"""
        with self._lock:
            for pack_id in list(self._servers.keys()):
                self._servers[pack_id].stop()
                self._socket_manager.cleanup_socket(pack_id)
            self._servers.clear()
        print("[UDSEgressProxyManager] All servers stopped")

    def is_running(self, pack_id: str) -> bool:
        """Pack用サーバーが起動中か"""
        with self._lock:
            return pack_id in self._servers

    def list_active_packs(self) -> List[str]:
        """アクティブなPack一覧"""
        with self._lock:
            return list(self._servers.keys())

    def get_base_dir(self) -> Path:
        """ソケットのベースディレクトリを取得"""
        return self._socket_manager.get_base_dir_path()


# ============================================================
# グローバルインスタンス
# ============================================================

_global_uds_proxy_manager: Optional[UDSEgressProxyManager] = None
_uds_proxy_lock = threading.Lock()


def get_uds_egress_proxy_manager() -> UDSEgressProxyManager:
    """
    グローバルなUDSEgressProxyManagerを取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。
    """
    from .di_container import get_container
    return get_container().get("egress_proxy_manager")


def initialize_uds_egress_proxy(
    network_grant_manager=None,
    audit_logger=None
) -> UDSEgressProxyManager:
    """UDSEgressProxyManagerを初期化"""
    global _global_uds_proxy_manager
    with _uds_proxy_lock:
        if _global_uds_proxy_manager:
            _global_uds_proxy_manager.stop_all()
        _global_uds_proxy_manager = UDSEgressProxyManager(
            network_grant_manager=network_grant_manager,
            audit_logger=audit_logger
        )
    # DI コンテナのキャッシュも更新
    from .di_container import get_container
    get_container().set_instance("egress_proxy_manager", _global_uds_proxy_manager)
    return _global_uds_proxy_manager


def shutdown_uds_egress_proxy() -> None:
    """UDSEgressProxyManagerをシャットダウン"""
    global _global_uds_proxy_manager
    with _uds_proxy_lock:
        if _global_uds_proxy_manager:
            _global_uds_proxy_manager.stop_all()
            _global_uds_proxy_manager = None
    # DI コンテナのキャッシュもクリア
    try:
        from .di_container import get_container
        get_container().reset("egress_proxy_manager")
    except Exception:
        pass


# ============================================================
# 以下、既存のTCP版プロキシ（後方互換性のため維持）
# ============================================================


@dataclass
class ProxyRequest:
    """プロキシリクエスト"""
    owner_pack: str
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[bytes]
    timeout_seconds: float = 30.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ProxyRequest':
        return cls(
            owner_pack=data.get("owner_pack", ""),
            method=data.get("method", "GET").upper(),
            url=data.get("url", ""),
            headers=data.get("headers", {}),
            body=data.get("body", "").encode("utf-8") if data.get("body") else None,
            timeout_seconds=data.get("timeout_seconds", 30.0),
        )


@dataclass
class ProxyResponse:
    """プロキシレスポンス"""
    success: bool
    status_code: int = 0
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    error: Optional[str] = None
    error_type: Optional[str] = None
    allowed: bool = True
    rejection_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "success": self.success,
            "status_code": self.status_code,
            "headers": self.headers,
            "body": self.body,
            "allowed": self.allowed,
        }
        if self.error:
            d["error"] = self.error
            d["error_type"] = self.error_type
        if self.rejection_reason:
            d["rejection_reason"] = self.rejection_reason
        return d


class EgressHTTPServer(HTTPServer):
    """カスタムHTTPServer（インスタンス変数でマネージャを保持）"""

    def __init__(self, server_address, RequestHandlerClass, network_grant_manager=None, audit_logger=None):
        super().__init__(server_address, RequestHandlerClass)
        self.network_grant_manager = network_grant_manager
        self.audit_logger = audit_logger
        self.allowed_internal_ips = ["127.0.0.1", "::1", "localhost"]


class EgressProxyHandler(BaseHTTPRequestHandler):
    """HTTPリクエストハンドラ"""

    def log_message(self, format: str, *args) -> None:
        pass

    @property
    def network_grant_manager(self):
        server = self.server
        if not isinstance(server, EgressHTTPServer):
            raise RuntimeError("egress handler is attached to an invalid server")
        return server.network_grant_manager

    @property
    def audit_logger(self):
        server = self.server
        if not isinstance(server, EgressHTTPServer):
            raise RuntimeError("egress handler is attached to an invalid server")
        return server.audit_logger

    @property
    def allowed_internal_ips(self):
        server = self.server
        if not isinstance(server, EgressHTTPServer):
            raise RuntimeError("egress handler is attached to an invalid server")
        return server.allowed_internal_ips

    def _send_json_response(self, status_code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _check_client_allowed(self) -> bool:
        client_ip = self.client_address[0]
        return client_ip in self.allowed_internal_ips

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Owner-Pack")
        self.end_headers()

    def do_POST(self) -> None:
        if not self._check_client_allowed():
            self._send_json_response(403, {"success": False, "error": "Forbidden: Only local connections allowed", "error_type": "forbidden_client"})
            return

        if self.path != "/proxy/request":
            self._send_json_response(404, {"success": False, "error": f"Not found: {self.path}", "error_type": "not_found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            request_data = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError as e:
            self._send_json_response(400, {"success": False, "error": f"Invalid JSON: {e}", "error_type": "invalid_json"})
            return
        except Exception as e:
            self._send_json_response(400, {"success": False, "error": f"Request error: {e}", "error_type": "request_error"})
            return

        owner_pack = self.headers.get("X-Owner-Pack") or request_data.get("owner_pack")
        if not owner_pack:
            self._send_json_response(400, {"success": False, "error": "Missing owner_pack", "error_type": "missing_owner_pack"})
            return

        request_data["owner_pack"] = owner_pack

        try:
            proxy_request = ProxyRequest.from_dict(request_data)
            response = self._process_request(proxy_request)
            status = 200 if response.success else (403 if not response.allowed else 502)
            self._send_json_response(status, response.to_dict())
        except Exception as e:
            self._send_json_response(500, {"success": False, "error": str(e), "error_type": type(e).__name__})

    def _process_request(self, request: ProxyRequest) -> ProxyResponse:
        """リクエストを処理"""
        try:
            parsed = urlparse(request.url)
            domain = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except Exception as e:
            response = ProxyResponse(
                success=False,
                error=f"Invalid URL: {e}",
                error_type="invalid_url",
                allowed=False
            )
            self._log_request(
                request, "", 0, False, 0,
                error=str(e), allowed=False, rejection_reason="invalid_url"
            )
            return response

        # ネットワーク権限チェック
        if self.network_grant_manager:
            check_result = self.network_grant_manager.check_access(request.owner_pack, domain, port)
            if not check_result.allowed:
                response = ProxyResponse(
                    success=False,
                    allowed=False,
                    rejection_reason=check_result.reason,
                    error=f"Network access denied: {check_result.reason}",
                    error_type="network_denied"
                )
                # 拒否を監査ログに記録
                self._log_request(
                    request, domain, port, False, 0,
                    error=response.error or "", allowed=False,
                    rejection_reason=check_result.reason
                )
                return response

        # HTTPリクエストを実行
        try:
            response = self._execute_http_request(request, domain, port)
            # 成功/失敗を監査ログに記録
            self._log_request(
                request, domain, port,
                success=response.success,
                status_code=response.status_code,
                error=response.error if not response.success else None,
                allowed=True
            )
            return response
        except Exception as e:
            error_msg = str(e)
            response = ProxyResponse(
                success=False,
                error=error_msg,
                error_type=type(e).__name__,
                allowed=True  # 許可はされたが実行失敗
            )
            # 実行失敗を監査ログに記録
            self._log_request(
                request, domain, port, False, 0,
                error=error_msg, allowed=True
            )
            return response

    def _execute_http_request(self, request: ProxyRequest, domain: str, port: int) -> ProxyResponse:
        """HTTPリクエストを実行"""
        try:
            parsed = urlparse(request.url)

            conn: http.client.HTTPConnection
            if parsed.scheme == "https":
                context = ssl.create_default_context()
                conn = http.client.HTTPSConnection(domain, port, timeout=request.timeout_seconds, context=context)
            else:
                conn = http.client.HTTPConnection(domain, port, timeout=request.timeout_seconds)

            try:
                path = parsed.path or "/"
                if parsed.query:
                    path = f"{path}?{parsed.query}"

                headers = dict(request.headers)
                if "Host" not in headers:
                    headers["Host"] = domain

                conn.request(request.method, path, body=request.body, headers=headers)
                resp = conn.getresponse()

                resp_headers = {}
                for key, value in resp.getheaders():
                    resp_headers[key] = value

                resp_body = resp.read()

                try:
                    body_str = resp_body.decode("utf-8")
                except UnicodeDecodeError:
                    body_str = base64.b64encode(resp_body).decode("ascii")
                    resp_headers["X-Proxy-Body-Encoding"] = "base64"

                return ProxyResponse(
                    success=True,
                    status_code=resp.status,
                    headers=resp_headers,
                    body=body_str,
                    allowed=True
                )
            finally:
                conn.close()
        except socket.timeout:
            return ProxyResponse(
                success=False,
                error="Request timed out",
                error_type="timeout",
                allowed=True  # 許可はされたがタイムアウト
            )
        except Exception as e:
            return ProxyResponse(
                success=False,
                error=str(e),
                error_type=type(e).__name__,
                allowed=True  # 許可はされたが実行失敗
            )

    def _log_request(
        self,
        request: ProxyRequest,
        domain: str,
        port: int,
        success: bool,
        status_code: int,
        error: str | None = None,
        allowed: bool = True,
        rejection_reason: str | None = None
    ) -> None:
        """リクエストを監査ログに記録"""
        if self.audit_logger:
            try:
                self.audit_logger.log_network_event(
                    pack_id=request.owner_pack,
                    domain=domain,
                    port=port,
                    allowed=allowed,
                    reason=rejection_reason if not allowed else None,
                    request_details={
                        "method": request.method,
                        "url": request.url,
                        "success": success,
                        "status_code": status_code,
                        "error": error,
                    }
                )
            except Exception:
                pass


class EgressProxyServer:
    """Egress Proxy サーバー"""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 8766

    def __init__(self, host: str | None = None, port: int | None = None,
                 network_grant_manager=None, audit_logger=None):
        self.host = host or self.DEFAULT_HOST
        self.port = port or self.DEFAULT_PORT
        self._network_grant_manager = network_grant_manager
        self._audit_logger = audit_logger
        self._server: Optional[EgressHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self._server is not None:
                return False
            try:
                self._server = EgressHTTPServer(
                    (self.host, self.port),
                    EgressProxyHandler,
                    network_grant_manager=self._network_grant_manager,
                    audit_logger=self._audit_logger
                )
                self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
                self._thread.start()
                print(f"[EgressProxy] Started on http://{self.host}:{self.port}")
                if self._audit_logger:
                    self._audit_logger.log_system_event(event_type="egress_proxy_start", success=True, details={"host": self.host, "port": self.port})
                return True
            except Exception as e:
                print(f"[EgressProxy] Failed to start: {e}")
                self._server = None
                self._thread = None
                return False

    def stop(self) -> bool:
        with self._lock:
            if self._server is None:
                return False
            try:
                self._server.shutdown()
                self._server = None
                if self._thread:
                    self._thread.join(timeout=5)
                    self._thread = None
                print("[EgressProxy] Stopped")
                if self._audit_logger:
                    self._audit_logger.log_system_event(event_type="egress_proxy_stop", success=True)
                return True
            except Exception as e:
                print(f"[EgressProxy] Error stopping: {e}")
                return False

    def is_running(self) -> bool:
        with self._lock:
            return self._server is not None and self._thread is not None and self._thread.is_alive()

    def get_endpoint(self) -> str:
        return f"http://{self.host}:{self.port}/proxy/request"

    def set_network_grant_manager(self, manager) -> None:
        self._network_grant_manager = manager
        if self._server:
            self._server.network_grant_manager = manager

    def set_audit_logger(self, logger) -> None:
        self._audit_logger = logger
        if self._server:
            self._server.audit_logger = logger


def make_proxy_request(proxy_url: str, owner_pack: str, method: str, url: str,
                       headers: Dict[str, str] | None = None,
                       body: str | None = None,
                       timeout_seconds: float = 30.0) -> ProxyResponse:
    """プロキシ経由でHTTPリクエストを送信"""
    try:
        parsed = urlparse(proxy_url)
        hostname = parsed.hostname
        if hostname is None:
            raise ValueError("proxy URL must include a hostname")
        conn = http.client.HTTPConnection(
            hostname, parsed.port or 80, timeout=timeout_seconds + 5
        )
        try:
            request_body = json.dumps({
                "owner_pack": owner_pack, "method": method, "url": url,
                "headers": headers or {}, "body": body or "", "timeout_seconds": timeout_seconds,
            }).encode("utf-8")
            conn.request("POST", parsed.path or "/proxy/request", body=request_body, headers={"Content-Type": "application/json", "X-Owner-Pack": owner_pack})
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8")
            resp_data = json.loads(resp_body)
            return ProxyResponse(
                success=resp_data.get("success", False), status_code=resp_data.get("status_code", 0),
                headers=resp_data.get("headers", {}), body=resp_data.get("body", ""),
                error=resp_data.get("error"), error_type=resp_data.get("error_type"),
                allowed=resp_data.get("allowed", True), rejection_reason=resp_data.get("rejection_reason"),
            )
        finally:
            conn.close()
    except Exception as e:
        return ProxyResponse(success=False, error=str(e), error_type=type(e).__name__)


_global_egress_proxy: Optional[EgressProxyServer] = None
_proxy_lock = threading.Lock()


def get_egress_proxy() -> EgressProxyServer:
    global _global_egress_proxy
    if _global_egress_proxy is None:
        with _proxy_lock:
            if _global_egress_proxy is None:
                _global_egress_proxy = EgressProxyServer()
    return _global_egress_proxy


def initialize_egress_proxy(host: str | None = None, port: int | None = None,
                            network_grant_manager=None, audit_logger=None,
                            auto_start: bool = True) -> EgressProxyServer:
    global _global_egress_proxy
    with _proxy_lock:
        if _global_egress_proxy and _global_egress_proxy.is_running():
            _global_egress_proxy.stop()
        _global_egress_proxy = EgressProxyServer(host=host, port=port, network_grant_manager=network_grant_manager, audit_logger=audit_logger)
        if auto_start:
            _global_egress_proxy.start()
    return _global_egress_proxy


def shutdown_egress_proxy() -> None:
    global _global_egress_proxy
    with _proxy_lock:
        if _global_egress_proxy:
            _global_egress_proxy.stop()
            _global_egress_proxy = None
