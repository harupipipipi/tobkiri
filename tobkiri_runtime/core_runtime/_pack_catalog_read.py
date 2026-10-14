"""Internal metadata reuse inside a fully verified physical Pack read."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from tobkiri_protocol.canonical import canonical_digest

from .admitted_pack_read_v4 import VerifiedPackRootRead, verified_pack_root_read
from .external_pack_catalog_v4 import (
    ExternalPackCatalogDenied,
    _catalog_path,
    _read_authenticated_catalog,
    _visible_entry,
)
from .pack_artifact_integrity import read_host_policy_snapshot
from .pack_boundary import load_pack_catalog

_TOKEN = object()


class _ReadScope:
    def __init__(self) -> None:
        self.thread = threading.get_ident()
        self.view: _CatalogRead | None = None


_CURRENT_SCOPE: ContextVar[_ReadScope | None] = ContextVar(
    "physical_pack_catalog_read", default=None
)


def _visible_catalog(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: dict(visible)
        for key, entry in value["entries"].items()
        if (visible := _visible_entry(entry)) is not None
    }


def _revision(value: Mapping[str, Any]) -> str:
    return canonical_digest(
        {
            "version": value["version"],
            "entries": _visible_catalog(value),
            "journal": value["journal"],
        }
    )


def _policy(root: Path) -> str | None:
    path = root / "pack_control/publisher_trust.v4.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return canonical_digest(read_host_policy_snapshot(path))


class _CatalogRead:
    """An internally issued, nonescaping metadata view, never a decision cache."""

    def __init__(
        self,
        token: object,
        roots: VerifiedPackRootRead,
        raw: Mapping[str, Any],
        bundled: Mapping[str, Any],
        owner: Path,
        active_identity: str | None,
    ) -> None:
        scope = _CURRENT_SCOPE.get()
        if (
            token is not _TOKEN
            or scope is None
            or scope.thread != threading.get_ident()
            or scope.view is not None
        ):
            raise ExternalPackCatalogDenied("catalog read must be internally issued")
        self._scope = scope
        self._final_checks: list[Callable[[], None]] = []
        self._token = token
        self._roots = roots
        self._owner = owner
        self._active_identity = active_identity
        self._pins = dict(roots._pins)
        self._active = True
        self._snapshot_digest = self._snapshot_identity()
        self._bundled = json.dumps(bundled, sort_keys=True, separators=(",", ":"))
        self._records = self._bundled
        records = json.loads(self._bundled)
        for key, entry in _visible_catalog(raw).items():
            pack_id = entry["pack_id"]
            if key != pack_id:
                continue  # Preserve baseline projection, not latest/active versions.
            if pack_id in records:
                # The ordinary unpinned admitted catalog rejects all shadows.
                raise ExternalPackCatalogDenied("external Pack shadows bundled catalog")
            records[pack_id] = entry["catalog_record"]
        self._records = json.dumps(records, sort_keys=True, separators=(",", ":"))
        self._revision = canonical_digest(
            {
                "bundled": bundled,
                "external_normal_packs": roots._snapshot.versions,
            }
        )
        self._derived_digest = canonical_digest(
            [
                self._records,
                self._bundled,
                self._revision,
                str(owner),
                active_identity,
            ]
        )
        scope.view = self

    def _snapshot_identity(self) -> str:
        value = self._roots._snapshot
        return canonical_digest(
            {
                "revision": value.revision,
                "records": value.records,
                "entries": value.entries,
                "versions": value.versions,
                "journal": list(value.journal),
                "roots": {key: str(root) for key, root in value.roots.items()},
            }
        )

    def validate(self) -> None:
        """Reject closure or mutation of any reused authenticated metadata."""
        from .bootstrap.profile_capture import runtime_user_data_root

        if (
            not self._active
            or self._token is not _TOKEN
            or _CURRENT_SCOPE.get() is not self._scope
            or self._scope.view is not self
            or self._scope.thread != threading.get_ident()
            or not self._roots._active
            or self._roots._pins != self._pins
            or runtime_user_data_root() != self._owner
            or canonical_digest(
                [
                    self._records,
                    self._bundled,
                    self._revision,
                    str(self._owner),
                    self._active_identity,
                ]
            )
            != self._derived_digest
            or self._snapshot_identity() != self._snapshot_digest
        ):
            raise ExternalPackCatalogDenied("catalog read is closed or changed")

    def register_final_check(self, check: Callable[[], None]) -> None:
        """Register a fresh mutable guard for after the physical boundary walks."""
        self.validate()
        self._final_checks.append(check)

    def final_checks(self) -> tuple[Callable[[], None], ...]:
        """Freeze guards for the issuing caller's final publication boundary."""
        self.validate()
        return tuple(self._final_checks)

    def require_active_identity(self, identity: str) -> None:
        """Reject mixing another active snapshot into this internal scope."""
        self.validate()
        if self._active_identity != identity:
            raise ExternalPackCatalogDenied("catalog read active binding differs")

    def catalog(self) -> dict[str, Any]:
        """Return detached baseline records from this authenticated scope."""
        self.validate()
        return json.loads(self._records)

    @property
    def revision(self) -> str:
        """Return the revision of the complete retained-version inventory."""
        self.validate()
        return self._revision

    def resolve(self, pack_id: str) -> Path:
        """Run existing exact-pin root checks, never resolve outside this scope."""
        self.validate()
        return self._roots.resolve(pack_id)

    def close(self) -> None:
        self._active = False


@contextmanager
def _catalog_metadata_read(
    root: Path,
    pins: Mapping[str, str],
    *,
    origin: Any = None,
    active_identity: str | None = None,
) -> Iterator[_CatalogRead]:
    """Reuse metadata between full catalog/root/Origin boundary checks.

    No arbitrary snapshot may be supplied. Existing canonical readers create
    the scope. Authority initialization keeps its normal lifecycle semantics.
    Publisher policy is read through its usual guarded, nonwriting reader.
    """
    if _CURRENT_SCOPE.get() is not None:
        raise ExternalPackCatalogDenied("nested physical catalog read is denied")
    scope = _ReadScope()
    current = _CURRENT_SCOPE.set(scope)
    view = None
    try:
        raw = _read_authenticated_catalog(_catalog_path(root), allow_missing=True)
        bundled = load_pack_catalog()
        policy = _policy(root)
        with verified_pack_root_read(pins, origin=origin) as roots:
            if _revision(raw) != roots._snapshot.revision:
                raise ExternalPackCatalogDenied("catalog changed before read")
            view = _CatalogRead(_TOKEN, roots, raw, bundled, root, active_identity)
            yield view
            view.validate()
            if (
                canonical_digest(load_pack_catalog()) != canonical_digest(bundled)
                or _policy(root) != policy
                or _revision(
                    _read_authenticated_catalog(_catalog_path(root), allow_missing=True)
                )
                != roots._snapshot.revision
            ):
                raise ExternalPackCatalogDenied(
                    "catalog or publisher policy changed during read"
                )
    finally:
        if view is not None:
            view.close()
        scope.view = None
        _CURRENT_SCOPE.reset(current)
