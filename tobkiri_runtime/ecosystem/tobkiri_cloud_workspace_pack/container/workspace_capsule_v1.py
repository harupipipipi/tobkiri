"""Dependency-free, bounded and content-addressed portable workspace format."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import unicodedata
import zipfile
from pathlib import PurePosixPath
from typing import Any, Mapping

VERSION = "tobkiri.workspace-capsule.v1"
HANDOFF_VERSION = "tobkiri.workspace-handoff.v1"
MAX_FILES = 128
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROTECTED = {
    "capsule-manifest.json",
    ".git",
    ".ssh",
    ".aws",
    ".azure",
    ".docker",
    ".gnupg",
    ".kube",
    ".tobkiri",
    ".rumi",
    ".rumi_snapshots",
    "secrets",
    "credentials",
    "approvals",
    "leases",
    "user_data",
    "userdata",
    "node_modules",
    "__pycache__",
}
_SECRET_NAMES = {
    "capsule-manifest.json",
    ".dockercfg",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
    "kubeconfig",
    "token",
    "tokens.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}


def canonical(value: Any) -> bytes:
    """Encode finite portable JSON without ambient object serialization."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(data: bytes) -> str:
    """Return an explicit SHA-256 content identity."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def identifier(value: Any) -> str:
    """Reject path-like and oversized public identities."""
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("workspace identity is invalid")
    return value


def integer(value: Any, minimum: int = 0) -> int:
    """Require an exact bounded integer, excluding booleans."""
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ValueError("workspace revision is invalid")
    return value


def content_digest(value: Any) -> str:
    """Require a versioned digest instead of accepting arbitrary metadata."""
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("workspace content digest is invalid")
    return value


def safe_path(value: Any) -> str:
    """Reject traversal, secret paths and platform-specific path aliases."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(c) < 32 for c in value)
        or "\\" in value
        or ":" in value
    ):
        raise ValueError("capsule path is invalid")
    parts = value.split("/")
    if value != unicodedata.normalize("NFC", value) or any(
        len(part.encode("utf-8")) > 255 for part in parts
    ):
        raise ValueError("capsule path component is not portable UTF-8")
    if any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in parts):
        raise ValueError("capsule path is not canonical")
    path = PurePosixPath(value)
    lowered = [p.casefold() for p in parts]
    if path.is_absolute() or any(p in _PROTECTED for p in lowered):
        raise PermissionError("capsule contains a protected path")
    name = lowered[-1]
    if (
        name in _SECRET_NAMES
        or name.startswith(".env")
        or name.endswith((".key", ".pem", ".p12", ".pfx", ".crt"))
        or any(
            re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
            for p in lowered
        )
    ):
        raise PermissionError("capsule contains a secret or reserved path")
    return value


def parse_json(data: bytes) -> Any:
    """Parse bounded JSON and reject duplicate keys and non-finite numbers."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("capsule manifest exceeds the size limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("capsule JSON contains duplicate keys")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("capsule JSON contains a non-finite number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)


def make_capsule(
    *,
    workspace_id: str,
    profile_id: str,
    plan_digest: str,
    revision: int,
    recipe_digest: str,
    files: Mapping[str, bytes],
    parent_digest: str = "",
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Seal work files and public provenance, excluding authority and host paths."""
    blobs: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []
    for path, data in sorted(files.items()):
        safe_path(path)
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise ValueError("capsule file exceeds the size limit")
        key = digest(data)
        blobs[key] = data
        entries.append({"path": path, "digest": key, "size": len(data), "mode": 420})
    manifest: dict[str, Any] = {
        "version": VERSION,
        "workspace_id": identifier(workspace_id),
        "source": {
            "profile_id": identifier(profile_id),
            "plan_digest": content_digest(plan_digest),
        },
        "revision": integer(revision, 1),
        "parent_digest": parent_digest,
        "recipe_digest": content_digest(recipe_digest),
        "files": entries,
        "total_bytes": sum(item["size"] for item in entries),
    }
    manifest["manifest_digest"] = digest(canonical(manifest))
    validate_capsule(manifest, blobs)
    return manifest, blobs


def validate_capsule(
    manifest: Mapping[str, Any],
    blobs: Mapping[str, bytes],
) -> None:
    """Verify the complete immutable manifest and exact regular-file blob set."""
    fields = {
        "version",
        "workspace_id",
        "source",
        "revision",
        "parent_digest",
        "recipe_digest",
        "files",
        "total_bytes",
        "manifest_digest",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != fields:
        raise ValueError("capsule manifest fields are invalid")
    if manifest["version"] != VERSION:
        raise ValueError("capsule version is unsupported")
    identifier(manifest["workspace_id"])
    source = manifest["source"]
    if not isinstance(source, Mapping) or set(source) != {"profile_id", "plan_digest"}:
        raise ValueError("capsule provenance fields are invalid")
    identifier(source["profile_id"])
    content_digest(source["plan_digest"])
    content_digest(manifest["recipe_digest"])
    integer(manifest["revision"], 1)
    if manifest["parent_digest"]:
        content_digest(manifest["parent_digest"])
    total = integer(manifest["total_bytes"])
    entries = manifest["files"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError("capsule file count is invalid")
    paths: set[str] = set()
    keys: set[str] = set()
    summed = 0
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {
            "path",
            "digest",
            "size",
            "mode",
        }:
            raise ValueError("capsule file fields are invalid")
        path = safe_path(entry["path"])
        alias = path.casefold()
        if alias in paths or any(
            alias.startswith(p + "/") or p.startswith(alias + "/") for p in paths
        ):
            raise ValueError("capsule contains colliding paths")
        paths.add(alias)
        key = content_digest(entry["digest"])
        size = integer(entry["size"])
        if (
            size > MAX_FILE_BYTES
            or entry["mode"] != 420
            or type(entry["mode"]) is not int
        ):
            raise ValueError("capsule file bounds or mode are invalid")
        data = blobs.get(key)
        if not isinstance(data, bytes) or len(data) != size or digest(data) != key:
            raise ValueError("capsule file digest does not match")
        keys.add(key)
        summed += size
    if set(blobs) != keys or total != summed or total > MAX_TOTAL_BYTES:
        raise ValueError("capsule aggregate bounds or blob set are invalid")
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    if len(canonical(manifest)) > MAX_MANIFEST_BYTES:
        raise ValueError("capsule manifest exceeds the size limit")
    if content_digest(manifest["manifest_digest"]) != digest(canonical(unsigned)):
        raise ValueError("capsule manifest digest does not match")


def export_archive(manifest: Mapping[str, Any], blobs: Mapping[str, bytes]) -> bytes:
    """Produce deterministic ZIP_STORED bytes with no links or host metadata."""
    validate_capsule(manifest, blobs)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        contents = {"manifest.json": canonical(manifest)}
        contents.update({"blobs/" + key[7:]: data for key, data in blobs.items()})
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
    data = output.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("capsule archive exceeds the size limit")
    return data


def import_archive(data: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Validate archive metadata and bounded bytes before any filesystem write."""
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_ARCHIVE_BYTES:
        raise ValueError("capsule archive size is invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not 2 <= len(entries) <= MAX_FILES + 1:
                raise ValueError("capsule archive entry count is invalid")
            names, total = set(), 0
            for entry in entries:
                if (
                    entry.filename in names
                    or entry.is_dir()
                    or entry.flag_bits & 1
                    or entry.compress_type != zipfile.ZIP_STORED
                    or entry.compress_size != entry.file_size
                    or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                    or entry.file_size > max(MAX_FILE_BYTES, MAX_MANIFEST_BYTES)
                    or entry.file_size > max(1, entry.compress_size) * 100
                    or (
                        entry.filename != "manifest.json"
                        and re.fullmatch(r"blobs/[0-9a-f]{64}", entry.filename) is None
                    )
                ):
                    raise ValueError("capsule archive contains an unsafe entry")
                names.add(entry.filename)
                total += entry.file_size
            if (
                "manifest.json" not in names
                or total > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES
            ):
                raise ValueError("capsule archive aggregate size is invalid")
            manifest = parse_json(archive.read("manifest.json"))
            blobs = {
                "sha256:" + name[6:]: archive.read(name)
                for name in names
                if name != "manifest.json"
            }
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise ValueError("capsule archive is invalid") from exc
    validate_capsule(manifest, blobs)
    return dict(manifest), blobs


def handoff_offer(manifest: Mapping[str, Any], expected_head: str) -> dict[str, Any]:
    """Describe future receiver CAS; this carries no writer lease or authorization."""
    if expected_head:
        content_digest(expected_head)
    return {
        "version": HANDOFF_VERSION,
        "workspace_id": manifest["workspace_id"],
        "checkpoint_digest": manifest["manifest_digest"],
        "source_revision": manifest["revision"],
        "expected_receiver_head": expected_head,
        "status": "prepared_locally",
        "remote_execution": "unavailable",
    }
