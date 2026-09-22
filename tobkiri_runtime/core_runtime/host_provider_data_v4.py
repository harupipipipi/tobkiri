"""Capture selected declarative data separately from executable Pack bindings."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping

from tobkiri_host.artifact_materialization import capture_declared_pack_data
from tobkiri_host.errors import AuthorizationError

from .external_pack_catalog_v4 import resolve_admitted_pack_root
from .host_provider_backend_v4 import (
    CapturedHostPackDataV4,
    HostProviderDataRequestV4,
)


class HostProviderDataCaptureV4:
    """Own bounded, immutable factory inputs from one validated Profile lock."""

    def __init__(self, lock: Mapping[str, Any], ecosystem_root: Path) -> None:
        self._effective = {
            item["identity"]: item["artifact_digest"]
            for item in lock["effective_set"]
            if item["role"] == "pack"
        }
        self._ecosystem_root = ecosystem_root
        self._roots: dict[str, tuple[Path, tuple[int, int]]] = {}
        self._data: dict[HostProviderDataRequestV4, CapturedHostPackDataV4] = {}

    def capture_for(self, factory: Any) -> tuple[CapturedHostPackDataV4, ...]:
        """Read only a verified factory's finite, selected data declarations.

        An unselected Pack contributes nothing. Once selected, unavailable or
        changed data fails capture rather than silently producing an empty catalog.
        The caller must load and verify the factory before using this method.
        """
        requests = getattr(factory, "declared_pack_data", ())
        if not isinstance(requests, tuple) or len(requests) > 16:
            raise AuthorizationError("Host Provider data declaration is invalid")
        for request in requests:
            _validate_request(request)
        if len(set(requests)) != len(requests):
            raise AuthorizationError("Host Provider data declaration is duplicated")
        captured = []
        for request in requests:
            digest = self._effective.get(request.pack_id)
            if digest is None:
                continue
            if request not in self._data:
                if request.pack_id not in self._roots:
                    root = resolve_admitted_pack_root(request.pack_id, self._ecosystem_root)
                    self._roots[request.pack_id] = (root, _root_identity(root))
                root, identity = self._roots[request.pack_id]
                if _root_identity(root) != identity:
                    raise AuthorizationError("captured data Pack root changed")
                files = capture_declared_pack_data(
                    root,
                    pack_id=request.pack_id,
                    artifact_digest=digest,
                    path_prefix=request.path_prefix,
                )
                if _root_identity(root) != identity:
                    raise AuthorizationError("captured data Pack root changed")
                self._data[request] = CapturedHostPackDataV4(
                    request.pack_id, digest, request.path_prefix, files
                )
            captured.append(self._data[request])
        self.assert_current()
        return tuple(captured)

    def assert_current(self) -> None:
        """Fence replaced or removed data roots for the captured dispatch session."""
        for root, identity in self._roots.values():
            if _root_identity(root) != identity:
                raise AuthorizationError("captured data Pack root changed")


def _validate_request(request: object) -> None:
    if not isinstance(request, HostProviderDataRequestV4):
        raise AuthorizationError("Host Provider data request is invalid")
    prefix = request.path_prefix
    if (
        not isinstance(request.pack_id, str)
        or re.fullmatch(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", request.pack_id) is None
        or not isinstance(prefix, str)
        or not prefix.endswith("/")
        or "\\" in prefix
        or "\x00" in prefix
        or PurePosixPath(prefix).is_absolute()
        or ".." in PurePosixPath(prefix).parts
        or str(PurePosixPath(prefix)) + "/" != prefix
        or prefix == "./"
    ):
        raise AuthorizationError("Host Provider data request is invalid")


def _root_identity(root: Path) -> tuple[int, int]:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise AuthorizationError("captured data Pack root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or (
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise AuthorizationError("captured data Pack root is unsafe")
    return int(metadata.st_dev), int(metadata.st_ino)
