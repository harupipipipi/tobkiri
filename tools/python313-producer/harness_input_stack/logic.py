"""Independent source probe for the original captured-artifact verifier.

This is not a production runtime or a complete Setup path.  It consumes only a
fixed captured-input DATA callback and invokes the original Protocol function.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
import sys
from typing import Callable

from ordered_json_v2 import dumps, loads
from tobkiri_protocol.captured_inputs import CapturedFile, CapturedInputs
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.platform_artifact import verify_platform_artifact

_VARIANT_FIELDS = frozenset({
    "relative_path",
    "entrypoint",
    "artifact_digest",
    "entrypoint_digest",
    "platform",
    "architecture",
    "bundle_identity",
})


class InvalidInputError(ValueError):
    """The explicit probe initialization frame is malformed."""


class AlreadyInitializedError(RuntimeError):
    """The probe has accepted its one initialization frame already."""


class NotInitializedError(RuntimeError):
    """The original verifier cannot run before explicit initialization."""


@dataclass(frozen=True)
class _Initialized:
    files: tuple[CapturedFile, ...]
    variant: dict[str, str]
    marker: object


def _not_read_during_init(_ordinal: int, _offset: int, _length: int) -> bytes:
    raise AssertionError("initialization must not call captured read-range")


def _parse_init(payload: str) -> tuple[tuple[CapturedFile, ...], dict[str, str]]:
    if type(payload) is not str:
        raise InvalidInputError("invalid native input probe frame")
    try:
        value = loads(payload)
    except (ValueError, TypeError, OverflowError):
        raise InvalidInputError("invalid native input probe frame") from None
    if type(value) is not dict or set(value) != {"files", "variant"}:
        raise InvalidInputError("invalid native input probe frame")
    raw_files = value["files"]
    raw_variant = value["variant"]
    if type(raw_files) is not list or type(raw_variant) is not dict:
        raise InvalidInputError("invalid native input probe frame")
    if set(raw_variant) != _VARIANT_FIELDS or any(
        type(raw_variant[name]) is not str for name in _VARIANT_FIELDS
    ):
        raise InvalidInputError("invalid native input probe frame")

    files: list[CapturedFile] = []
    for item in raw_files:
        if (type(item) is not dict or set(item) != {"path", "size"}
                or type(item["path"]) is not str
                or type(item["size"]) is not int):
            raise InvalidInputError("invalid native input probe frame")
        files.append(CapturedFile(path=item["path"], size=item["size"]))
    try:
        # Reuse the original inventory checks without issuing a DATA read.  The
        # actual capture bound to native read-range is created only by run().
        CapturedInputs(files, _not_read_during_init)
    except (ProtocolError, TypeError, ValueError, OverflowError):
        raise InvalidInputError("invalid native input probe frame") from None
    return tuple(files), dict(raw_variant)


def _error_data(error: ProtocolError) -> dict[str, object]:
    return {
        "status": "protocol_error",
        "error_type": f"{type(error).__module__}.{type(error).__qualname__}",
        "error_args": list(error.args),
    }


class Logic:
    """One explicitly initialized probe instance; no authority or OS access."""

    def __init__(self) -> None:
        self._initialized: _Initialized | None = None

    def init(self, payload: str) -> None:
        """Accept exactly one valid DATA frame; failed init leaves state intact."""
        if self._initialized is not None:
            raise AlreadyInitializedError("probe is already initialized")
        files, variant = _parse_init(payload)
        self._initialized = _Initialized(files, variant, object())

    def run(self, read_range: Callable[[int, int, int], bytes]) -> str:
        """Run the untouched artifact verifier over one fresh captured reader."""
        initialized = self._initialized
        if initialized is None:
            raise NotInitializedError("probe must be initialized before run")
        marker_before = initialized.marker
        calls: list[dict[str, int | None]] = []

        def observed_read(ordinal: int, offset: int, length: int) -> bytes:
            returned = read_range(ordinal, offset, length)
            byte_count = len(returned) if type(returned) is bytes else None
            calls.append({
                "ordinal": ordinal,
                "offset": offset,
                "length": length,
                "returned": byte_count,
            })
            return returned

        captured = CapturedInputs(initialized.files, observed_read)
        artifact_root = captured.root()
        try:
            verified = verify_platform_artifact(artifact_root, initialized.variant)
        except ProtocolError as error:
            outcome: dict[str, object] = _error_data(error)
        else:
            relative = verified.relative_to(artifact_root)
            outcome = {
                "status": "verified",
                "returned_relative_path": relative.as_posix(),
            }
        marker_after = self._initialized.marker if self._initialized is not None else None
        return dumps({
            "runtime": {"python": list(sys.version_info[:3]),
                        "pointer_bits": struct.calcsize("P") * 8},
            "outcome": outcome,
            "calls": calls,
            "local_marker_same_object": (
                self._initialized is initialized and marker_before is marker_after
            ),
        })
