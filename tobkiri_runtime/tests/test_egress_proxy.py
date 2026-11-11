"""
test_egress_proxy.py - egress_proxy.py regression tests (Wave 12 T-044)

Covers:
  - is_internal_ip(): IPv4/IPv6 blocked ranges, boundary values, external IPs
  - resolve_and_check_ip(): mock getaddrinfo for normal/internal/DNS failure
  - validate_request(): field validation (method, url, headers, timeout)
  - read_length_prefixed_json / write_length_prefixed_json: round-trip, oversize
  - read_response_with_limit(): Content-Length exceeded, chunk exceeded, normal
  - _pack_socket_name(): deterministic hash, distinct names
  - UDSSocketManager: get_socket_path, ensure_socket, cleanup_socket (tmp_path)
  - Permission utilities: _get_egress_socket_mode, _get_egress_socket_gid
"""
from __future__ import annotations

import io
import json
import os
import socket
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from core_runtime.egress_proxy import (
    is_internal_ip,
    resolve_and_check_ip,
    validate_request,
    read_length_prefixed_json,
    write_length_prefixed_json,
    read_response_with_limit,
    _pack_socket_name,
    UDSSocketManager,
    _get_egress_socket_mode,
    _get_egress_socket_gid,
    _is_ip_literal,
    ALLOWED_METHODS,
    MAX_HEADER_COUNT,
    MAX_HEADER_NAME_LENGTH,
    MAX_HEADER_VALUE_LENGTH,
    MAX_TIMEOUT,
    DEFAULT_TIMEOUT,
    MAX_REQUEST_SIZE,
    MAX_RESPONSE_SIZE,
    _EGRESS_DEFAULT_SOCKET_MODE,
    _EGRESS_RELAXED_SOCKET_MODE,
    execute_http_request,
)


# ======================================================================
# is_internal_ip
# ======================================================================

class TestIsInternalIp:
    """is_internal_ip() boundary and range tests."""

    # --- IPv4 blocked ranges ---

    @pytest.mark.parametrize("ip,expected_blocked", [
        ("0.0.0.0", True),       # "this network" 0.0.0.0/8
        ("0.255.255.255", True),
        ("10.0.0.0", True),      # private 10.0.0.0/8
        ("10.255.255.255", True),
        ("100.64.0.0", True),    # CGNAT 100.64.0.0/10
        ("100.127.255.255", True),
        ("127.0.0.1", True),     # loopback
        ("127.255.255.254", True),
        ("169.254.0.1", True),   # link-local
        ("172.16.0.0", True),    # private 172.16.0.0/12
        ("172.31.255.255", True),
        ("192.168.0.0", True),   # private 192.168.0.0/16
        ("192.168.255.255", True),
        ("224.0.0.1", True),     # multicast
        ("239.255.255.255", True),
        ("240.0.0.1", True),     # reserved
        ("255.255.255.255", True),  # broadcast
    ])
    def test_ipv4_blocked(self, ip: str, expected_blocked: bool):
        blocked, reason = is_internal_ip(ip)
        assert blocked is expected_blocked
        assert reason != ""

    @pytest.mark.parametrize("ip", [
        "1.1.1.1",
        "8.8.8.8",
        "100.128.0.0",   # just outside CGNAT
        "11.0.0.0",      # just outside 10/8
        "172.32.0.0",    # just outside 172.16/12
        "192.169.0.0",   # just outside 192.168/16
        "223.255.255.255",  # just below multicast
    ])
    def test_ipv4_external(self, ip: str):
        blocked, reason = is_internal_ip(ip)
        assert blocked is False
        assert reason == ""

    # --- IPv6 blocked ranges ---

    @pytest.mark.parametrize("ip,expected_blocked", [
        ("::", True),           # unspecified
        ("::1", True),          # loopback
        ("fc00::1", True),      # ULA
        ("fdff::1", True),      # ULA upper bound
        ("fe80::1", True),      # link-local
        ("ff00::1", True),      # multicast
    ])
    def test_ipv6_blocked(self, ip: str, expected_blocked: bool):
        blocked, reason = is_internal_ip(ip)
        assert blocked is expected_blocked

    @pytest.mark.parametrize("ip", [
        "2001:4860:4860::8888",
        "2606:4700::1111",
    ])
    def test_ipv6_external(self, ip: str):
        blocked, reason = is_internal_ip(ip)
        assert blocked is False

    def test_invalid_ip_string(self):
        blocked, reason = is_internal_ip("not-an-ip")
        assert blocked is True
        assert "Invalid" in reason

    def test_empty_ip_string(self):
        blocked, reason = is_internal_ip("")
        assert blocked is True


# ======================================================================
# resolve_and_check_ip
# ======================================================================

class TestResolveAndCheckIp:
    """resolve_and_check_ip() with mocked DNS."""

    def test_ip_literal_external(self):
        """IP literal that is external -> not blocked."""
        blocked, reason, ips = resolve_and_check_ip("8.8.8.8")
        assert blocked is False
        assert ips == ["8.8.8.8"]

    def test_ip_literal_internal(self):
        """IP literal that is internal -> blocked."""
        blocked, reason, ips = resolve_and_check_ip("127.0.0.1")
        assert blocked is True
        assert ips == []

    def test_hostname_resolves_to_external(self):
        """Hostname resolves to external IP -> not blocked."""
        fake_results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),
        ]
        with patch("core_runtime.egress_ip.socket.getaddrinfo", return_value=fake_results):
            blocked, reason, ips = resolve_and_check_ip("example.com")
        assert blocked is False
        assert "1.2.3.4" in ips

    def test_hostname_resolves_to_internal_dns_rebinding(self):
        """Hostname resolves to internal IP -> blocked (DNS rebinding)."""
        fake_results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0)),
        ]
        with patch("core_runtime.egress_ip.socket.getaddrinfo", return_value=fake_results):
            blocked, reason, ips = resolve_and_check_ip("evil.example.com")
        assert blocked is True
        assert "rebinding" in reason.lower() or "blocked" in reason.lower()

    def test_hostname_dns_failure(self):
        """DNS resolution failure -> blocked."""
        with patch("core_runtime.egress_ip.socket.getaddrinfo",
                    side_effect=socket.gaierror("Name resolution failed")):
            blocked, reason, ips = resolve_and_check_ip("nonexistent.example.invalid")
        assert blocked is True
        assert "DNS" in reason or "resolution" in reason.lower()
        assert ips == []

    def test_hostname_mixed_ips_one_internal(self):
        """Hostname resolves to mixed IPs (one internal) -> blocked."""
        fake_results = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0)),
        ]
        with patch("core_runtime.egress_ip.socket.getaddrinfo", return_value=fake_results):
            blocked, reason, ips = resolve_and_check_ip("mixed.example.com")
        assert blocked is True


# ======================================================================
# validate_request
# ======================================================================

class TestValidateRequest:
    """validate_request() field tests."""

    def test_valid_get_request(self):
        req = {"method": "GET", "url": "https://example.com/api"}
        valid, reason = validate_request(req)
        assert valid is True
        assert reason == ""

    def test_valid_post_request(self):
        req = {"method": "POST", "url": "http://example.com/data",
               "headers": {"Content-Type": "application/json"},
               "timeout_seconds": 10}
        valid, reason = validate_request(req)
        assert valid is True

    def test_missing_method(self):
        req = {"url": "https://example.com"}
        valid, reason = validate_request(req)
        assert valid is False
        assert "Method" in reason

    def test_disallowed_method(self):
        req = {"method": "CONNECT", "url": "https://example.com"}
        valid, reason = validate_request(req)
        assert valid is False
        assert "not allowed" in reason

    def test_missing_url(self):
        req = {"method": "GET"}
        valid, reason = validate_request(req)
        assert valid is False
        assert "URL" in reason

    def test_unsupported_scheme(self):
        req = {"method": "GET", "url": "ftp://example.com/file"}
        valid, reason = validate_request(req)
        assert valid is False
        assert "scheme" in reason.lower()

    def test_url_no_hostname(self):
        req = {"method": "GET", "url": "http://"}
        valid, reason = validate_request(req)
        assert valid is False

    def test_headers_not_dict(self):
        req = {"method": "GET", "url": "https://example.com", "headers": "bad"}
        valid, reason = validate_request(req)
        assert valid is False
        assert "dict" in reason.lower()

    def test_too_many_headers(self):
        headers = {f"X-Header-{i}": "val" for i in range(MAX_HEADER_COUNT + 1)}
        req = {"method": "GET", "url": "https://example.com", "headers": headers}
        valid, reason = validate_request(req)
        assert valid is False
        assert "Too many" in reason

    def test_header_name_too_long(self):
        headers = {"X" * (MAX_HEADER_NAME_LENGTH + 1): "val"}
        req = {"method": "GET", "url": "https://example.com", "headers": headers}
        valid, reason = validate_request(req)
        assert valid is False
        assert "name too long" in reason.lower()

    def test_header_value_too_long(self):
        headers = {"X-Key": "V" * (MAX_HEADER_VALUE_LENGTH + 1)}
        req = {"method": "GET", "url": "https://example.com", "headers": headers}
        valid, reason = validate_request(req)
        assert valid is False
        assert "value too long" in reason.lower()

    def test_timeout_zero(self):
        req = {"method": "GET", "url": "https://example.com", "timeout_seconds": 0}
        valid, reason = validate_request(req)
        assert valid is False
        assert "positive" in reason.lower()

    def test_timeout_negative(self):
        req = {"method": "GET", "url": "https://example.com", "timeout_seconds": -5}
        valid, reason = validate_request(req)
        assert valid is False

    def test_timeout_exceeds_max(self):
        req = {"method": "GET", "url": "https://example.com",
               "timeout_seconds": MAX_TIMEOUT + 1}
        valid, reason = validate_request(req)
        assert valid is False
        assert "too large" in reason.lower()

    def test_timeout_invalid_type(self):
        req = {"method": "GET", "url": "https://example.com",
               "timeout_seconds": "not_a_number"}
        valid, reason = validate_request(req)
        assert valid is False
        assert "Invalid timeout" in reason

    def test_all_allowed_methods(self):
        for method in ALLOWED_METHODS:
            req = {"method": method, "url": "https://example.com"}
            valid, _ = validate_request(req)
            assert valid is True, f"Method {method} should be allowed"

    def test_method_case_insensitive(self):
        req = {"method": "get", "url": "https://example.com"}
        valid, _ = validate_request(req)
        assert valid is True


# ======================================================================
# read_length_prefixed_json / write_length_prefixed_json
# ======================================================================

class _FakeSocket:
    """In-memory fake socket for length-prefix JSON tests."""

    def __init__(self, data: bytes = b""):
        self._buf = io.BytesIO(data)
        self._out = io.BytesIO()

    def recv(self, n: int) -> bytes:
        return self._buf.read(n)

    def sendall(self, data: bytes) -> None:
        self._out.write(data)

    def get_sent(self) -> bytes:
        return self._out.getvalue()


class TestLengthPrefixedJson:
    """Round-trip and edge cases for length-prefix JSON protocol."""

    def test_round_trip(self):
        payload = {"key": "value", "number": 42}
        sock_write = _FakeSocket()
        write_length_prefixed_json(sock_write, payload)

        raw = sock_write.get_sent()
        sock_read = _FakeSocket(raw)
        result = read_length_prefixed_json(sock_read, MAX_REQUEST_SIZE)
        assert result == payload

    def test_empty_dict(self):
        sock_write = _FakeSocket()
        write_length_prefixed_json(sock_write, {})
        raw = sock_write.get_sent()

        sock_read = _FakeSocket(raw)
        result = read_length_prefixed_json(sock_read, MAX_REQUEST_SIZE)
        assert result == {}

    def test_message_too_large(self):
        big_payload = json.dumps({"data": "x" * 100}).encode("utf-8")
        length_prefix = struct.pack(">I", len(big_payload))
        raw = length_prefix + big_payload
        sock = _FakeSocket(raw)
        with pytest.raises(ValueError, match="too large"):
            read_length_prefixed_json(sock, 10)  # max_size=10

    def test_connection_closed_during_length(self):
        sock = _FakeSocket(b"\x00\x00")  # only 2 bytes, need 4
        result = read_length_prefixed_json(sock, MAX_REQUEST_SIZE)
        assert result is None

    def test_connection_closed_during_payload(self):
        payload = b'{"key":"value"}'
        length_prefix = struct.pack(">I", len(payload))
        raw = length_prefix + payload[:5]  # truncated
        sock = _FakeSocket(raw)
        with pytest.raises(ValueError, match="closed"):
            read_length_prefixed_json(sock, MAX_REQUEST_SIZE)

    def test_zero_length_payload(self):
        raw = struct.pack(">I", 0)
        sock = _FakeSocket(raw)
        result = read_length_prefixed_json(sock, MAX_REQUEST_SIZE)
        assert result == {}

    def test_unicode_payload(self):
        payload = {"message": "こんにちは世界", "emoji": "🎉"}
        sock_write = _FakeSocket()
        write_length_prefixed_json(sock_write, payload)
        raw = sock_write.get_sent()

        sock_read = _FakeSocket(raw)
        result = read_length_prefixed_json(sock_read, MAX_REQUEST_SIZE)
        assert result["message"] == "こんにちは世界"
        assert result["emoji"] == "🎉"


# ======================================================================
# read_response_with_limit
# ======================================================================

class _FakeHTTPResponse:
    """Minimal fake HTTP response for read_response_with_limit."""

    def __init__(self, body: bytes, content_length: Optional[str] = None):
        self._body = io.BytesIO(body)
        self._content_length = content_length

    def getheader(self, name: str) -> Optional[str]:
        if name.lower() == "content-length" or name == "Content-Length":
            return self._content_length
        return None

    def read(self, n: int) -> bytes:
        return self._body.read(n)


class TestReadResponseWithLimit:
    """read_response_with_limit boundary tests."""

    def test_normal_read(self):
        body = b"Hello, World!"
        resp = _FakeHTTPResponse(body, content_length=str(len(body)))
        data, exceeded, bytes_read = read_response_with_limit(resp, MAX_RESPONSE_SIZE)
        assert data == body
        assert exceeded is False
        assert bytes_read == len(body)

    def test_content_length_exceeds_limit(self):
        resp = _FakeHTTPResponse(b"x" * 100, content_length="99999999")
        data, exceeded, bytes_read = read_response_with_limit(resp, 1024)
        assert exceeded is True
        assert data == b""
        assert bytes_read == 0

    def test_chunk_read_exceeds_limit(self):
        body = b"x" * 2048
        resp = _FakeHTTPResponse(body, content_length=None)
        data, exceeded, bytes_read = read_response_with_limit(resp, 1024)
        assert exceeded is True
        assert bytes_read > 1024

    def test_exact_limit(self):
        body = b"x" * 1024
        resp = _FakeHTTPResponse(body, content_length=str(1024))
        data, exceeded, bytes_read = read_response_with_limit(resp, 1024)
        assert exceeded is False
        assert len(data) == 1024

    def test_empty_body(self):
        resp = _FakeHTTPResponse(b"", content_length="0")
        data, exceeded, bytes_read = read_response_with_limit(resp, MAX_RESPONSE_SIZE)
        assert data == b""
        assert exceeded is False
        assert bytes_read == 0

    def test_invalid_content_length_still_reads(self):
        body = b"some data"
        resp = _FakeHTTPResponse(body, content_length="not-a-number")
        data, exceeded, bytes_read = read_response_with_limit(resp, MAX_RESPONSE_SIZE)
        assert data == body
        assert exceeded is False


# ======================================================================
# _pack_socket_name
# ======================================================================

class TestPackSocketName:
    """_pack_socket_name deterministic hashing tests."""

    def test_deterministic(self):
        name1 = _pack_socket_name("pack_alpha")
        name2 = _pack_socket_name("pack_alpha")
        assert name1 == name2

    def test_different_packs_different_names(self):
        name1 = _pack_socket_name("pack_alpha")
        name2 = _pack_socket_name("pack_beta")
        assert name1 != name2

    def test_ends_with_sock(self):
        name = _pack_socket_name("my_pack")
        assert name.endswith(".sock")

    def test_hash_length(self):
        name = _pack_socket_name("test_pack")
        # Format: {sha256[:32]}.sock
        assert len(name) == 32 + 5  # 32 hex chars + ".sock"

    def test_empty_pack_id(self):
        name = _pack_socket_name("")
        assert name.endswith(".sock")
        assert len(name) == 37


# ======================================================================
# UDSSocketManager (with tmp_path)
# ======================================================================

class TestUDSSocketManager:
    """UDSSocketManager lifecycle with tmp_path."""

    def test_get_socket_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCK_DIR", str(tmp_path))
        mgr = UDSSocketManager()
        path = mgr.get_socket_path("test_pack")
        assert path.parent == tmp_path
        assert path.name == _pack_socket_name("test_pack")

    def test_ensure_socket_creates_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCK_DIR", str(tmp_path))
        mgr = UDSSocketManager()
        success, error, sock_path = mgr.ensure_socket("pack_1")
        assert success is True
        assert error == ""
        assert sock_path is not None
        assert sock_path.parent.exists()

    def test_ensure_socket_removes_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCK_DIR", str(tmp_path))
        mgr = UDSSocketManager()
        # Pre-create a file at the socket path
        expected_path = tmp_path / _pack_socket_name("pack_pre")
        expected_path.touch()
        assert expected_path.exists()

        success, error, sock_path = mgr.ensure_socket("pack_pre")
        assert success is True
        # The old file should have been removed (ensure_socket unlinks existing)
        # sock_path is now registered but file is not recreated (server creates it)

    def test_cleanup_socket(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCK_DIR", str(tmp_path))
        mgr = UDSSocketManager()
        success, _, sock_path = mgr.ensure_socket("pack_cleanup")
        assert success is True

        # Create a dummy file to simulate socket
        sock_path.touch()
        assert sock_path.exists()

        mgr.cleanup_socket("pack_cleanup")
        assert not sock_path.exists()

    def test_cleanup_nonexistent_pack(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCK_DIR", str(tmp_path))
        mgr = UDSSocketManager()
        # Should not raise
        mgr.cleanup_socket("nonexistent_pack")

    def test_get_base_dir_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCK_DIR", str(tmp_path))
        mgr = UDSSocketManager()
        base = mgr.get_base_dir_path()
        assert base == tmp_path

    def test_socket_path_uses_hash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCK_DIR", str(tmp_path))
        mgr = UDSSocketManager()
        path1 = mgr.get_socket_path("pack_a")
        path2 = mgr.get_socket_path("pack_b")
        assert path1 != path2
        assert path1.name != path2.name


# ======================================================================
# Permission utilities
# ======================================================================

class TestPermissionUtilities:
    """Tests for _get_egress_socket_mode and _get_egress_socket_gid."""

    def test_default_mode(self, monkeypatch):
        monkeypatch.delenv("RUMI_EGRESS_SOCKET_MODE", raising=False)
        assert _get_egress_socket_mode() == _EGRESS_DEFAULT_SOCKET_MODE

    def test_relaxed_mode(self, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCKET_MODE", "0666")
        assert _get_egress_socket_mode() == _EGRESS_RELAXED_SOCKET_MODE

    def test_invalid_mode_returns_default(self, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCKET_MODE", "0777")
        assert _get_egress_socket_mode() == _EGRESS_DEFAULT_SOCKET_MODE

    def test_gid_not_set(self, monkeypatch):
        monkeypatch.delenv("RUMI_EGRESS_SOCKET_GID", raising=False)
        assert _get_egress_socket_gid() is None

    def test_gid_valid(self, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCKET_GID", "1000")
        assert _get_egress_socket_gid() == 1000

    def test_gid_invalid(self, monkeypatch):
        monkeypatch.setenv("RUMI_EGRESS_SOCKET_GID", "notanumber")
        assert _get_egress_socket_gid() is None


# ======================================================================
# _is_ip_literal
# ======================================================================

class TestIsIpLiteral:
    """_is_ip_literal helper tests."""

    def test_ipv4_literal(self):
        assert _is_ip_literal("1.2.3.4") is True

    def test_ipv6_literal(self):
        assert _is_ip_literal("::1") is True

    def test_hostname_not_literal(self):
        assert _is_ip_literal("example.com") is False

    def test_empty_string(self):
        assert _is_ip_literal("") is False


# ======================================================================
# Network grant enforcement
# ======================================================================

class TestNetworkGrantEnforcement:
    """Regression tests for fail-closed egress grant handling."""

    def test_missing_network_grant_manager_denies_without_connecting(self):
        """No NetworkGrantManager must fail closed before outbound connect."""
        with patch(
            "core_runtime.egress_proxy.resolve_and_check_ip",
            return_value=(False, "", ["203.0.113.10"]),
        ), patch("core_runtime.egress_proxy.socket.create_connection") as mock_connect:
            result = execute_http_request(
                pack_id="pack_a",
                request={"method": "GET", "url": "http://attacker.example/"},
                network_grant_manager=None,
                audit_logger=None,
            )

        assert result["success"] is False
        assert result["error_type"] == "grant_denied"
        assert "network grant manager unavailable" in result["error"]
        mock_connect.assert_not_called()


# ======================================================================
# BUG-5-1: TCP Fallback Tests
# ======================================================================

class TestTCPFallback:
    """Windows TCP フォールバックのテスト (BUG-5-1)"""

    def test_uds_socket_manager_tcp_port_management(self, tmp_path, monkeypatch):
        """UDSSocketManager の TCP ポート管理"""
        monkeypatch.setenv("RUMI_EGRESS_SOCK_DIR", str(tmp_path))
        mgr = UDSSocketManager()
        assert mgr.get_port("pack_a") is None
        mgr.set_port("pack_a", 12345)
        assert mgr.get_port("pack_a") == 12345
        mgr.clear_port("pack_a")
        assert mgr.get_port("pack_a") is None

    def test_uds_egress_server_tcp_start_on_windows(self, tmp_path, monkeypatch):
        """Windows 時に TCP サーバーが起動する"""
        monkeypatch.setattr("core_runtime.egress_proxy._IS_WINDOWS", True)
        from core_runtime.egress_proxy import UDSEgressServer
        from core_runtime.ipc_auth import IpcAuthManager

        auth_mgr = IpcAuthManager()
        auth_mgr.generate_token("test_pack_tcp")

        server = UDSEgressServer(
            pack_id="test_pack_tcp",
            socket_path=tmp_path / "dummy.sock",
            network_grant_manager=None,
            audit_logger=None,
        )
        server._ipc_auth = auth_mgr
        started = server.start()

        try:
            if started:
                assert server._is_tcp is True
                assert server._tcp_port is not None
                assert server._tcp_port > 0
        finally:
            if started:
                server.stop()

    def test_tcp_auth_reject_invalid_token(self, tmp_path, monkeypatch):
        """TCP サーバーが不正トークンを拒否する"""
        monkeypatch.setattr("core_runtime.egress_proxy._IS_WINDOWS", True)
        import socket as _socket
        import struct as _struct
        import json as _json
        from core_runtime.egress_proxy import UDSEgressServer
        from core_runtime.ipc_auth import IpcAuthManager

        auth_mgr = IpcAuthManager()
        auth_mgr.generate_token("test_pack_reject")

        server = UDSEgressServer(
            pack_id="test_pack_reject",
            socket_path=tmp_path / "dummy.sock",
            network_grant_manager=None,
            audit_logger=None,
        )
        server._ipc_auth = auth_mgr
        started = server.start()

        try:
            if started:
                client = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                client.settimeout(5)
                client.connect(("127.0.0.1", server._tcp_port))
                try:
                    auth_msg = _json.dumps({"auth_token": "wrong_token"}).encode("utf-8")
                    client.sendall(_struct.pack(">I", len(auth_msg)) + auth_msg)

                    length_data = b""
                    while len(length_data) < 4:
                        chunk = client.recv(4 - len(length_data))
                        if not chunk:
                            break
                        length_data += chunk
                    if len(length_data) == 4:
                        length = _struct.unpack(">I", length_data)[0]
                        resp_data = b""
                        while len(resp_data) < length:
                            chunk = client.recv(length - len(resp_data))
                            if not chunk:
                                break
                            resp_data += chunk
                        resp = _json.loads(resp_data.decode("utf-8"))
                        assert resp["auth_ok"] is False
                finally:
                    client.close()
        finally:
            if started:
                server.stop()


class TestHttpPackAuthentication:
    """Exercise the HTTP handler without sockets or external requests."""

    @staticmethod
    def handler(payload, *, token=None, claimed=None, manager=True, auth=True):
        from core_runtime.egress_proxy import EgressHTTPServer, EgressProxyHandler
        from core_runtime.ipc_auth import IpcAuthManager

        authority = IpcAuthManager()
        valid = authority.generate_token("pack-a")
        server = object.__new__(EgressHTTPServer)
        server.ipc_auth = authority if auth else None
        server.network_grant_manager = MagicMock() if manager else None
        server.audit_logger = None
        server.allowed_internal_ips = ["127.0.0.1"]
        handler = object.__new__(EgressProxyHandler)
        handler.server = server
        handler.client_address = ("127.0.0.1", 1234)
        handler.path = "/proxy/request"
        body = json.dumps(payload).encode()
        handler.rfile = io.BytesIO(body)
        handler.headers = {"Content-Length": str(len(body))}
        if token is not None:
            handler.headers["Authorization"] = "Bearer " + (valid if token == "valid" else token)
        if claimed is not None:
            handler.headers["X-Owner-Pack"] = claimed
        handler._send_json_response = MagicMock()
        handler._process_request = MagicMock()
        return handler, valid

    @pytest.mark.parametrize("token", [None, "forged", "non-ascii-あ"])
    def test_self_reported_owner_never_authenticates(self, token):
        handler, _ = self.handler({"owner_pack": "pack-a"}, token=token, claimed="pack-a")
        handler.do_POST()
        assert handler._send_json_response.call_args.args[0] == 403
        handler._process_request.assert_not_called()

    def test_missing_authority_and_missing_grant_manager_fail_closed(self):
        for options in ({"auth": False}, {"manager": False}):
            handler, _ = self.handler({"owner_pack": "pack-a"}, token="valid", **options)
            handler.do_POST()
            assert handler._send_json_response.call_args.args[0] == 403
            handler._process_request.assert_not_called()

    def test_token_cannot_claim_another_pack(self):
        handler, _ = self.handler({"owner_pack": "pack-b"}, token="valid")
        handler.do_POST()
        assert handler._send_json_response.call_args.args[0] == 403
        handler._process_request.assert_not_called()

    def test_authenticated_pack_is_used_for_grant_and_normal_response(self):
        from core_runtime.egress_proxy import EgressProxyHandler, ProxyResponse

        handler, _ = self.handler({"owner_pack": "pack-a", "url": "https://example.com"}, token="valid")
        del handler._process_request
        handler.server.network_grant_manager.check_access.return_value.allowed = True
        handler._execute_http_request = MagicMock(return_value=ProxyResponse(success=True, body="normal"))
        handler.do_POST()
        handler.server.network_grant_manager.check_access.assert_called_once_with("pack-a", "example.com", 443)
        assert handler._send_json_response.call_args.args[1]["body"] == "normal"
        handler.server.network_grant_manager = None
        request = handler._execute_http_request.call_args.args[0]
        assert EgressProxyHandler._process_request(handler, request).allowed is False
        assert handler._execute_http_request.call_count == 1

    def test_denied_grant_never_executes(self):
        from core_runtime.egress_proxy import ProxyRequest, EgressProxyHandler

        handler, _ = self.handler({}, token="valid")
        handler.server.network_grant_manager.check_access.return_value.allowed = False
        handler.server.network_grant_manager.check_access.return_value.reason = "not granted"
        handler._execute_http_request = MagicMock()
        result = EgressProxyHandler._process_request(handler, ProxyRequest("pack-a", "GET", "https://example.com", {}, None))
        assert result.allowed is False
        handler._execute_http_request.assert_not_called()

    def test_responses_and_preflight_have_no_cors_grants(self):
        from core_runtime.egress_proxy import EgressProxyHandler

        handler, _ = self.handler({})
        del handler._send_json_response
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = io.BytesIO()
        handler.do_OPTIONS()
        handler.send_response.assert_called_with(405)
        EgressProxyHandler._send_json_response(handler, 403, {"success": False})
        assert all(not args[0].startswith("Access-Control-") for args, _ in handler.send_header.call_args_list)

    def test_proxy_client_sends_credential_only_to_proxy(self):
        from core_runtime.egress_proxy import make_proxy_request

        conn = MagicMock()
        conn.getresponse.return_value.read.return_value = b'{"success":true,"allowed":true}'
        with patch("core_runtime.egress_proxy.http.client.HTTPConnection", return_value=conn):
            result = make_proxy_request(
                "http://127.0.0.1:8766/proxy/request", "pack-a", "GET",
                "https://example.com", auth_token="fixed-test-credential",
            )
        assert result.success is True
        args = conn.request.call_args.kwargs
        assert args["headers"]["Authorization"] == "Bearer fixed-test-credential"
        assert "fixed-test-credential" not in args["body"].decode()
        assert json.loads(args["body"])["headers"] == {}

    def test_client_transport_failure_does_not_claim_a_grant(self):
        from core_runtime.egress_proxy import make_proxy_request

        with patch(
            "core_runtime.egress_proxy.http.client.HTTPConnection",
            side_effect=OSError("proxy unavailable"),
        ):
            result = make_proxy_request(
                "http://127.0.0.1:8766/proxy/request", "pack-a", "GET",
                "https://example.invalid", auth_token="test-credential",
            )
        assert result.success is False
        assert result.allowed is False


@pytest.mark.parametrize("grant", ["allowed", "denied", "missing"])
def test_host_executor_http_authentication_crosses_fork(tmp_path, monkeypatch, grant):
    """Exercise real worker, HTTP authentication and grant checks together."""
    from core_runtime import egress_proxy as egress
    from core_runtime.python_file_executor import ExecutionContext, PythonFileExecutor

    manager = None if grant == "missing" else MagicMock()
    if manager is not None:
        manager.check_access.return_value.allowed = grant == "allowed"
        manager.check_access.return_value.reason = "test grant denied"
    proxy = egress.EgressProxyServer(port=0, network_grant_manager=manager)
    assert proxy.start()
    parent_pid = os.getpid()

    def parent_only(method):
        def checked(*args, **kwargs):
            assert os.getpid() == parent_pid, "worker accessed parent proxy state"
            return method(*args, **kwargs)

        return checked

    def get_proxy():
        assert os.getpid() == parent_pid, "worker looked up the parent proxy"
        return proxy

    monkeypatch.setattr(egress, "get_egress_proxy", get_proxy)
    for name in ("is_running", "get_endpoint", "get_pack_auth_token"):
        monkeypatch.setattr(proxy, name, parent_only(getattr(proxy, name)))
    monkeypatch.setattr(
        proxy._ipc_auth,
        "generate_token",
        parent_only(proxy._ipc_auth.generate_token),
    )
    upstream = MagicMock(
        return_value=egress.ProxyResponse(success=True, body="upstream response"),
    )
    monkeypatch.setattr(egress.EgressProxyHandler, "_execute_http_request", upstream)
    pack_file = tmp_path / "pack_http.py"
    pack_file.write_text(
        "import os\n"
        "def run(input_data, context):\n"
        "    context['owner_pack'] = input_data['owner_pack']\n"
        "    response = context['http_request'](\n"
        "        'GET', input_data['url'], headers={'X-Target': 'present'})\n"
        "    return {'worker_pid': os.getpid(), 'response': response}\n",
        encoding="utf-8",
    )
    context = ExecutionContext(
        flow_id="test-http",
        step_id="request",
        phase="test",
        ts="",
        owner_pack="pack-a",
        inputs={},
    )
    executor = object.__new__(PythonFileExecutor)
    try:
        # The executor must issue this credential itself, in the real parent.
        assert proxy._ipc_auth.get_token("pack-a") is None
        result = executor._execute_on_host(
            pack_file,
            "pack-a",
            {"owner_pack": "pack-b", "url": "https://example.invalid/target"},
            context,
            timeout_seconds=10,
        )
        assert result.success, result.error
        assert result.output["worker_pid"] != parent_pid
        credential = proxy._ipc_auth.get_token("pack-a")
        assert credential and proxy._ipc_auth.verify_token(credential) == "pack-a"
        assert proxy._ipc_auth.get_token("pack-b") is None
        response = result.output["response"]
        assert response["allowed"] is (grant == "allowed")
        assert response["success"] is (grant == "allowed")
        if manager is not None:
            manager.check_access.assert_called_once_with("pack-a", "example.invalid", 443)
        if grant == "allowed":
            request, domain, port = upstream.call_args.args
            assert (request.owner_pack, domain, port) == ("pack-a", "example.invalid", 443)
            assert request.headers == {"X-Target": "present"}
            assert response["body"] == "upstream response"
        else:
            upstream.assert_not_called()
    finally:
        assert proxy.stop()


def test_uds_handler_keeps_socket_bound_pack_identity(tmp_path, monkeypatch):
    """A normal UDS request cannot replace its socket's Pack with payload data."""
    from core_runtime import egress_proxy as egress

    execute = MagicMock(return_value={"success": True, "body": "socket-bound"})
    monkeypatch.setattr(egress, "execute_http_request", execute)
    server = egress.UDSEgressServer(
        pack_id="pack-a",
        socket_path=tmp_path / "pack-a.sock",
        network_grant_manager=MagicMock(),
        audit_logger=None,
    )
    client, accepted = socket.socketpair()
    with client, accepted:
        client.settimeout(5)
        write_length_prefixed_json(client, {
            "owner_pack": "pack-b", "method": "GET", "url": "https://example.invalid",
        })
        server._handle_client(accepted)
        response = read_length_prefixed_json(client, MAX_RESPONSE_SIZE)
    assert response == {"success": True, "body": "socket-bound"}
    execute.assert_called_once()
    assert execute.call_args.kwargs["pack_id"] == "pack-a"
