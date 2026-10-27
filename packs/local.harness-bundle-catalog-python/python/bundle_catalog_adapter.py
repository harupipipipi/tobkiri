"""Bounded DATA carrier for the original installed-defaults bundle loader."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import math

from tobkiri_protocol.bundle_catalog import BundleIntegrityError, BundledCatalog
from tobkiri_protocol.errors import SchemaValidationError

MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024

_CONFIG_FIELDS = frozenset({"format", "lock_base64", "blobs", "reads"})
_READ_FIELDS = frozenset({"index", "path", "reread", "blob"})
_CONFIG_FORMAT = "tobkiri.bundle-defaults/2"


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


def _decode_base64(value: str) -> bytes:
    """Decode canonical standard Base64 without normalizing its input."""
    try:
        encoded = value.encode("ascii", errors="strict")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeError, ValueError, binascii.Error):
        _invalid_request()
    if base64.b64encode(decoded) != encoded:
        _invalid_request()
    return decoded


def make_owner(config: str) -> BundleDefaultsOwner:
    """Validate and retain the exact immutable installed-defaults carrier."""
    if type(config) is not str:
        _invalid_request()
    try:
        if len(config.encode("utf-8", errors="strict")) > MAX_CONFIG_BYTES:
            _limit_exceeded("bundle defaults carrier exceeds four MiB")
        value = json.loads(
            config,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_nonfinite_number,
            parse_float=_parse_finite_float,
        )
    except BundleCatalogError:
        raise
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        _invalid_request()

    if type(value) is not dict or value.keys() != _CONFIG_FIELDS:
        _invalid_request()
    if value["format"] != _CONFIG_FORMAT or type(value["format"]) is not str:
        _invalid_request()
    if (
        type(value["lock_base64"]) is not str
        or type(value["blobs"]) is not list
        or type(value["reads"]) is not list
    ):
        _invalid_request()

    lock_bytes = _decode_base64(value["lock_base64"])
    source_bytes = len(lock_bytes)
    if source_bytes > MAX_SOURCE_BYTES:
        _limit_exceeded("bundle defaults source bytes exceed four MiB")

    blobs: list[bytes] = []
    unique: set[bytes] = set()
    for encoded in value["blobs"]:
        if type(encoded) is not str:
            _invalid_request()
        raw = _decode_base64(encoded)
        source_bytes += len(raw)
        if source_bytes > MAX_SOURCE_BYTES:
            _limit_exceeded("bundle defaults source bytes exceed four MiB")
        if raw in unique:
            _invalid_request()
        unique.add(raw)
        blobs.append(raw)

    reads: list[BundleReadOccurrence] = []
    referenced: set[int] = set()
    logical_bytes = len(lock_bytes)
    for item in value["reads"]:
        if type(item) is not dict or item.keys() != _READ_FIELDS:
            _invalid_request()
        index = item["index"]
        path = item["path"]
        reread = item["reread"]
        ordinal = item["blob"]
        if (
            type(index) is not int
            or index < 0
            or type(path) is not str
            or type(reread) is not bool
            or type(ordinal) is not int
            or ordinal < 0
            or ordinal >= len(blobs)
        ):
            _invalid_request()
        raw_bytes = blobs[ordinal]
        referenced.add(ordinal)
        logical_bytes += len(raw_bytes)
        if logical_bytes > MAX_SOURCE_BYTES:
            _limit_exceeded("bundle defaults logical read bytes exceed four MiB")
        reads.append(BundleReadOccurrence(index, path, reread, raw_bytes))

    if len(referenced) != len(blobs):
        _invalid_request()

    return BundleDefaultsOwner(lock_bytes=lock_bytes, reads=tuple(reads))


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
