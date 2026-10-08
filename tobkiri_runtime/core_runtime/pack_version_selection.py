"""Exact signed Pack revision selection, independent of execution authority."""

from __future__ import annotations

from typing import Any, Mapping

from packaging.version import InvalidVersion, Version

from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.ids import validate_artifact_digest, validate_canonical_id
from tobkiri_protocol.validation import validate_file

from .pack_boundary import load_pack_catalog, resolve_pack_root


def version_catalog_key(pack_id: str, artifact_digest: str) -> str:
    """Return the authenticated catalog key for one immutable added revision."""

    validate_canonical_id(pack_id, field="pack_id")
    validate_artifact_digest(artifact_digest, field="artifact_digest")
    return f"{pack_id}@{artifact_digest}"


def catalog_key_identity(key: str, entry: Mapping[str, Any]) -> str:
    """Reject aliases between a catalog key and its signed artifact identity."""

    pack_id = str(entry.get("pack_id") or "")
    if key not in {
        pack_id,
        version_catalog_key(pack_id, str(entry.get("artifact_digest") or "")),
    }:
        raise ValueError("Pack revision catalog key is inconsistent")
    return pack_id


def verify_version_predecessor(
    pack_id: str,
    expected_digest: str,
    *,
    versions: Mapping[str, Mapping[str, Mapping[str, Any]]],
    publisher_id: str,
    version: str,
    artifact_digest: str | None = None,
) -> None:
    """Bind an update to a retained Normal Pack and its approved publisher.

    A bundled baseline is verified from its finite catalog root. Its first
    signed update requires the native publisher confirmation, not implicit
    trust in a key embedded in the new Pack. Subsequent updates retain that
    publisher identity. Admission never changes the baseline selection.
    """

    validate_artifact_digest(expected_digest, field="predecessor_artifact_digest")
    previous = versions.get(pack_id, {}).get(expected_digest)
    if previous is not None:
        if previous["publisher_id"] != publisher_id:
            raise ValueError("Pack update publisher differs from its predecessor")
        previous_version = str(previous["version_string"])
    else:
        record = load_pack_catalog().get(pack_id)
        if record is None or record.get("kind") not in {"normal_sandbox", "application"}:
            raise ValueError("Pack update predecessor is not a Normal Pack")
        root = resolve_pack_root(pack_id)
        manifest = validate_file(root / "pack.v4.json", "pack")
        compiled = compile_pack_root(root)
        if (
            manifest["pack"]["kind"] not in {"normal_sandbox", "application"}
            or manifest["requirements"]["execution_boundary"] not in {"sandbox", "declarative_only"}
            or compiled.artifact.pack_id != pack_id
            or compiled.artifact.digest != expected_digest
        ):
            raise ValueError("bundled Pack update predecessor digest is stale")
        previous_version = str(manifest["pack"]["version"])
        publishers = {row["publisher_id"] for row in versions.get(pack_id, {}).values()}
        if publishers and publishers != {publisher_id}:
            raise ValueError("bundled Pack update publisher differs from retained revisions")
    try:
        for retained in versions.get(pack_id, {}).values():
            if Version(str(retained["version_string"])) == Version(version):
                if retained["artifact_digest"] != artifact_digest:
                    raise ValueError("Pack version is already bound to a different digest")
                if retained["publisher_id"] != publisher_id:
                    raise ValueError("Pack revision publisher is inconsistent")
        if previous is not None and previous["artifact_digest"] == artifact_digest:
            if Version(version) != Version(previous_version):
                raise ValueError("Pack revision version is inconsistent")
            return
        if Version(version) <= Version(previous_version):
            raise ValueError("Pack update must have a newer version")
    except InvalidVersion as error:
        raise ValueError("Pack update version is invalid") from error
