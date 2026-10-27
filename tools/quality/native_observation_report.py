"""Lossless, bounded CI diagnostics for native-refactor comparisons.

This file format is diagnostic only. It does not measure, change, or establish
the application product transport boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any

CHUNK_BYTES = 1024 * 1024
TOTAL_BYTES = 64 * 1024 * 1024
MANIFEST_BYTES = 4 * 1024 * 1024

_FORMAT = "tobkiri-native-observation-report"
_VERSION = 1
_PRODUCT_SCOPE = {
    "purpose": "native-refactor-comparison-only",
    "product_boundary_measurement": {"status": "unknown-not-measured", "bytes": None},
    "product_limit": {
        "bytes": 4 * 1024 * 1024,
        "status": "unchanged-not-exercised",
    },
    "hashes": "integrity-only-not-authentication",
}
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class NativeObservationReportError(ValueError):
    """The report input or stored report is invalid."""


class ReportingCapExceeded(NativeObservationReportError):
    """The bounded diagnostic report would exceed its reporting cap."""


class InvalidReport(NativeObservationReportError):
    """A saved diagnostic report is incomplete, corrupt, or unsafe."""


def _validate_json_value(value: object, *, path: str = "$", depth: int = 0) -> None:
    if depth > 128:
        raise NativeObservationReportError(f"JSON value is too deeply nested at {path}")
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise NativeObservationReportError(f"non-finite JSON number at {path}")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise NativeObservationReportError(
                    f"JSON object key is not a string at {path}"
                )
            _validate_json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise NativeObservationReportError(
        f"unsupported JSON value at {path}: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _json_bytes(value: object, *, maximum: int | None = None) -> bytes:
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise NativeObservationReportError(
            "value cannot be encoded as report JSON"
        ) from error
    if maximum is not None and len(encoded) > maximum:
        raise ReportingCapExceeded("one encoded report value exceeds the reporting cap")
    return encoded


def _safe_stem(path: Path) -> str:
    stem = path.stem
    if not _SAFE_NAME.fullmatch(stem):
        raise NativeObservationReportError("report filename stem is not a safe name")
    return stem


def _product_scope() -> dict[str, object]:
    return {
        "purpose": _PRODUCT_SCOPE["purpose"],
        "product_boundary_measurement": {
            "status": "unknown-not-measured",
            "bytes": None,
        },
        "product_limit": {
            "bytes": 4 * 1024 * 1024,
            "status": "unchanged-not-exercised",
        },
        "hashes": "integrity-only-not-authentication",
    }


def _chunk_name(stem: str, observation_ordinal: int, chunk_ordinal: int) -> str:
    return f"{stem}--{observation_ordinal:03d}--{chunk_ordinal:04d}.json.part"


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _chunk_raw(raw: bytes) -> list[bytes]:
    if type(CHUNK_BYTES) is not int or CHUNK_BYTES <= 0:
        raise NativeObservationReportError("CHUNK_BYTES must be a positive integer")
    if not raw:
        return [b""]
    return [
        raw[offset : offset + CHUNK_BYTES] for offset in range(0, len(raw), CHUNK_BYTES)
    ]


def _aggregate_size(rows: list[tuple[str, bytes]]) -> int:
    # Compact JSON object size without building a second aggregate byte string.
    return 2 + sum(
        len(_json_bytes(name)) + 1 + len(raw) + (1 if index else 0)
        for index, (name, raw) in enumerate(rows)
    )


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
    try:
        encoded = _json_bytes(manifest, maximum=MANIFEST_BYTES)
    except ReportingCapExceeded as error:
        raise ReportingCapExceeded("report manifest exceeds MANIFEST_BYTES") from error
    return encoded


def _write_exclusive(path: Path, raw: bytes, created: list[Path]) -> None:
    with path.open("xb") as stream:
        created.append(path)
        stream.write(raw)


def write_report(
    path: Path,
    observations: dict,
    *,
    source: dict,
    call_boundaries: list,
) -> dict:
    """Write all observations losslessly, then publish the complete manifest.

    The destination directory must already exist. Existing manifests or chunk
    files are never overwritten. A cap failure raises ``ReportingCapExceeded``.
    """

    if type(CHUNK_BYTES) is not int or CHUNK_BYTES <= 0:
        raise NativeObservationReportError("CHUNK_BYTES must be a positive integer")
    if type(TOTAL_BYTES) is not int or TOTAL_BYTES <= 0:
        raise NativeObservationReportError("TOTAL_BYTES must be a positive integer")
    if type(MANIFEST_BYTES) is not int or MANIFEST_BYTES <= 0:
        raise NativeObservationReportError("MANIFEST_BYTES must be a positive integer")
    if type(path) is not Path:
        path = Path(path)
    stem = _safe_stem(path)
    if type(observations) is not dict:
        raise NativeObservationReportError("observations must be an ordered dict")
    if type(source) is not dict or type(call_boundaries) is not list:
        raise NativeObservationReportError(
            "source must be a dict and call_boundaries a list"
        )
    _validate_json_value(source, path="$.source")
    _validate_json_value(call_boundaries, path="$.call_boundaries")

    observation_rows: list[tuple[str, bytes]] = []
    observation_entries = []
    planned_chunks: list[tuple[str, bytes]] = []
    planned_chunk_bytes = 0
    seen_ids: set[str] = set()
    for observation_ordinal, (observation_id, observation) in enumerate(
        observations.items()
    ):
        if (
            type(observation_id) is not str
            or not _SAFE_NAME.fullmatch(observation_id)
            or observation_id in seen_ids
        ):
            raise NativeObservationReportError(
                "observation IDs must be unique bounded safe strings"
            )
        seen_ids.add(observation_id)
        raw = _json_bytes(observation, maximum=TOTAL_BYTES)
        planned_chunk_bytes += len(raw)
        if planned_chunk_bytes > TOTAL_BYTES:
            raise ReportingCapExceeded("observation chunks exceed TOTAL_BYTES")
        observation_rows.append((observation_id, raw))
        pieces = _chunk_raw(raw)
        chunk_entries = []
        for chunk_ordinal, piece in enumerate(pieces):
            name = _chunk_name(stem, observation_ordinal, chunk_ordinal)
            chunk_entries.append(
                {
                    "path": name,
                    "ordinal": chunk_ordinal,
                    "length": len(piece),
                    "sha256": _hash(piece),
                }
            )
            planned_chunks.append((name, piece))
        observation_entries.append(
            {
                "id": observation_id,
                "ordinal": observation_ordinal,
                "length": len(raw),
                "sha256": _hash(raw),
                "chunk_count": len(chunk_entries),
                "chunks": chunk_entries,
            }
        )

    manifest: dict[str, object] = {
        "format": _FORMAT,
        "version": _VERSION,
        "complete": True,
        "scope": _product_scope(),
        "aggregate_json_bytes": _aggregate_size(observation_rows),
        "observation_count": len(observation_entries),
        "observations": observation_entries,
        "source": source,
        "call_boundaries": call_boundaries,
    }
    encoded_manifest = _manifest_bytes(manifest)
    total_bytes = len(encoded_manifest) + sum(len(raw) for _, raw in planned_chunks)
    if total_bytes > TOTAL_BYTES:
        raise ReportingCapExceeded("manifest and chunks exceed TOTAL_BYTES")

    parent = path.parent
    if not parent.is_dir():
        raise NativeObservationReportError("report destination directory must exist")
    names = [name for name, _ in planned_chunks]
    if len(names) != len(set(names)):
        raise NativeObservationReportError("generated chunk names are not unique")
    targets = [parent / name for name in names]
    for target in [*targets, path]:
        if os.path.lexists(target):
            raise FileExistsError(target)
    if any(item.name.startswith(stem + "--") for item in parent.iterdir()):
        raise FileExistsError("destination has an existing report chunk prefix")

    created: list[Path] = []
    try:
        for (name, raw), target in zip(planned_chunks, targets, strict=True):
            if target.name != name or target.parent != parent:
                raise NativeObservationReportError(
                    "generated chunk escaped destination"
                )
            _write_exclusive(target, raw, created)
        _write_exclusive(path, encoded_manifest, created)
    except BaseException:
        for created_path in reversed(created):
            try:
                created_path.unlink()
            except FileNotFoundError:
                pass
        raise
    return manifest


def _require_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise InvalidReport(f"{label} has an invalid shape")
    return value


def _require_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise InvalidReport(f"{label} must be an integer >= {minimum}")
    return value


def _require_hash(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise InvalidReport(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _read_regular_single_link(path: Path, maximum: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise InvalidReport(f"required report file is missing: {path.name}") from error
    if not stat.S_ISREG(info.st_mode):
        raise InvalidReport(f"report entry is not a regular file: {path.name}")
    if info.st_nlink != 1:
        raise InvalidReport(f"report file has a hard link: {path.name}")
    if info.st_size > maximum:
        raise ReportingCapExceeded(f"report file exceeds its cap: {path.name}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise InvalidReport(f"report file cannot be read: {path.name}") from error
    if len(raw) != info.st_size:
        raise InvalidReport(f"report file changed while being read: {path.name}")
    return raw


def _parse_json_bytes(raw: bytes, label: str) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        _validate_json_value(value)
        if _json_bytes(value) != raw:
            raise ValueError("JSON is not compact ensure_ascii encoding")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise InvalidReport(f"{label} is not valid canonical UTF-8 JSON") from error
    return value


def read_report(path: Path) -> tuple[dict, dict[str, bytes]]:
    """Verify a report completely and return its manifest and ordered raw JSON."""

    if type(TOTAL_BYTES) is not int or TOTAL_BYTES <= 0:
        raise NativeObservationReportError("TOTAL_BYTES must be a positive integer")
    if type(CHUNK_BYTES) is not int or CHUNK_BYTES <= 0:
        raise NativeObservationReportError("CHUNK_BYTES must be a positive integer")
    if type(MANIFEST_BYTES) is not int or MANIFEST_BYTES <= 0:
        raise NativeObservationReportError("MANIFEST_BYTES must be a positive integer")
    if type(path) is not Path:
        path = Path(path)
    stem = _safe_stem(path)
    manifest_raw = _read_regular_single_link(path, MANIFEST_BYTES)
    parsed = _parse_json_bytes(manifest_raw, "manifest")
    manifest = _require_keys(
        parsed,
        {
            "format",
            "version",
            "complete",
            "scope",
            "aggregate_json_bytes",
            "observation_count",
            "observations",
            "source",
            "call_boundaries",
        },
        "manifest",
    )
    if (
        type(manifest["format"]) is not str
        or manifest["format"] != _FORMAT
        or type(manifest["version"]) is not int
        or manifest["version"] != _VERSION
    ):
        raise InvalidReport("manifest format or version is unsupported")
    if manifest["complete"] is not True:
        raise InvalidReport("manifest is incomplete")
    if manifest["scope"] != _product_scope():
        raise InvalidReport("manifest product-boundary scope is invalid")
    _validate_json_value(manifest["source"], path="$.source")
    _validate_json_value(manifest["call_boundaries"], path="$.call_boundaries")
    if (
        type(manifest["source"]) is not dict
        or type(manifest["call_boundaries"]) is not list
    ):
        raise InvalidReport(
            "manifest source/call-boundary metadata has an invalid type"
        )

    rows = manifest["observations"]
    if type(rows) is not list:
        raise InvalidReport("manifest observations must be an ordered list")
    count = _require_int(manifest["observation_count"], "observation_count")
    if count != len(rows):
        raise InvalidReport("manifest observation count does not match its list")

    expected_names: set[str] = set()
    aggregate_rows: list[tuple[str, bytes]] = []
    complete_observations: dict[str, bytes] = {}
    declared_chunk_bytes = 0
    for observation_ordinal, raw_entry in enumerate(rows):
        entry = _require_keys(
            raw_entry,
            {"id", "ordinal", "length", "sha256", "chunk_count", "chunks"},
            f"observation {observation_ordinal}",
        )
        observation_id = entry["id"]
        if type(observation_id) is not str or not _SAFE_NAME.fullmatch(observation_id):
            raise InvalidReport("observation ID is not a bounded safe string")
        if observation_id in complete_observations:
            raise InvalidReport("manifest contains duplicate observation IDs")
        if _require_int(entry["ordinal"], "observation ordinal") != observation_ordinal:
            raise InvalidReport("observations are reordered or have a missing ordinal")
        raw_length = _require_int(entry["length"], "observation length")
        observation_hash = _require_hash(entry["sha256"], "observation SHA-256")
        chunk_entries = entry["chunks"]
        if type(chunk_entries) is not list:
            raise InvalidReport("observation chunks must be an ordered list")
        chunk_count = _require_int(entry["chunk_count"], "chunk_count")
        expected_count = max(1, (raw_length + CHUNK_BYTES - 1) // CHUNK_BYTES)
        if chunk_count != expected_count or len(chunk_entries) != expected_count:
            raise InvalidReport("observation chunk count is incomplete or unexpected")

        pieces = []
        for chunk_ordinal, raw_chunk in enumerate(chunk_entries):
            chunk = _require_keys(
                raw_chunk,
                {"path", "ordinal", "length", "sha256"},
                f"observation {observation_ordinal} chunk {chunk_ordinal}",
            )
            expected_name = _chunk_name(stem, observation_ordinal, chunk_ordinal)
            if chunk["path"] != expected_name:
                raise InvalidReport("chunk path is unexpected or reordered")
            if _require_int(chunk["ordinal"], "chunk ordinal") != chunk_ordinal:
                raise InvalidReport("chunks are reordered or have a missing ordinal")
            chunk_length = _require_int(chunk["length"], "chunk length")
            expected_length = min(
                CHUNK_BYTES,
                max(0, raw_length - chunk_ordinal * CHUNK_BYTES),
            )
            if chunk_length != expected_length:
                raise InvalidReport("chunk length does not match its position")
            chunk_hash = _require_hash(chunk["sha256"], "chunk SHA-256")
            if expected_name in expected_names:
                raise InvalidReport("manifest contains duplicate chunk paths")
            expected_names.add(expected_name)
            if len(manifest_raw) + declared_chunk_bytes + chunk_length > TOTAL_BYTES:
                raise ReportingCapExceeded("manifest and chunks exceed TOTAL_BYTES")
            declared_chunk_bytes += chunk_length
            chunk_raw = _read_regular_single_link(
                path.parent / expected_name, CHUNK_BYTES
            )
            if len(chunk_raw) != chunk_length or _hash(chunk_raw) != chunk_hash:
                raise InvalidReport(f"chunk integrity check failed: {expected_name}")
            pieces.append(chunk_raw)

        if len(manifest_raw) + declared_chunk_bytes > TOTAL_BYTES:
            raise ReportingCapExceeded("manifest and chunks exceed TOTAL_BYTES")
        raw = b"".join(pieces)
        if len(raw) != raw_length or _hash(raw) != observation_hash:
            raise InvalidReport(f"observation integrity check failed: {observation_id}")
        _parse_json_bytes(raw, f"observation {observation_id}")
        complete_observations[observation_id] = raw
        aggregate_rows.append((observation_id, raw))

    aggregate_size = _require_int(
        manifest["aggregate_json_bytes"], "aggregate_json_bytes"
    )
    if _aggregate_size(aggregate_rows) != aggregate_size:
        raise InvalidReport(
            "aggregate diagnostic JSON size does not match observations"
        )
    if len(manifest_raw) + declared_chunk_bytes > TOTAL_BYTES:
        raise ReportingCapExceeded("manifest and chunks exceed TOTAL_BYTES")

    try:
        siblings = list(path.parent.iterdir())
    except OSError as error:
        raise InvalidReport("report directory cannot be enumerated") from error
    for sibling in siblings:
        if sibling.name.startswith(stem + "--") and sibling.name not in expected_names:
            raise InvalidReport(f"unexpected sibling report chunk: {sibling.name}")
    return manifest, complete_observations
