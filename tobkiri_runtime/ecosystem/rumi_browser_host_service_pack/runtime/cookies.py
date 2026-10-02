"""Normalize user-exported browser cookies without touching source profiles."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

MAX_COOKIES = 5_000
MAX_IMPORT_BYTES = 4 * 1024 * 1024
_DOMAIN = re.compile(r"^\.?[a-zA-Z0-9_\-\u0080-\uffff]+(?:\.[a-zA-Z0-9_\-\u0080-\uffff]+)*$")


def normalize_cookie(value: Mapping[str, Any]) -> dict[str, Any]:
    """Accept Chromium/Firefox JSON-export field names with strict bounds."""

    name = str(value.get("name") or "").strip()
    domain = str(value.get("domain") or "").strip().lower()
    cookie_value = str(value.get("value") or "")
    path = str(value.get("path") or "/")
    if (
        not name
        or len(name) > 256
        or any(ord(char) < 32 or char in ";=" for char in name)
        or len(domain) > 253
        or not _DOMAIN.fullmatch(domain)
        or len(cookie_value.encode("utf-8")) > 16_384
        or len(path) > 1024
        or not path.startswith("/")
    ):
        raise ValueError("Cookie name, domain, path or size is invalid")
    same_site = str(value.get("same_site", value.get("sameSite", "Lax")))
    aliases = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "Lax",
    }
    same_site = aliases.get(same_site.lower(), same_site)
    if same_site not in {"Strict", "Lax", "None"}:
        raise ValueError("Cookie SameSite value is invalid")
    expires = value.get("expires_at", value.get("expires", value.get("expirationDate")))
    if expires is not None:
        try:
            expires = float(expires)
        except (TypeError, ValueError) as exc:
            raise ValueError("Cookie expiry is invalid") from exc
        if not math.isfinite(expires):
            raise ValueError("Cookie expiry is invalid")
        expires = expires if expires > 0 else None
    secure = value.get("secure", True)
    http_only = value.get("http_only", value.get("httpOnly", True))
    if not isinstance(secure, bool) or not isinstance(http_only, bool):
        raise ValueError("Cookie security flags must be booleans")
    return {
        "name": name,
        "value": cookie_value,
        "domain": domain,
        "path": path,
        "secure": secure,
        "http_only": http_only,
        "same_site": same_site,
        "expires_at": expires,
    }


def parse_import(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse an explicit JSON or Netscape export and optional domain filter."""

    raw = payload.get("cookies")
    content = payload.get("content")
    if raw is not None and content is not None:
        raise ValueError("Provide cookies or an exported file, not both")
    if content is not None:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("Cookie export exceeds the 4 MiB import limit")
        kind = str(payload.get("format") or "json").lower()
        if kind == "json":
            raw = json.loads(content)
            if isinstance(raw, dict):
                raw = raw.get("cookies")
        elif kind == "netscape":
            raw = _netscape(content)
        else:
            raise ValueError("Cookie format must be json or netscape")
    if not isinstance(raw, list) or len(raw) > MAX_COOKIES:
        raise ValueError("Cookies must be a bounded list")
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("Cookie import exceeds the 4 MiB limit")
    if any(not isinstance(item, Mapping) for item in raw):
        raise ValueError("Every cookie must be an object")
    cookies = [normalize_cookie(item) for item in raw]
    domains = payload.get("domains")
    if domains is not None:
        if not isinstance(domains, list) or not domains or len(domains) > 100:
            raise ValueError("Cookie domain filter must be a bounded list")
        allowed = []
        for value in domains:
            domain = str(value).strip().lower().lstrip(".")
            if len(domain) > 253 or not _DOMAIN.fullmatch(domain):
                raise ValueError("Cookie domain filter is invalid")
            allowed.append(domain)
        cookies = [
            cookie
            for cookie in cookies
            if any(
                cookie["domain"].lstrip(".") == domain
                or cookie["domain"].lstrip(".").endswith("." + domain)
                for domain in allowed
            )
        ]
    return cookies


def to_cdp(cookie: Mapping[str, Any]) -> dict[str, Any]:
    """Project the canonical cookie record to a CDP CookieParam."""

    result = {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": cookie["domain"],
        "path": cookie["path"],
        "secure": cookie["secure"],
        "httpOnly": cookie["http_only"],
        "sameSite": cookie["same_site"],
    }
    if cookie.get("expires_at") is not None:
        result["expires"] = cookie["expires_at"]
    return result


def _netscape(content: str) -> list[dict[str, Any]]:
    result = []
    for line in content.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_") :]
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t", 6)
        if len(fields) != 7 or fields[1].upper() not in {"TRUE", "FALSE"}:
            raise ValueError("Malformed Netscape cookie export")
        domain, _subdomains, path, secure, expires, name, value = fields
        if secure.upper() not in {"TRUE", "FALSE"}:
            raise ValueError("Malformed Netscape cookie security flag")
        result.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "expires_at": expires,
                "name": name,
                "value": value,
                "http_only": http_only,
            }
        )
        if len(result) > MAX_COOKIES:
            raise ValueError("Too many cookies in export")
    return result
