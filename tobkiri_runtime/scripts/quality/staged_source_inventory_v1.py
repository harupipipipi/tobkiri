"""Validate preserved, non-admitted Source without granting Pack authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "io.tobkiri.staged-source-inventory.v1"
STAGED_SOURCE_IDS = frozenset(
    {"tobkiri_surface_renderer_pack", "tobkiri_voice_agent_pack"}
)
INVENTORY_PATH = Path("schemas/staged_source_inventory.v1.json")
SCHEMA_PATH = Path("schemas/staged_source_inventory_v1.schema.json")
SCHEMA_DIGEST = (
    "sha256:df79878937ae04b7ea97f98348fc8209e13651d575793c44c3ef8b659d192c73"
)
MAX_DOCUMENT_BYTES = 131_072
MAX_AUTHORITY_BYTES = 2_097_152
MAX_FILE_BYTES = 1_048_576
MAX_SOURCE_FILES = 32
MAX_SOURCE_DIRECTORIES = 32
FORBIDDEN_AUTHORITY_FILES = frozenset(
    {
        "pack.v4.json",
        "contracts.v4.json",
        "executables.v4.json",
        "artifact-index.v4.json",
        "ecosystem.json",
        "rumi.pack.v3.json",
        "compatibility-alias.v1.json",
    }
)
_RECORD_KEYS = {
    "source_id",
    "source_root",
    "state",
    "runtime_authority",
    "installed_claim",
    "profile_selection",
    "files",
}
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class StagedSourceFinding:
    """One finite Source validation failure, never an admission decision."""

    path: Path
    rule: str
    detail: str


@dataclass(frozen=True)
class StagedSourceFile:
    """The exact reviewed relative path and byte digest."""

    path: str
    sha256: str


@dataclass(frozen=True)
class StagedSourceRecord:
    """A preserved Source root with no runtime or installed claim."""

    source_id: str
    source_root: str
    files: tuple[StagedSourceFile, ...]


@dataclass(frozen=True)
class StagedSourceInventory:
    """All-or-nothing validated Source classification and its evidence."""

    records: tuple[StagedSourceRecord, ...]
    findings: tuple[StagedSourceFinding, ...]
    document_digest: str | None
    schema_digest: str | None
    files_digest: str | None

    @property
    def source_ids(self) -> frozenset[str]:
        """Return no classified roots when any Source validation fails."""
        if self.findings:
            return frozenset()
        return frozenset(record.source_id for record in self.records)

    def proof(self) -> dict[str, Any]:
        """Expose separate Source evidence, with no runtime release status."""
        return {
            "schema": SCHEMA,
            "status": "RED" if self.findings else "GREEN",
            "source_ids": sorted(self.source_ids),
            "source_count": len(self.source_ids),
            "document_digest": self.document_digest,
            "schema_digest": self.schema_digest,
            "files_digest": self.files_digest,
            "records": [
                {
                    "source_id": record.source_id,
                    "source_root": record.source_root,
                    "state": "staged_unadmitted",
                    "runtime_authority": False,
                    "installed_claim": False,
                    "profile_selection": False,
                    "files": [
                        {"path": file.path, "sha256": file.sha256}
                        for file in record.files
                    ],
                }
                for record in self.records
            ],
        }


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _link_free_components(path: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    result = [(current, current.stat(follow_symlinks=False))]
    for part in absolute.parts[1:]:
        if part in {".", ".."}:
            raise ValueError("Source path has a relative component")
        current /= part
        metadata = current.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Source path component is linked")
        result.append((current, metadata))
    return tuple(result)


def _check_components(components: tuple[tuple[Path, os.stat_result], ...]) -> None:
    for path, before in components:
        after = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(after.st_mode) or (
            getattr(after, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Source path component became linked")
        if (before.st_dev, before.st_ino, before.st_mode) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
        ):
            raise ValueError("Source path component changed during verification")


def _read_regular(path: Path, limit: int) -> bytes:
    components = _link_free_components(path)
    before = components[-1][1]
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("Source file must be regular and not linked")
    if before.st_ino <= 0:
        raise ValueError("Source file identity is unavailable")
    if before.st_size > limit:
        raise ValueError("Source file exceeds its byte bound")
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    try:
        opened = os.fstat(descriptor)
        if any(getattr(before, field) != getattr(opened, field) for field in fields):
            raise ValueError("opened Source file identity differs")
        _check_components(components)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        final = (os.fstat(descriptor), path.stat(follow_symlinks=False))
        _check_components(components)
        if len(data) != before.st_size or any(
            getattr(before, field) != getattr(after, field)
            for after in final
            for field in fields
        ):
            raise ValueError("Source file changed during verification")
        return data
    finally:
        os.close(descriptor)


def _read_json(path: Path, limit: int = MAX_DOCUMENT_BYTES) -> tuple[Any, bytes]:
    raw = _read_regular(path, limit)
    return json.loads(raw, object_pairs_hook=_strict_pairs), raw


def _relative_file(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in value.split("/"))
    ):
        raise ValueError("Source path is not a safe relative file")
    return value


def _source_files(root: Path) -> dict[str, str]:
    _link_free_components(root)
    if not root.is_dir() or root.is_symlink() or root.parent.is_symlink():
        raise ValueError("Source root must be an existing unlinked directory")
    files: dict[str, str] = {}
    directories = 1
    for directory, subdirs, names in os.walk(
        root, followlinks=False, onerror=_walk_error
    ):
        directories += len(subdirs)
        if directories > MAX_SOURCE_DIRECTORIES:
            raise ValueError("staged Source contains too many directories")
        for name in subdirs:
            _link_free_components(Path(directory) / name)
        for name in names:
            if name.casefold() in FORBIDDEN_AUTHORITY_FILES:
                raise ValueError("staged Source contains an authority artifact")
            path = Path(directory) / name
            relative = _relative_file(path.relative_to(root).as_posix())
            files[relative] = (
                "sha256:"
                + hashlib.sha256(_read_regular(path, MAX_FILE_BYTES)).hexdigest()
            )
            if len(files) > MAX_SOURCE_FILES:
                raise ValueError("staged Source contains too many files")
    return files


def _walk_error(error: OSError) -> None:
    raise error


def _contains_stage_identity(value: Any) -> bool:
    if isinstance(value, str):
        return any(
            value == source_id or value.startswith(source_id + ".")
            for source_id in STAGED_SOURCE_IDS
        )
    if isinstance(value, Mapping):
        return any(
            _contains_stage_identity(key) or _contains_stage_identity(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_stage_identity(item) for item in value)
    return False


def load_staged_source_inventory(
    runtime_root: Path, catalog: Mapping[str, Any]
) -> StagedSourceInventory:
    """Validate the closed Source set; any failure retains unlisted-root guards.

    This reads bundled Source metadata only. It never imports staged code,
    discovers live external Packs, or changes an installed Profile.
    """
    runtime_root = Path(runtime_root).absolute()
    inventory_path = runtime_root / INVENTORY_PATH
    findings: list[StagedSourceFinding] = []
    records: list[StagedSourceRecord] = []
    document_digest: str | None = None
    schema_digest: str | None = None
    try:
        if runtime_root.is_symlink() or inventory_path.parent.is_symlink():
            raise ValueError("Source metadata directory is linked")
        schema, schema_bytes = _read_json(runtime_root / SCHEMA_PATH)
        schema_digest = "sha256:" + hashlib.sha256(schema_bytes).hexdigest()
        if schema_digest != SCHEMA_DIGEST or not isinstance(schema, dict):
            raise ValueError("staged Source schema differs from reviewed definition")
        payload, raw = _read_json(inventory_path)
        document_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if not isinstance(payload, dict) or set(payload) != {"schema", "records"}:
            raise ValueError("Source inventory fields are not closed")
        if payload["schema"] != SCHEMA or not isinstance(payload["records"], list):
            raise ValueError("Source inventory schema is invalid")
        rows = payload["records"]
        if len(rows) != 2:
            raise ValueError("Source inventory must contain exactly two records")
        for field in ("pack_ids", "excluded_packs"):
            values = catalog.get(field)
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise ValueError("bundled Pack inventory is invalid")
        if _contains_stage_identity(catalog):
            raise ValueError("staged Source overlaps Pack or compatibility authority")
        authority_paths = [
            "schemas/manifest_authority.v1.json",
            "schemas/executable_sources.v1.json",
            "ecosystem/defaultspack/v4/defaults.profile.v5.json",
        ]
        for relative in authority_paths:
            authority, _ = _read_json(runtime_root / relative, MAX_AUTHORITY_BYTES)
            if not isinstance(authority, dict):
                raise ValueError("bundled authority Source metadata is invalid")
            if _contains_stage_identity(authority):
                raise ValueError("staged Source has a bundled authority selection")
        for row in rows:
            if not isinstance(row, dict) or set(row) != _RECORD_KEYS:
                raise ValueError("staged Source record fields are not closed")
            source_id = row["source_id"]
            if not isinstance(source_id, str) or source_id not in STAGED_SOURCE_IDS:
                raise ValueError("staged Source identity is unknown")
            source_root = f"ecosystem/{source_id}"
            if row["source_root"] != source_root or row["state"] != "staged_unadmitted":
                raise ValueError("staged Source root or state is invalid")
            for flag in ("runtime_authority", "installed_claim", "profile_selection"):
                if type(row[flag]) is not bool or row[flag]:
                    raise ValueError("staged Source cannot claim runtime admission")
            raw_files = row["files"]
            if (
                not isinstance(raw_files, list)
                or not 1 <= len(raw_files) <= MAX_SOURCE_FILES
            ):
                raise ValueError("staged Source file set is invalid")
            declared: dict[str, str] = {}
            for file in raw_files:
                if not isinstance(file, dict) or set(file) != {"path", "sha256"}:
                    raise ValueError("staged Source file fields are not closed")
                relative = _relative_file(file["path"])
                digest = file["sha256"]
                if (
                    relative in declared
                    or not isinstance(digest, str)
                    or not _SHA256.fullmatch(digest)
                ):
                    raise ValueError("staged Source path or digest is invalid")
                declared[relative] = digest
            if list(declared) != sorted(declared):
                raise ValueError("staged Source file records must be sorted")
            observed = _source_files(runtime_root / source_root)
            if declared != observed:
                raise ValueError("staged Source file set or byte digest differs")
            records.append(
                StagedSourceRecord(
                    source_id,
                    source_root,
                    tuple(
                        StagedSourceFile(path, digest)
                        for path, digest in declared.items()
                    ),
                )
            )
        if [record.source_id for record in records] != sorted(STAGED_SOURCE_IDS):
            raise ValueError(
                "staged Source identities must be exact, unique, and sorted"
            )
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        findings.append(
            StagedSourceFinding(
                inventory_path,
                "staged_source_inventory_invalid",
                str(exc)[:240],
            )
        )
    if findings:
        return StagedSourceInventory(
            (), tuple(findings), document_digest, schema_digest, None
        )
    files_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                [
                    [record.source_id, file.path, file.sha256]
                    for record in records
                    for file in record.files
                ],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    return StagedSourceInventory(
        tuple(records), (), document_digest, schema_digest, files_digest
    )
