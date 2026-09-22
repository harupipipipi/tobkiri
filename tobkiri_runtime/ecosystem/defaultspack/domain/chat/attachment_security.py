from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any


_HIGH_RISK_NAME = re.compile(
    r"(^|[._-])(\.env|credentials?|secrets?|auth|cookies?|private[_-]?key|"
    r"id_(?:rsa|dsa|ecdsa|ed25519))([._-]|$)|\.(?:pem|key|p12|pfx|keystore|log|dump)$",
    re.IGNORECASE,
)
_CONTENT_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(r"\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+([^\s]+)", re.IGNORECASE),
    re.compile(r"\b(?:cookie|set-cookie)\s*:\s*([^\r\n]+)", re.IGNORECASE),
    re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]+:([^\s/@]+)@[^\s]+", re.IGNORECASE),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|"
        r"xox[baprs]-[A-Za-z0-9-]{20,}|npm_[A-Za-z0-9]{20,}|"
        r"[sr]k_live_[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{20,})\b"
    ),
    re.compile(
        r"\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|client[_-]?secret|private[_-]?key|password|"
        r"passwd|token|secret)\s*[:=]\s*[\"']?([^\s\"',;]{6,})",
        re.IGNORECASE,
    ),
)
_REVIEWED_STATUSES = {"approved", "redacted", "metadata_only"}
_BINARY_EXTENSIONS = {"zip", "exe", "dll", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "wasm", "bin"}
_TEXT_EXTENSIONS = {
    "bash", "bat", "c", "cfg", "conf", "cpp", "cs", "css", "csv", "env",
    "go", "graphql", "h", "hpp", "html", "ini", "java", "js", "json", "jsx",
    "kt", "log", "lua", "md", "mdx", "mjs", "php", "properties", "ps1", "py",
    "rb", "rs", "sh", "sql", "svg", "toml", "ts", "tsx", "txt", "xml",
    "yaml", "yml", "zsh",
}
_BINARY_MIME = re.compile(
    r"^(?:application/(?:octet-stream|pdf|zip|x-7z-compressed|x-rar-compressed|"
    r"vnd\.ms-|vnd\.openxmlformats-officedocument)|audio/|image/(?!svg\+xml)|video/)",
    re.IGNORECASE,
)
_HIGH_ENTROPY_CANDIDATE = re.compile(r"\b[A-Za-z0-9_+/=-]{32,128}\b")
_BINARY_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MAX_CUSTOM_PATTERNS = 32
_MAX_CUSTOM_PATTERN_LENGTH = 128


def _content_fingerprint(content: str) -> str:
    value = 0x811C9DC5
    encoded = content.encode("utf-16-le", errors="surrogatepass")
    for index in range(0, len(encoded), 2):
        code_unit = encoded[index] | (encoded[index + 1] << 8)
        value ^= code_unit
        value = (value * 0x01000193) & 0xFFFFFFFF
    # JavaScript String.length counts UTF-16 code units.
    return f"fnv1a32:{len(encoded) // 2}:{value:08x}"


def _attachment_payload(attachment: dict[str, Any]) -> str:
    if "content" in attachment and attachment.get("content") is not None:
        return str(attachment.get("content"))
    return str(attachment.get("dataUrl") or "")


def _attachment_fingerprint(attachment: dict[str, Any]) -> str:
    fingerprint_input = "\0".join(
        (
            str(attachment.get("name") or ""),
            str(attachment.get("size") if attachment.get("size") is not None else ""),
            str(attachment.get("type") or ""),
            "1" if bool(attachment.get("truncated")) else "0",
            str(attachment.get("source") or ""),
            str(attachment.get("sourcePath") or ""),
            _attachment_payload(attachment),
        )
    )
    return _content_fingerprint(fingerprint_input)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _normalize_custom_patterns(value: Any) -> list[str]:
    candidates = value if isinstance(value, list) else re.split(r"\r?\n|,", str(value or ""))
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        pattern = str(candidate or "").strip()
        folded = pattern.casefold()
        if (
            len(pattern) < 3
            or len(pattern) > _MAX_CUSTOM_PATTERN_LENGTH
            or folded in seen
        ):
            continue
        seen.add(folded)
        normalized.append(pattern)
        if len(normalized) >= _MAX_CUSTOM_PATTERNS:
            break
    return normalized


def _configured_literal_patterns() -> list[str]:
    override = os.environ.get("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        user_data = os.environ.get("RUMI_USER_DATA", "").strip()
        path = (
            Path(user_data).expanduser() / "defaultspack" / "shared" / "frontend_settings.json"
            if user_data
            else Path(__file__).resolve().parents[2]
            / "user_data"
            / "shared"
            / "frontend_settings.json"
        )
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Attachment security policy could not be loaded") from exc
    privacy = payload.get("privacy_security") if isinstance(payload, dict) else None
    return _normalize_custom_patterns(
        privacy.get("attachment_secret_patterns") if isinstance(privacy, dict) else None
    )


def _looks_high_entropy(value: str) -> bool:
    if not (re.search(r"[a-z]", value) and re.search(r"[A-Z]", value) and re.search(r"\d", value)):
        return False
    probabilities = (value.count(character) / len(value) for character in set(value))
    entropy = -sum(probability * math.log2(probability) for probability in probabilities)
    return entropy >= 3.8


def _content_requires_review(content: str, custom_patterns: list[str]) -> bool:
    if _BINARY_CONTROL.search(content):
        return True
    if any(pattern.search(content) for pattern in _CONTENT_PATTERNS):
        return True
    if any(
        pattern.casefold() in content.casefold()
        for pattern in custom_patterns
    ):
        return True
    return any(
        _looks_high_entropy(match.group(0))
        for match in _HIGH_ENTROPY_CANDIDATE.finditer(content)
    )


def attachment_requires_review(
    attachment: dict[str, Any], custom_patterns: list[str] | None = None
) -> bool:
    name = str(attachment.get("name") or "")
    content = str(attachment.get("content") or "")
    has_unscanned_data_url = (
        attachment.get("content") is None and bool(attachment.get("dataUrl"))
    )
    basename = re.split(r"[\\/]", name)[-1]
    extension = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
    mime = str(attachment.get("type") or "").lower()
    if (
        _HIGH_RISK_NAME.search(name)
        or (basename.startswith(".") and len(basename) > 1)
        or bool(attachment.get("truncated"))
        or (mime.startswith("text/") and extension in _BINARY_EXTENSIONS)
        or (_BINARY_MIME.search(mime) is not None and extension in _TEXT_EXTENSIONS)
        or has_unscanned_data_url
    ):
        return True
    return _content_requires_review(content, custom_patterns or [])


def validate_attachment_security_reviews(
    attachments: list[Any],
    custom_patterns: list[str] | None = None,
) -> None:
    """Re-scan client attachments and require a review bound to the current content."""

    patterns = (
        _configured_literal_patterns()
        if custom_patterns is None
        else _normalize_custom_patterns(custom_patterns)
    )
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("Attachment payload must be an object")
        requires_review = attachment_requires_review(attachment, patterns)
        review = attachment.get("securityReview")
        if review is not None and not isinstance(review, dict):
            raise ValueError("Attachment security review is malformed")
        status = str(review.get("status") or "") if isinstance(review, dict) else ""
        if status == "required":
            raise ValueError("Sensitive or truncated attachment requires explicit review before dispatch")
        if status in _REVIEWED_STATUSES:
            if not isinstance(review, dict) or review.get("version") != 1:
                raise ValueError("Attachment security review version is unsupported")
            expected = _attachment_fingerprint(attachment)
            if str(review.get("fingerprint") or "") != expected:
                raise ValueError("Attachment changed after security review; review it again before dispatch")
            content = str(attachment.get("content") or "")
            if review.get("scannedCharacters") != _utf16_length(content):
                raise ValueError("Attachment security review range does not match the current content")
            if bool(review.get("truncated")) != bool(attachment.get("truncated")):
                raise ValueError("Attachment truncation changed after security review")
            if status == "metadata_only":
                if "content" in attachment or "dataUrl" in attachment:
                    raise ValueError("Metadata-only attachment must not include file content")
                continue
            if status == "redacted" and (
                bool(attachment.get("truncated"))
                or _content_requires_review(content, patterns)
            ):
                raise ValueError("Redacted attachment still requires security review")
            continue
        if requires_review:
            raise ValueError("Sensitive or truncated attachment requires explicit review before dispatch")
