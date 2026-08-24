from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlparse, urlunparse


UNSAFE_SCHEMES = {
    "javascript",
    "file",
    "data",
    "chrome",
    "chrome-extension",
    "about",
    "blob",
}
LOCAL_HOSTS = {"localhost", "localhost.localdomain"}
PRIVATE_IPV4_RE = re.compile(
    r"\b(?:127(?:\.\d{1,3}){3}|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2})\b"
)
_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ENCODED_CONTROL_RE = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.IGNORECASE)
_SECRET_QUERY_KEY_RE = re.compile(
    r"(?:^|[_-])(token|secret|password|passwd|key|signature|credential|auth|code)(?:$|[_-])",
    re.IGNORECASE,
)
_LOCAL_TARGET_RE = re.compile(
    r"^(?P<host>localhost|127\.0\.0\.1|\[::1\]|::1|0\.0\.0\.0)"
    r"(?::(?P<port>\d{1,5}))?"
    r"(?P<suffix>(?:/[^\s]*)?(?:\?[^\s]*)?(?:#[^\s]*)?)$",
    re.IGNORECASE,
)
_DOMAIN_RE = re.compile(
    r"^(?P<host>(?:[A-Za-z0-9]"
    r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})"
    r"(?::(?P<port>\d{1,5}))?"
    r"(?P<suffix>(?:/[^\s]*)?(?:\?[^\s]*)?(?:#[^\s]*)?)$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class SafeUrlResult:
    ok: bool
    normalized_url: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "normalized_url": self.normalized_url,
            "reason": self.reason,
        }


def build_google_fallback_url(query: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(str(query or "").strip())


def unsafe_scheme_reason(raw: str) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    match = _SCHEME_RE.match(text)
    if not match:
        return None
    scheme = str(match.group("scheme") or "").lower()
    if scheme in UNSAFE_SCHEMES:
        return f"unsafe URL scheme is blocked: {scheme}:"
    return None


def query_explicitly_targets_localhost(query: str) -> bool:
    raw = str(query or "").strip().casefold()
    if not raw:
        return False
    if "localhost" in raw or "[::1]" in raw or "::1" in raw:
        return True
    if PRIVATE_IPV4_RE.search(raw):
        return True
    if "0.0.0.0" in raw:
        return True
    return False


def classify_direct_url(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text or any(char.isspace() for char in text):
        return None

    local = _LOCAL_TARGET_RE.match(text)
    if local:
        port = _validated_port(local.group("port"))
        if local.group("port") and port is None:
            return None
        host = str(local.group("host") or "")
        suffix = str(local.group("suffix") or "")
        authority = host if port is None else f"{host}:{port}"
        return {
            "url": f"http://{authority}{suffix}",
            "scheme": "http",
            "reason": "recognized local development target",
        }

    blocked_reason = unsafe_scheme_reason(text)
    if blocked_reason:
        match = _SCHEME_RE.match(text)
        return {
            "blocked": True,
            "scheme": str(match.group("scheme") or "").lower() if match else "",
            "reason": blocked_reason,
        }

    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        parsed = urlparse(text)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        return {
            "url": urlunparse(
                (
                    parsed.scheme.lower(),
                    parsed.netloc,
                    parsed.path or "",
                    "",
                    parsed.query or "",
                    parsed.fragment or "",
                )
            ),
            "scheme": parsed.scheme.lower(),
            "reason": "recognized absolute URL",
        }

    domain = _DOMAIN_RE.match(text)
    if domain:
        port = _validated_port(domain.group("port"))
        if domain.group("port") and port is None:
            return None
        host = str(domain.group("host") or "")
        suffix = str(domain.group("suffix") or "")
        authority = host if port is None else f"{host}:{port}"
        return {
            "url": f"https://{authority}{suffix}",
            "scheme": "https",
            "reason": "recognized domain target",
        }
    return None


def validate_candidate_url(url: str, *, user_query: str = "", allow_localhost: bool | None = None) -> SafeUrlResult:
    raw = str(url or "")
    if not raw:
        return SafeUrlResult(False, reason="empty_url")
    if raw != raw.strip() or _CONTROL_RE.search(raw) or _ENCODED_CONTROL_RE.search(raw):
        return SafeUrlResult(False, reason="control_characters")
    if "\\" in raw:
        return SafeUrlResult(False, reason="ambiguous_url_syntax")
    try:
        parsed = urlparse(raw)
    except ValueError:
        return SafeUrlResult(False, reason="malformed_url")
    scheme = parsed.scheme.casefold()
    if scheme in UNSAFE_SCHEMES:
        return SafeUrlResult(False, reason="unsafe_scheme")
    if scheme not in {"http", "https"}:
        return SafeUrlResult(False, reason="unsupported_scheme")
    if not parsed.hostname:
        return SafeUrlResult(False, reason="missing_hostname")
    if parsed.username or parsed.password:
        return SafeUrlResult(False, reason="embedded_credentials")
    try:
        port = parsed.port
    except ValueError:
        return SafeUrlResult(False, reason="invalid_port")
    # Routed destinations are untrusted even when the query text names a local
    # target. Only an explicit internal caller opt-in may relax this policy.
    allow_local = bool(allow_localhost) if allow_localhost is not None else False
    if not allow_local and host_is_private_or_local(parsed.hostname):
        return SafeUrlResult(False, reason="private_or_local_host")
    try:
        ascii_host = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return SafeUrlResult(False, reason="invalid_hostname")
    authority_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    authority = authority_host if port is None else f"{authority_host}:{port}"
    normalized = urlunparse(
        (
            scheme,
            authority,
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )
    return SafeUrlResult(True, normalized_url=normalized)


def url_safe_for_persistence(url: str) -> str:
    """Return a normalized URL only when it has no credential-like component."""
    validation = validate_candidate_url(url, allow_localhost=False)
    if not validation.ok:
        return ""
    parsed = urlparse(validation.normalized_url)
    if parsed.fragment:
        return ""
    if any(
        _SECRET_QUERY_KEY_RE.search(key)
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        return ""
    return validation.normalized_url


def host_is_private_or_local(host: str) -> bool:
    normalized = str(host or "").strip().strip("[]").casefold()
    if not normalized:
        return True
    if (
        normalized in LOCAL_HOSTS
        or normalized.endswith(".localhost")
        or normalized.endswith(".local")
        or normalized.endswith(".lan")
        or normalized.endswith(".home")
        or normalized.endswith(".internal")
        or normalized == "home.arpa"
        or normalized.endswith(".home.arpa")
    ):
        return True
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        # WHATWG URL parsers accept legacy one-to-four component IPv4 forms,
        # including decimal, octal, and hexadecimal components. Reject their
        # private/local interpretations at the backend boundary as well.
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}", normalized):
            return False
        try:
            packed = socket.inet_aton(normalized)
            ip = ipaddress.ip_address(packed)
        except OSError:
            return True
        return not ip.is_global or ip.is_multicast or getattr(ip, "is_site_local", False)
    return not ip.is_global or ip.is_multicast or getattr(ip, "is_site_local", False)


def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve a host and reject it unless every DNS answer is public."""
    normalized = str(host or "").strip().strip("[]")
    if host_is_private_or_local(normalized):
        raise ValueError("private_or_local_host")
    try:
        answers = socket.getaddrinfo(
            normalized,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ValueError("dns_resolution_failed") from exc
    addresses = tuple(
        dict.fromkeys(str(sockaddr[0]) for _, _, _, _, sockaddr in answers)
    )
    if not addresses:
        raise ValueError("dns_resolution_failed")
    if any(host_is_private_or_local(address) for address in addresses):
        raise ValueError("dns_resolved_private_or_local_host")
    return addresses


def _validated_port(raw_port: str | None) -> int | None:
    if raw_port in (None, ""):
        return None
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None
