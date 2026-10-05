"""One-shot binding for the Launcher-verified packaged application bundle.

The sealed Python bootstrap owns the raw launch arguments and only exposes the
validated PackVM bundle evidence through its opaque dispatch scope.  This
module intentionally has no environment-variable, JSON, or raw-value setter:
consumers can only read the binding installed during sealed role preparation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import threading
from types import MappingProxyType
from typing import Mapping


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
_BINDING_KEYS = (
    "root",
    "provisioning_sha256",
    "helper_manifest_sha256",
    "helper_team_id",
)
_BINDING_LOCK = threading.RLock()
_PACKVM_BUNDLE_BINDING: PackagedApplicationBundleBinding | None = None
_PACKVM_BUNDLE_BINDING_INITIALIZED = False


class PackagedApplicationBundleBindingError(RuntimeError):
    """Raised when sealed PackVM bundle evidence is absent or invalid."""


@dataclass(frozen=True)
class PackagedApplicationBundleBinding:
    """Immutable Launcher-verified identity for one packaged ``.app`` bundle."""

    root: Path
    provisioning_sha256: str
    helper_manifest_sha256: str
    helper_team_id: str


def install_packvm_bundle_binding_from_sealed_scope(
    scope: object,
    module_file: str | Path,
) -> PackagedApplicationBundleBinding | None:
    """Install exactly one PackVM bundle binding issued by sealed bootstrap.

    The opaque dispatch scope must validate the exact module target before it
    returns its immutable mapping.  A sealed non-macOS role legitimately has
    no PackVM application bundle; that absence is also sealed one-shot.
    Repeating preparation with the same evidence is harmless, while any
    attempt to exchange absence for a bundle (or vice versa) fails closed.
    """

    app_root_for = getattr(scope, "app_root_for", None)
    binding_for = getattr(scope, "packvm_bundle_binding_for", None)
    if not callable(app_root_for) or not callable(binding_for):
        raise PackagedApplicationBundleBindingError(
            "sealed dispatch scope lacks PackVM bundle authority"
        )

    # Both methods are target-bound by the sealed bootstrap.  Calling the
    # application-root capability also prevents a scope that only exposes a
    # syntactically plausible mapping from becoming a trusted binding.
    try:
        app_root = app_root_for(module_file)
        raw_binding = binding_for(module_file)
    except Exception as exc:
        raise PackagedApplicationBundleBindingError(
            "sealed PackVM bundle authority is unavailable"
        ) from exc
    if not isinstance(app_root, Path):
        raise PackagedApplicationBundleBindingError(
            "sealed dispatch scope returned an invalid application root"
        )

    binding = _validated_binding(raw_binding)
    with _BINDING_LOCK:
        global _PACKVM_BUNDLE_BINDING, _PACKVM_BUNDLE_BINDING_INITIALIZED
        if not _PACKVM_BUNDLE_BINDING_INITIALIZED:
            _PACKVM_BUNDLE_BINDING = binding
            _PACKVM_BUNDLE_BINDING_INITIALIZED = True
            return binding
        existing = _PACKVM_BUNDLE_BINDING
        if existing != binding:
            raise PackagedApplicationBundleBindingError(
                "PackVM application bundle binding was already established"
            )
        return existing


def packvm_bundle_binding() -> PackagedApplicationBundleBinding | None:
    """Return the sealed one-shot PackVM bundle identity, if this is a sealed run."""

    with _BINDING_LOCK:
        return _PACKVM_BUNDLE_BINDING


def _validated_binding(
    raw_binding: object,
) -> PackagedApplicationBundleBinding | None:
    """Decode only the bootstrap's immutable exact PackVM binding mapping."""

    if raw_binding is None:
        return None
    if not isinstance(raw_binding, MappingProxyType):
        raise PackagedApplicationBundleBindingError("sealed PackVM bundle binding is not immutable")
    if tuple(raw_binding) != _BINDING_KEYS:
        raise PackagedApplicationBundleBindingError("sealed PackVM bundle binding shape is invalid")
    values: Mapping[str, object] = raw_binding
    root_value = values["root"]
    provisioning_sha256 = values["provisioning_sha256"]
    helper_manifest_sha256 = values["helper_manifest_sha256"]
    helper_team_id = values["helper_team_id"]
    if not isinstance(root_value, str):
        raise PackagedApplicationBundleBindingError("sealed PackVM bundle root is invalid")
    try:
        root = Path(root_value)
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PackagedApplicationBundleBindingError(
            "sealed PackVM bundle root is unavailable"
        ) from exc
    if (
        not root.is_absolute()
        or root != resolved_root
        or root.is_symlink()
        or not root.is_dir()
        or root.suffix != ".app"
    ):
        raise PackagedApplicationBundleBindingError("sealed PackVM bundle root is not canonical")
    if not isinstance(provisioning_sha256, str) or _DIGEST.fullmatch(provisioning_sha256) is None:
        raise PackagedApplicationBundleBindingError("sealed PackVM provisioning digest is invalid")
    if (
        not isinstance(helper_manifest_sha256, str)
        or _DIGEST.fullmatch(helper_manifest_sha256) is None
    ):
        raise PackagedApplicationBundleBindingError(
            "sealed PackVM helper manifest digest is invalid"
        )
    if not isinstance(helper_team_id, str) or (
        helper_team_id and _TEAM_ID.fullmatch(helper_team_id) is None
    ):
        raise PackagedApplicationBundleBindingError("sealed PackVM helper team identity is invalid")
    return PackagedApplicationBundleBinding(
        root=root,
        provisioning_sha256=provisioning_sha256,
        helper_manifest_sha256=helper_manifest_sha256,
        helper_team_id=helper_team_id,
    )


__all__ = [
    "PackagedApplicationBundleBinding",
    "PackagedApplicationBundleBindingError",
    "install_packvm_bundle_binding_from_sealed_scope",
    "packvm_bundle_binding",
]
