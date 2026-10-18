from __future__ import annotations

import base64
import binascii
import re
from typing import Any

MAX_ATTACHMENTS = 1
MAX_TEXT_BYTES = 120_000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = 7 * 1024 * 1024

_IMAGE_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
_IMAGE_EXTENSIONS = {
    "image/gif": {"gif"},
    "image/jpeg": {"jpeg", "jpg"},
    "image/png": {"png"},
    "image/webp": {"webp"},
}
_GENERIC_BINARY_TYPES = {"", "application/octet-stream"}
_TEXT_TYPES = {
    "application/csv",
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/toml",
    "application/typescript",
    "application/xml",
    "image/svg+xml",
}
_TEXT_EXTENSIONS = {
    "c",
    "cfg",
    "conf",
    "cpp",
    "cs",
    "css",
    "csv",
    "go",
    "graphql",
    "h",
    "hpp",
    "html",
    "ini",
    "java",
    "js",
    "json",
    "jsx",
    "kt",
    "log",
    "lua",
    "md",
    "mdx",
    "mjs",
    "php",
    "properties",
    "py",
    "rb",
    "rs",
    "sh",
    "sql",
    "svg",
    "toml",
    "ts",
    "tsx",
    "txt",
    "xml",
    "yaml",
    "yml",
    "zsh",
}
_DATA_URL = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$")


def normalize_attachments(value: Any) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise ValueError("Search Home accepts one attachment at a time")

    item = value[0]
    if not isinstance(item, dict):
        raise ValueError("attachment must be an object")
    name = _safe_name(item.get("name"))
    mime = str(item.get("type") or "").strip().lower()
    size = item.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError("attachment size must be a non-negative integer")
    attachment_id = _safe_id(item.get("id"))

    if mime in _IMAGE_TYPES:
        extension = _extension(name)
        if extension and extension not in _IMAGE_EXTENSIONS[mime]:
            raise ValueError("image attachment extension does not match its declared type")
        data_url = item.get("dataUrl")
        match = _DATA_URL.fullmatch(data_url) if isinstance(data_url, str) else None
        if match is None or match.group(1).lower() != mime or "content" in item:
            raise ValueError("image attachment data does not match its declared type")
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("image attachment contains invalid base64 data") from exc
        if size > MAX_IMAGE_BYTES or len(decoded) > MAX_IMAGE_BYTES or len(decoded) != size:
            raise ValueError(
                "image attachments must be 5 MB or smaller and report their exact size"
            )
        if not _matches_image_signature(decoded, mime):
            raise ValueError("image attachment bytes do not match its declared type")
        return [
            {"id": attachment_id, "name": name, "size": size, "type": mime, "dataUrl": data_url}
        ]

    if _is_text(name, mime):
        content = item.get("content")
        if not isinstance(content, str) or "dataUrl" in item:
            raise ValueError("text attachments require content and must not include dataUrl")
        encoded_size = len(content.encode("utf-8"))
        if size > MAX_TEXT_BYTES or encoded_size > MAX_TEXT_BYTES:
            raise ValueError("text and code attachments must be 120 KB or smaller")
        if encoded_size != size:
            raise ValueError("text attachments must report their exact UTF-8 size")
        normalized_mime = "text/plain" if mime in _GENERIC_BINARY_TYPES else mime
        return [
            {
                "id": attachment_id,
                "name": name,
                "size": size,
                "type": normalized_mime,
                "content": content,
            }
        ]

    raise ValueError("unsupported attachment type; use text/code, PNG, JPEG, GIF, or WebP")


def attachment_metadata(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: item[key] for key in ("id", "name", "size", "type") if key in item}
        for item in attachments
    ]


def _safe_name(value: Any) -> str:
    name = re.split(r"[\\/]", re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")))[-1]
    name = name.replace("`", "'").strip()[:240]
    if not name:
        raise ValueError("attachment name is required")
    return name


def _is_text(name: str, mime: str) -> bool:
    extension = _extension(name)
    if extension in {item for values in _IMAGE_EXTENSIONS.values() for item in values}:
        return False
    if mime.startswith("text/") or mime in _TEXT_TYPES:
        return True
    return mime in _GENERIC_BINARY_TYPES and extension in _TEXT_EXTENSIONS


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _safe_id(value: Any) -> str:
    attachment_id = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(value or "")).strip("-")[:128]
    return attachment_id or "search-home-attachment"


def _matches_image_signature(data: bytes, mime: str) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return len(data) >= 4 and data.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False
