"""
egress_ip.py - IP検証ユーティリティ

内部IP/禁止レンジ判定、DNS解決＆内部IPチェック。
egress_proxy.py から分離 (W13-T047)。
"""
from __future__ import annotations

import ipaddress
import socket
from typing import List, Tuple


# ============================================================
# 禁止IPレンジ
# ============================================================

BLOCKED_IPV4_NETWORKS = [
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("224.0.0.0/4"),
    ipaddress.IPv4Network("240.0.0.0/4"),
]

BLOCKED_IPV6_NETWORKS = [
    ipaddress.IPv6Network("::/128"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("ff00::/8"),
]

BLOCKED_IPV4_ADDRESSES = {
    ipaddress.IPv4Address("255.255.255.255"),
}


# ============================================================
# IP検証ユーティリティ
# ============================================================

def is_internal_ip(ip_str: str) -> Tuple[bool, str]:
    """
    IPが内部/禁止レンジか判定

    Returns:
        (is_blocked, reason)
    """
    try:
        ip = ipaddress.ip_address(ip_str)

        if isinstance(ip, ipaddress.IPv4Address):
            if ip in BLOCKED_IPV4_ADDRESSES:
                return True, f"IP {ip} is a broadcast address"
            for ipv4_network in BLOCKED_IPV4_NETWORKS:
                if ip in ipv4_network:
                    return True, f"IP {ip} is in blocked range {ipv4_network}"
        else:
            for ipv6_network in BLOCKED_IPV6_NETWORKS:
                if ip in ipv6_network:
                    return True, f"IP {ip} is in blocked range {ipv6_network}"

        return False, ""
    except ValueError as e:
        return True, f"Invalid IP address: {e}"


def _is_ip_literal(host: str) -> bool:
    """ホストがIPリテラルかどうか判定"""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve_and_check_ip(hostname: str) -> Tuple[bool, str, List[str]]:
    """
    ホスト名をDNS解決し、内部IPが含まれていないかチェック

    Returns:
        (is_blocked, reason, resolved_ips)
    """
    if _is_ip_literal(hostname):
        is_blocked, reason = is_internal_ip(hostname)
        return is_blocked, reason, [hostname] if not is_blocked else []

    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        resolved_ips = sorted({str(result[4][0]) for result in results})

        if not resolved_ips:
            return True, f"DNS resolution failed: no addresses for {hostname}", []

        for ip in resolved_ips:
            is_internal, reason = is_internal_ip(ip)
            if is_internal:
                return True, f"DNS rebinding blocked: {reason}", resolved_ips

        return False, "", resolved_ips
    except socket.gaierror as e:
        return True, f"DNS resolution failed: {e}", []
    except Exception as e:
        return True, f"DNS check error: {e}", []
