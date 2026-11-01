"""Read-only document, image, audio, and recording inspection."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any, Callable, Final, Mapping


FILE_INSPECT: Final[str] = "tobkiri.service.file.inspect.v1"
FILE_INSPECT_FOR_MEDIA: Final[str] = "rumi_file_inspect_pack.file-inspect.for-media"
_MAX_TEXT_BYTES: Final[int] = 4 * 1024 * 1024
_TEXT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".yaml", ".yml", ".xml", ".html"}
)
_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
)
_AUDIO_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
)
_VIDEO_SUFFIXES: Final[frozenset[str]] = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi"})


class MediaInspectService:
    """Inspect workspace media through the read-only file contract only."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Inspect one workspace document or media artifact."""

        if name == "document.parse":
            return self._parse_document(payload)
        if name == "image.inspect":
            return self._inspect_kind(payload, "image", _IMAGE_SUFFIXES)
        if name == "audio.inspect":
            return self._inspect_kind(payload, "audio", _AUDIO_SUFFIXES)
        if name == "recording.inspect":
            return self._inspect_kind(payload, "recording", _VIDEO_SUFFIXES)
        raise ValueError(f"unknown media inspect operation: {name}")

    def _parse_document(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        path = _relative_path(payload)
        suffix = path.suffix.lower()
        if suffix not in _TEXT_SUFFIXES:
            return {
                "status": "unavailable",
                "success": False,
                "error_type": "document_decoder_not_installed",
                "path": path.as_posix(),
                "suffix": suffix,
            }
        result = self.client.invoke(
            FILE_INSPECT,
            FILE_INSPECT_FOR_MEDIA,
            _file_payload(
                payload,
                {
                    "name": "read",
                    "profile_id": _profile(payload),
                    "workspace_id": _workspace(payload),
                    "path": path.as_posix(),
                    "max_bytes": _MAX_TEXT_BYTES,
                    "encoding": str(payload.get("encoding") or "utf-8"),
                },
            ),
        )
        if not isinstance(result, Mapping):
            raise TypeError("file inspect contract returned an invalid result")
        content = str(result.get("content") or "")
        parsed: Any = content
        media_type = "text/plain"
        if suffix == ".json":
            parsed = json.loads(content)
            media_type = "application/json"
        return {
            "status": "ok",
            "success": True,
            "workspace_id": _workspace(payload),
            "path": path.as_posix(),
            "media_type": media_type,
            "content": parsed,
            "size": int(result.get("size") or 0),
            "read_only": True,
        }

    def _inspect_kind(
        self,
        payload: Mapping[str, Any],
        kind: str,
        suffixes: frozenset[str],
    ) -> dict[str, Any]:
        path = _relative_path(payload)
        suffix = path.suffix.lower()
        if suffix not in suffixes:
            raise ValueError(f"path is not a supported {kind} artifact")
        result = self.client.invoke(
            FILE_INSPECT,
            FILE_INSPECT_FOR_MEDIA,
            _file_payload(
                payload,
                {
                    "name": "stat",
                    "profile_id": _profile(payload),
                    "workspace_id": _workspace(payload),
                    "path": path.as_posix(),
                },
            ),
        )
        if not isinstance(result, Mapping) or result.get("is_file") is not True:
            raise FileNotFoundError("media artifact is unavailable")
        return {
            "status": "ok",
            "success": True,
            "workspace_id": _workspace(payload),
            "path": path.as_posix(),
            "kind": kind,
            "suffix": suffix,
            "size": int(result.get("size") or 0),
            "modified_ns": int(result.get("modified_ns") or 0),
            "decoded": False,
            "read_only": True,
        }


def create_media_inspector(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the read-only media inspection operation."""

    return MediaInspectService(client).invoke


def _relative_path(payload: Mapping[str, Any]) -> PurePosixPath:
    path = PurePosixPath(str(payload.get("path") or "").strip())
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise PermissionError("a workspace-relative path is required")
    return path


def _workspace(payload: Mapping[str, Any]) -> str:
    workspace_id = str(payload.get("workspace_id") or "").strip()
    if not workspace_id:
        raise ValueError("workspace_id is required")
    return workspace_id


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _file_payload(
    source: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Forward only Host-stamped workspace authority to File Inspect."""

    result = dict(payload)
    binding = source.get("_workspace_binding")
    if isinstance(binding, Mapping):
        result["_workspace_binding"] = dict(binding)
    if source.get("require_selected") is True:
        result["require_selected"] = True
    return result
