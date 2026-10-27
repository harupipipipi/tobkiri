"""Bounded immutable resources for the original installed-defaults reader."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import sys

from tobkiri_protocol.bundle_catalog import BundleIntegrityError, BundledCatalog
from tobkiri_protocol.errors import SchemaValidationError

MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024

_READ_FIELDS = frozenset({"index", "path", "reread", "blob"})


class BundleCatalogError(RuntimeError):
    """A typed, bounded Core error for carrier, limit, or output failures."""

    def __init__(self, code: str, message: str) -> None:
        if code not in {"invalid-request", "limit-exceeded", "projection-failed"}:
            raise ValueError("unsupported bundle catalog error code")
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BundleReadOccurrence:
    """One exact raw entry read requested by the original lock loader."""

    index: int
    path: str
    reread: bool
    raw_bytes: bytes


@dataclass(frozen=True, slots=True)
class BundleDefaultsOwner:
    """Immutable DATA owner retaining lock bytes and every read occurrence."""

    lock_bytes: bytes
    reads: tuple[BundleReadOccurrence, ...]
    resource_owner: object | None = None


@dataclass(frozen=True, slots=True)
class _ArtifactDeclarationReference:
    """Non-filesystem reference to artifact declarations retained by an owner."""

    owner: BundleDefaultsOwner


class _ReadBoundaryError(ValueError):
    """The original loader asked for an occurrence absent from its owner."""


def _invalid_request() -> None:
    """Raise a safe carrier-boundary error without echoing caller data."""
    raise BundleCatalogError("invalid-request", "invalid bundle defaults carrier")


def _limit_exceeded(message: str) -> None:
    """Raise a typed transport-bound error."""
    raise BundleCatalogError("limit-exceeded", message)


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build JSON objects while rejecting duplicate keys at every depth."""
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_nonfinite_number(token: str) -> None:
    """Reject JSON extensions such as NaN and Infinity."""
    raise ValueError("non-finite JSON number")


def _parse_finite_float(token: str) -> float:
    """Reject exponent overflow as well as named non-finite constants."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def source_identity(owner: BundleDefaultsOwner) -> dict[str, object]:
    """Describe actual retained source bytes, never authority or a parsed result."""
    # Sharing is decided by exact bytes. Hash each actual unique byte string
    # once in this observation; no domain validation result is cached.
    hashes: dict[bytes, str] = {}
    for row in owner.reads:
        if row.raw_bytes not in hashes:
            hashes[row.raw_bytes] = hashlib.sha256(row.raw_bytes).hexdigest()
    return {
        "format": "tobkiri.bundle-source-bytes/1",
        "lock": {
            "bytes": len(owner.lock_bytes),
            "sha256": hashlib.sha256(owner.lock_bytes).hexdigest(),
        },
        "reads": [
            {
                "index": row.index,
                "path": row.path,
                "reread": row.reread,
                "bytes": len(row.raw_bytes),
                "sha256": hashes[row.raw_bytes],
            }
            for row in owner.reads
        ],
    }


def identity_text(value: object) -> str:
    """Serialize a finite source identity without conflating bools and ordinals."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def embedded_owner() -> BundleDefaultsOwner:
    """Read only this actual module's fixed compiled-in immutable defaults.

    No caller config, external sidecar, OS path or previous domain result is
    accepted. Parsing and every original domain validation still occur at read.
    """
    module = sys.modules.get(__name__)
    accessor = getattr(globals().get("__loader__"), "_tobkiri_bundle_defaults", None)
    if accessor is None:
        raise BundleCatalogError(
            "projection-failed", "immutable bundle source owner is unavailable"
        )
    try:
        resource = accessor(module)
        raw = (resource / "index.json").read_bytes()
        if len(raw) > MAX_INDEX_BYTES:
            _limit_exceeded("embedded bundle source exceeds four MiB")
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_nonfinite_number,
            parse_float=_parse_finite_float,
        )
        if (
            type(value) is not dict
            or set(value) != {"format", "lock", "blobs", "reads", "source_identity"}
            or value["format"] != "tobkiri.embedded-bundle/2"
            or type(value["blobs"]) is not list
            or type(value["reads"]) is not list
        ):
            _invalid_request()

        def read_member(name, expected):
            if (
                type(expected) is not dict
                or set(expected) != {"bytes", "sha256"}
                or type(expected["bytes"]) is not int
                or not 0 <= expected["bytes"] <= MAX_SOURCE_BYTES
                or type(expected["sha256"]) is not str
                or len(expected["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in expected["sha256"])
            ):
                _invalid_request()
            member = (resource / name).read_bytes()
            if (
                len(member) != expected["bytes"]
                or hashlib.sha256(member).hexdigest() != expected["sha256"]
            ):
                _invalid_request()
            return member

        lock = read_member("lock.json", value["lock"])
        blobs = []
        unique = set()
        owned = len(lock)
        for ordinal, expected in enumerate(value["blobs"]):
            member = read_member(f"blobs/{ordinal}.json", expected)
            owned += len(member)
            if owned > MAX_SOURCE_BYTES:
                _limit_exceeded("bundle defaults source bytes exceed four MiB")
            if member in unique:
                _invalid_request()
            unique.add(member)
            blobs.append(member)
        reads = []
        referenced = set()
        logical = len(lock)
        actual = []
        for row in value["reads"]:
            if (
                type(row) is not dict
                or row.keys() != _READ_FIELDS
                or type(row["index"]) is not int
                or row["index"] < 0
                or type(row["path"]) is not str
                or type(row["reread"]) is not bool
                or type(row["blob"]) is not int
                or not 0 <= row["blob"] < len(blobs)
            ):
                _invalid_request()
            ordinal = row["blob"]
            referenced.add(ordinal)
            logical += len(blobs[ordinal])
            if logical > MAX_SOURCE_BYTES:
                _limit_exceeded("bundle defaults logical read bytes exceed four MiB")
            reads.append(
                BundleReadOccurrence(
                    row["index"], row["path"], row["reread"], blobs[ordinal]
                )
            )
            actual.append(
                {
                    "index": row["index"],
                    "path": row["path"],
                    "reread": row["reread"],
                    **value["blobs"][ordinal],
                }
            )
        if len(referenced) != len(blobs):
            _invalid_request()
        observed = {
            "format": "tobkiri.bundle-source-bytes/1",
            "lock": value["lock"],
            "reads": actual,
        }
        if identity_text(observed) != identity_text(value["source_identity"]):
            raise BundleCatalogError(
                "invalid-request", "embedded bundle source bytes changed"
            )
        return BundleDefaultsOwner(lock, tuple(reads), resource)
    except BundleCatalogError:
        raise
    except (
        ImportError,
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        raise BundleCatalogError(
            "projection-failed", "immutable bundle source is unavailable"
        ) from None


class _OccurrenceReader:
    """Return only the next exact lock-entry occurrence held by the owner."""

    def __init__(self, owner: BundleDefaultsOwner) -> None:
        self._owner = owner
        self._position = 0

    def __call__(self, index: int, path: str, reread: bool) -> bytes:
        if self._position >= len(self._owner.reads):
            raise _ReadBoundaryError("bundle reader requested an absent occurrence")
        occurrence = self._owner.reads[self._position]
        if (occurrence.index, occurrence.path, occurrence.reread) != (
            index,
            path,
            reread,
        ):
            raise _ReadBoundaryError(
                "bundle reader occurrence order did not match owner data"
            )
        self._position += 1
        return occurrence.raw_bytes

    def require_complete(self) -> None:
        """Reject extra carrier reads after the original loader has returned."""
        if self._position != len(self._owner.reads):
            raise _ReadBoundaryError("bundle owner contains unused read occurrences")


def _serialize_catalog(catalog: BundledCatalog) -> str:
    """Serialize only the five original installed-default collections."""
    try:
        result = json.dumps(
            {
                "packs": catalog.packs,
                "bases": catalog.bases,
                "shells": catalog.shells,
                "profiles": catalog.profiles,
                "executable_catalogs": catalog.executable_catalogs,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        encoded = result.encode("utf-8", errors="strict")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise BundleCatalogError(
            "projection-failed",
            "bundle catalog result serialization failed",
        ) from None
    if len(encoded) > MAX_RESULT_BYTES:
        _limit_exceeded("bundle catalog result exceeds four MiB")
    return result


def read_catalog(owner: BundleDefaultsOwner) -> str:
    """Invoke the original loader once and return only its source collections."""
    if type(owner) is not BundleDefaultsOwner:
        _invalid_request()
    reader = _OccurrenceReader(owner)
    artifact_reference = _ArtifactDeclarationReference(owner)
    try:
        # The owner and artifact reference are DATA views. Neither provides
        # __fspath__, path, existence, approval, or IO behavior.
        catalog = BundledCatalog.load_from_reader(
            owner.lock_bytes,
            entry_reader=reader,
            root=owner,
            artifact_root_resolver=lambda: artifact_reference,
        )
        reader.require_complete()
    except _ReadBoundaryError as error:
        raise BundleCatalogError("invalid-request", str(error)) from None
    except (BundleIntegrityError, SchemaValidationError) as error:
        # Keep original protocol diagnostics, but never echo the carrier or a traceback.
        raise BundleCatalogError("invalid-request", str(error)) from None
    except Exception:
        raise BundleCatalogError(
            "projection-failed",
            "bundle catalog projection failed",
        ) from None
    return _serialize_catalog(catalog)
