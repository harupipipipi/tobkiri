"""Batch exact Pack roots within a fully revalidated inspection scope."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Mapping

from tobkiri_host.artifact_compiler import compile_pack_root

from .external_pack_catalog_v4 import (
    ExternalPackCatalogDenied,
    ExternalPackCatalogSnapshot,
    _assert_retention_origin_current_owner,
    _verify_retained_bundled_pack,
    load_external_pack_catalog,
)
from .pack_boundary import PackBoundaryError, load_pack_catalog, resolve_pack_root
from .bundled_host_baseline_v4 import current_bundled_host_baseline_read

if TYPE_CHECKING:
    from .bundled_artifact_retention_v4 import BundledArtifactOrigin


def _compiled_digest(root: Path) -> str:
    try:
        return compile_pack_root(root).artifact.digest
    except Exception as error:
        raise ExternalPackCatalogDenied("bundled Pack artifact is invalid") from error


class VerifiedPackRootRead:
    """Resolve one captured pin set; never retain this object across reads."""

    def __init__(
        self,
        pins: Mapping[str, str],
        snapshot: ExternalPackCatalogSnapshot,
        *,
        origin: BundledArtifactOrigin | None = None,
    ) -> None:
        self._pins = dict(pins)
        self._snapshot = snapshot
        self._used: dict[str, tuple[Path, tuple[int, int]]] = {}
        self._origin = origin
        self._baseline_read = current_bundled_host_baseline_read()
        self._retained: set[str] = set()
        self._active = True

    def resolve(self, pack_id: str) -> Path:
        """Resolve an exact admitted or bundled root with its usual jail checks."""
        if not self._active:
            raise ExternalPackCatalogDenied("Pack root read scope is closed")
        if pack_id not in self._pins:
            raise ExternalPackCatalogDenied(
                "Pack is absent from the captured read pin set"
            )
        root = self._snapshot.roots.get(pack_id)
        if root is None:
            try:
                root = resolve_pack_root(pack_id)
            except PackBoundaryError as error:
                if pack_id not in load_pack_catalog():
                    # A sealed bundle-only artifact has no canonical source
                    # root. The caller must separately validate its kind.
                    raise
                raise ExternalPackCatalogDenied(
                    "selected bundled Pack root is unavailable"
                ) from error
            if _compiled_digest(root) != self._pins[pack_id]:
                if self._origin is None:
                    raise ExternalPackCatalogDenied(
                        "selected bundled Pack revision is unavailable"
                    )
                root = _verify_retained_bundled_pack(
                    self._origin, pack_id=pack_id, artifact_digest=self._pins[pack_id]
                )
                self._retained.add(pack_id)
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
        if self._origin is not None:
            _assert_retention_origin_current_owner(self._origin)
        if current_bundled_host_baseline_read() is not self._baseline_read:
            raise ExternalPackCatalogDenied("bundled baseline read scope changed")
        current = load_external_pack_catalog(self._pins)
        if (
            current.revision != self._snapshot.revision
            or current.entries != self._snapshot.entries
            or current.roots != self._snapshot.roots
        ):
            raise ExternalPackCatalogDenied("external Pack catalog changed during read")
        for pack_id, (root, identity) in self._used.items():
            if pack_id not in current.roots:
                if pack_id in self._retained:
                    assert self._origin is not None
                    # Preserve the baseline jail/compiler checks even when the
                    # exact old revision comes from the separate retained root.
                    _compiled_digest(resolve_pack_root(pack_id))
                    checked = _verify_retained_bundled_pack(
                        self._origin,
                        pack_id=pack_id,
                        artifact_digest=self._pins[pack_id],
                    )
                else:
                    checked = resolve_pack_root(pack_id)
                if checked != root or _compiled_digest(checked) != self._pins[pack_id]:
                    raise ExternalPackCatalogDenied("bundled Pack changed during read")
            metadata = root.lstat()
            if (
                root.is_symlink()
                or not root.is_dir()
                or (metadata.st_dev, metadata.st_ino) != identity
            ):
                raise ExternalPackCatalogDenied(
                    "selected Pack root changed during read"
                )
        if self._origin is not None:
            _assert_retention_origin_current_owner(self._origin)

    def close(self) -> None:
        """Prevent a retained resolver from escaping its verified read scope."""
        self._active = False


@contextmanager
def verified_pack_root_read(
    artifact_pins: Mapping[str, str],
    *,
    origin: BundledArtifactOrigin | None = None,
) -> Iterator[VerifiedPackRootRead]:
    """Batch root inspection, fully rechecking before a result can escape.

    This scope must contain no effects, admission, approvals or retention. It
    owns no process cache and performs full authenticated CAS verification on
    both boundaries, including retained revisions outside the selected pins.
    An explicit retained origin also requires fresh normal Authority reads;
    their standard Store initialization may perform schema/metadata writes.
    Authorization decisions are never cached by this scope.
    """
    pins = dict(artifact_pins)
    if origin is not None:
        _assert_retention_origin_current_owner(origin)
    read = VerifiedPackRootRead(pins, load_external_pack_catalog(pins), origin=origin)
    try:
        yield read
        read.verify_current()
    finally:
        read.close()
