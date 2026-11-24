"""Bind optional retained bytes to one normally captured active Profile."""

from __future__ import annotations

from pathlib import Path
import stat
from typing import Any

from tobkiri_protocol.canonical import canonical_digest

from .bundled_artifact_retention_v4 import (
    BundledArtifactOrigin,
    BundledArtifactRetentionDenied,
    assert_retention_origin_current,
    capture_retention_origin,
)


def retained_origin_for_verified_active_profile(
    active: Any, *, user_data_root: Path
) -> BundledArtifactOrigin | None:
    """Capture an exact active origin only when this owner has retained bytes.

    Store presence is only an optimization. It never authenticates a bundle,
    grants access, or copies files. The Host supplies the owner root, and normal
    active capture verifies Authority before any retained root may be read.
    Existing owners without retained storage keep the usual read path.
    """
    root = Path(user_data_root).absolute()
    store = root / "retained-bundled-artifacts-v4"
    try:
        metadata = store.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        raise BundledArtifactRetentionDenied("retained artifact store is unsafe")

    origin = capture_retention_origin(root)
    assert_retention_origin_current(origin)
    profile = active.resolved.profile
    lock = active.resolved.lock
    plan = active.resolved.plan
    activation = active.activation
    identity = (
        str(profile["profile_id"]),
        str(plan["profile_revision"]),
        str(activation["activation_id"]),
        str(plan["plan_digest"]),
        str(lock["lock_digest"]),
    )
    if (
        identity != origin.profile_binding
        or canonical_digest(profile) != plan["profile_revision"]
        or canonical_digest(
            {key: value for key, value in plan.items() if key != "plan_digest"}
        )
        != origin.plan_digest
        or canonical_digest(
            {key: value for key, value in lock.items() if key != "lock_digest"}
        )
        != lock["lock_digest"]
        or lock["plan_digest"] != origin.plan_digest
        or plan["bundle_digest"] != origin.bundle_digest
        or lock["bundle_digest"] != origin.bundle_digest
        or plan["security_epoch"] != origin.security_epoch
        or activation["security_epoch"] != origin.security_epoch
        or activation["fencing_token"] != origin.fencing_token
    ):
        raise BundledArtifactRetentionDenied(
            "retained artifact origin differs from the captured active Profile"
        )
    assert_retention_origin_current(origin)
    return origin
