"""Batch exact Pack roots only within a fully revalidated pure read."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from tobkiri_host.artifact_compiler import compile_pack_root

from .external_pack_catalog_v4 import (
    ExternalPackCatalogDenied,
    ExternalPackCatalogSnapshot,
    load_external_pack_catalog,
)
from .pack_boundary import PackBoundaryError, resolve_pack_root


def _compiled_digest(root: Path) -> str:
    try:
        return compile_pack_root(root).artifact.digest
    except Exception as error:
        raise ExternalPackCatalogDenied("bundled Pack artifact is invalid") from error


class VerifiedPackRootRead:
    """Resolve one captured pin set; never retain this object across reads."""

    def __init__(self, pins: Mapping[str, str], snapshot: ExternalPackCatalogSnapshot) -> None:
        self._pins = dict(pins)
        self._snapshot = snapshot
        self._used: dict[str, tuple[Path, tuple[int, int]]] = {}
        self._active = True

    def resolve(self, pack_id: str) -> Path:
        """Resolve an exact admitted or bundled root with its usual jail checks."""
        if not self._active:
            raise ExternalPackCatalogDenied("Pack root read scope is closed")
        if pack_id not in self._pins:
            raise PackBoundaryError("Pack is absent from the captured read pin set")
        root = self._snapshot.roots.get(pack_id)
        if root is None:
            root = resolve_pack_root(pack_id)
            if _compiled_digest(root) != self._pins[pack_id]:
                raise PackBoundaryError("selected bundled Pack revision is unavailable")
        elif self._snapshot.entries[pack_id]["artifact_digest"] != self._pins[pack_id]:
            raise ExternalPackCatalogDenied("selected external Pack revision changed")
        metadata = root.lstat()
        if root.is_symlink() or not root.is_dir():
            raise ExternalPackCatalogDenied("selected Pack root is unavailable")
        identity = (metadata.st_dev, metadata.st_ino)
        previous = self._used.setdefault(pack_id, (root, identity))
        if previous != (root, identity):
            raise ExternalPackCatalogDenied("selected Pack root changed during read")
        return root

    def verify_current(self) -> None:
        """Reverify catalog authentication, all CAS bytes and used bundled roots."""
        current = load_external_pack_catalog(self._pins)
        if (
            current.revision != self._snapshot.revision
            or current.entries != self._snapshot.entries
            or current.roots != self._snapshot.roots
        ):
            raise ExternalPackCatalogDenied("external Pack catalog changed during read")
        for pack_id, (root, identity) in self._used.items():
            if pack_id not in current.roots:
                checked = resolve_pack_root(pack_id)
                if checked != root or _compiled_digest(checked) != self._pins[pack_id]:
                    raise ExternalPackCatalogDenied("bundled Pack changed during read")
            metadata = root.lstat()
            if (
                root.is_symlink()
                or not root.is_dir()
                or (metadata.st_dev, metadata.st_ino) != identity
            ):
                raise ExternalPackCatalogDenied("selected Pack root changed during read")

    def close(self) -> None:
        """Prevent a retained resolver from escaping its verified read scope."""
        self._active = False


@contextmanager
def verified_pack_root_read(
    artifact_pins: Mapping[str, str],
) -> Iterator[VerifiedPackRootRead]:
    """Batch pure root inspection, fully rechecking before a result can escape.

    This scope must contain no effects, approvals or Authority mutations. It
    owns no process cache and performs full authenticated CAS verification on
    both boundaries, including retained revisions outside the selected pins.
    """
    pins = dict(artifact_pins)
    read = VerifiedPackRootRead(pins, load_external_pack_catalog(pins))
    try:
        yield read
        read.verify_current()
    finally:
        read.close()
