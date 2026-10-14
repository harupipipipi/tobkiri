"""Serve public UI bytes from one exact, authenticated Pack capture."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from types import MappingProxyType
from typing import Callable, Mapping
from urllib.parse import unquote

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document

from .external_pack_catalog_v4 import _read_regular_from_root_descriptor
from .api.web_mounts import WebMountEntry


_PUBLIC_SUFFIXES = frozenset(
    {
        ".html",
        ".js",
        ".css",
        ".png",
        ".svg",
        ".wasm",
        ".task",
        ".txt",
        ".md",
        ".woff",
        ".woff2",
        ".ttf",
        ".ico",
        ".webp",
        ".jpg",
        ".jpeg",
        ".gif",
    }
)
_MAX_ASSET_BYTES = 32 * 1024 * 1024


class CapturedPackUIAssetDenied(RuntimeError):
    """The selected Pack cannot supply the requested authenticated UI byte."""


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise CapturedPackUIAssetDenied("captured UI root is unavailable")
    return metadata.st_dev, metadata.st_ino


def _open_root(path: Path, expected: tuple[int, int]) -> int:
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise CapturedPackUIAssetDenied("captured UI root is unsafe")
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != expected:
            raise CapturedPackUIAssetDenied("captured UI root identity changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _public_asset_path(value: str) -> str:
    parts = PurePosixPath(value).parts
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or "/".join(parts) != value
        or not parts
        or parts[0] != "ui"
        or any(part in {".", ".."} or part.startswith(".") for part in parts)
        or PurePosixPath(value).suffix.lower() not in _PUBLIC_SUFFIXES
    ):
        raise CapturedPackUIAssetDenied("captured UI asset path is unsafe")
    return value


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _read_ui_regular(descriptor: int, relative: str) -> tuple[bytes, int]:
    return _read_regular_from_root_descriptor(
        descriptor,
        relative,
        max_file_bytes=_MAX_ASSET_BYTES,
    )


@dataclass(frozen=True)
class CapturedPackUIAssets:
    """Immutable asset inventory owned by a live, exact dispatch capture.

    Signature admission and selected artifact resolution precede construction.
    Reads never choose a newer catalog revision or an alternate source root.
    """

    pack_id: str
    artifact_digest: str
    pack_root: Path
    root_identity: tuple[int, int]
    owner_root: Path
    owner_identity: tuple[int, int]
    asset_digests: Mapping[str, str]
    index_file: str
    assert_current: Callable[[], None]
    legacy_builtin: bool = False

    @classmethod
    def capture(
        cls,
        *,
        pack_id: str,
        artifact_digest: str,
        pack_root: Path,
        owner_root: Path,
        index_file: str,
        assert_current: Callable[[], None],
        legacy_builtin: bool = False,
    ) -> CapturedPackUIAssets:
        """Verify a selected Pack's sealed UI inventory without directory discovery."""
        root = Path(pack_root).absolute()
        owner = Path(owner_root).absolute()
        root_identity = _directory_identity(root)
        owner_identity = _directory_identity(owner)
        descriptor = _open_root(root, root_identity)
        try:
            manifest_raw, _ = _read_ui_regular(descriptor, "pack.v4.json")
            index_raw, _ = _read_ui_regular(descriptor, "artifact-index.v4.json")
            manifest = validate_document(manifest_raw, "pack")
            index = validate_document(index_raw, "pack_artifact_index")
            if (
                manifest["pack"]["id"] != pack_id
                or index["pack_id"] != pack_id
                or manifest["pack"]["artifact_digest"] != artifact_digest
                or canonical_digest(manifest["artifacts"]) != artifact_digest
                or manifest["integrity"]["artifact_set_digest"] != artifact_digest
                or index["artifact_set_digest"] != artifact_digest
                or index["source_identity"] != manifest["integrity"]["source_identity"]
                or index["integrity_seal"]["signed_digest"]
                != canonical_digest(
                    {
                        key: value
                        for key, value in index.items()
                        if key != "integrity_seal"
                    }
                )
            ):
                raise CapturedPackUIAssetDenied("captured UI artifact identity changed")
            declared = [
                item for item in manifest["artifacts"] if item["path"].startswith("ui/")
            ]
            indexed = [
                item for item in index["artifacts"] if item["path"].startswith("ui/")
            ]
            if not declared and not indexed and legacy_builtin:
                assets: dict[str, str] = {}
            else:
                assets = {}
                for item in declared:
                    path = _public_asset_path(item["path"])
                    if item["kind"] != "asset" or path in assets:
                        raise CapturedPackUIAssetDenied(
                            "captured UI asset declaration is invalid"
                        )
                    assets[path] = item["digest"]
                index_assets: dict[str, str] = {}
                for item in indexed:
                    path = _public_asset_path(item["path"])
                    if item["role"] != "asset" or path in index_assets:
                        raise CapturedPackUIAssetDenied(
                            "captured UI asset index is invalid"
                        )
                    index_assets[path] = item["digest"]
                if assets != index_assets or f"ui/{index_file}" not in assets:
                    raise CapturedPackUIAssetDenied(
                        "captured UI asset inventory is incomplete"
                    )
                for path, expected_digest in assets.items():
                    content, _ = _read_ui_regular(descriptor, path)
                    if (
                        len(content) > _MAX_ASSET_BYTES
                        or _digest(content) != expected_digest
                    ):
                        raise CapturedPackUIAssetDenied(
                            "captured UI asset digest changed"
                        )
                legacy_builtin = False
            result = cls(
                pack_id,
                artifact_digest,
                root,
                root_identity,
                owner,
                owner_identity,
                MappingProxyType(assets),
                index_file,
                assert_current,
                legacy_builtin,
            )
            result._assert_current()
            return result
        finally:
            os.close(descriptor)

    def _assert_current(self) -> None:
        from .bootstrap.profile_capture import runtime_user_data_root

        if (
            runtime_user_data_root().resolve(strict=True) != self.owner_root
            or _directory_identity(self.owner_root) != self.owner_identity
        ):
            raise CapturedPackUIAssetDenied("captured UI Host owner changed")
        self.assert_current()
        if _directory_identity(self.pack_root) != self.root_identity:
            raise CapturedPackUIAssetDenied("captured UI Pack root changed")

    def read(self, relative: str, spa_fallback: bool) -> tuple[bytes, str]:
        """Return verified bytes and filename, or reject without source fallback."""
        if self.legacy_builtin:
            raise CapturedPackUIAssetDenied("legacy UI uses its explicit bundled mount")
        self._assert_current()
        decoded = unquote(relative, errors="strict")
        # A second encoded layer is never a filesystem path or SPA route.
        parts = PurePosixPath(decoded).parts
        if (
            "\\" in decoded
            or "\x00" in decoded
            or "%" in decoded
            or any(part in {".", ".."} or part.startswith(".") for part in parts)
            or decoded.startswith("//")
        ):
            raise CapturedPackUIAssetDenied("captured UI request path is unsafe")
        name = decoded.lstrip("/") or self.index_file
        path = f"ui/{name}"
        if path not in self.asset_digests:
            if spa_fallback and "." not in PurePosixPath(name).name:
                path = f"ui/{self.index_file}"
            else:
                raise FileNotFoundError("UI asset is not declared")
        expected = self.asset_digests[path]
        descriptor = _open_root(self.pack_root, self.root_identity)
        try:
            content, _ = _read_ui_regular(descriptor, path)
            if len(content) > _MAX_ASSET_BYTES or _digest(content) != expected:
                raise CapturedPackUIAssetDenied("captured UI asset digest changed")
            self._assert_current()
            return content, path
        finally:
            os.close(descriptor)


def bind_captured_pack_web_mounts(
    mounts: tuple[WebMountEntry, ...] | None,
    capture: Callable[[str, str], CapturedPackUIAssets | None] | None,
) -> tuple[WebMountEntry, ...] | None:
    """Bind trusted launch declarations to the selected capture's UI assets.

    The source declaration supplies an owner hint, never an artifact root.
    Only the production capture resolves that hint against its finite pins.
    """
    if mounts is None or capture is None:
        return mounts
    captured: dict[tuple[str, str], CapturedPackUIAssets | None] = {}
    result: list[WebMountEntry] = []
    for original in mounts:
        mount: WebMountEntry = dict(original)  # type: ignore[assignment]
        source_ui_root = Path(mount["web_root"]).absolute()
        if source_ui_root.name != "ui":
            result.append(mount)
            continue
        key = source_ui_root.parent.name, mount["index_file"]
        if key not in captured:
            captured[key] = capture(*key)
        assets = captured[key]
        if assets is None:
            raise CapturedPackUIAssetDenied("UI mount owner is not selected")
        if not mount["auth_required"]:
            raise CapturedPackUIAssetDenied("captured Pack UI must be authenticated")
        if assets.legacy_builtin:
            if source_ui_root != assets.pack_root / "ui":
                raise CapturedPackUIAssetDenied(
                    "legacy UI mount differs from its bundled root"
                )
        else:
            mount["web_root"] = assets.pack_root / "ui"
            mount["byte_reader"] = assets.read
        result.append(mount)
    return tuple(result)
