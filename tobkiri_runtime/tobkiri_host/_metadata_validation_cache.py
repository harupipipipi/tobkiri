"""Bounded reuse of pure, exact-byte Pack metadata validation successes.

Only the four metadata documents read during artifact capture use this cache.
Schemas, references, aliases, format checkers and semantic rules are stable
process inputs, not hot-reloaded dependencies. Validator identity is keyed so
a replacement call-site validator cannot reuse another validator's success.

No filesystem, digest, binding, materialization or authority decision is
cached. Those checks must still run on each capture, including warm reads.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.errors import ProtocolError

_MAX_DOCUMENT_BYTES = 256 * 1024
_MAX_RETAINED_BYTES = 2 * 1024 * 1024
_MAX_ENTRIES = 64
_METADATA_KINDS = frozenset(
    {"pack", "pack_contract_catalog", "pack_artifact_index", "executable_catalog"}
)
_VALIDATION_POLICY = ("pack-capture-metadata-v1", True)
_Validator = Callable[..., dict[str, Any]]


@dataclass(frozen=True, eq=False)
class _ValidatorIdentity:
    # A strong reference prevents reuse of a collected validator's object id.
    validator: _Validator

    def __hash__(self) -> int:
        return id(self.validator)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _ValidatorIdentity) and self.validator is other.validator
        )


_Key = tuple[str, tuple[str, bool], _ValidatorIdentity, bytes]


def _matches_parse(validated: Any, parsed: Any) -> bool:
    """Require the validator to preserve strict types, values and object order."""
    if type(validated) is not type(parsed):
        return False
    if isinstance(parsed, dict):
        return (
            all(type(key) is str for key in validated)
            and list(validated) == list(parsed)
            and all(
                _matches_parse(validated[key], value) for key, value in parsed.items()
            )
        )
    if isinstance(parsed, list):
        return len(validated) == len(parsed) and all(
            _matches_parse(left, right) for left, right in zip(validated, parsed)
        )
    return validated == parsed


class _MetadataValidationCache:
    """Retain success markers and raw bytes, never mutable parsed documents.

    The byte budget covers serialized keys, not process RSS or temporary
    parsing memory. Validation and parsing happen outside the mutex, allowing
    concurrent misses to validate independently.
    """

    def __init__(
        self,
        *,
        max_document_bytes: int = _MAX_DOCUMENT_BYTES,
        max_retained_bytes: int = _MAX_RETAINED_BYTES,
        max_entries: int = _MAX_ENTRIES,
    ) -> None:
        self._max_document_bytes = max_document_bytes
        self._max_retained_bytes = max_retained_bytes
        self._max_entries = max_entries
        self._reset_after_fork()

    def _reset_after_fork(self) -> None:
        # An inherited mutex may belong to a vanished thread. Never acquire it.
        self._lock = threading.Lock()
        self._entries: OrderedDict[_Key, None] = OrderedDict()
        self._retained_bytes = 0

    def validate(
        self,
        raw: Mapping[str, Any] | str | bytes,
        kind: str,
        *,
        validator: _Validator,
    ) -> dict[str, Any]:
        """Validate metadata, returning a fresh strict parse on an exact hit.

        Only plain bytes of the four supported kinds are eligible. All other
        inputs and failures keep the ordinary validator's diagnostics. The
        fixed policy always rejects authority fields, even on uncached paths.
        Validators that transform parsed data are supported but never cached.
        """
        if (
            type(raw) is not bytes
            or type(kind) is not str
            or kind not in _METADATA_KINDS
        ):
            return validator(raw, kind, reject_authority_fields=True)
        size = len(raw)
        if (
            size > self._max_document_bytes
            or size > self._max_retained_bytes
            or self._max_entries <= 0
        ):
            return validator(raw, kind, reject_authority_fields=True)
        key = (kind, _VALIDATION_POLICY, _ValidatorIdentity(validator), raw)
        with self._lock:
            hit = key in self._entries
            if hit:
                self._entries.move_to_end(key)
        if hit:
            try:
                parsed = strict_loads(raw)
            except ProtocolError:
                # Preserve the ordinary validator's wrapping and diagnostics.
                return validator(raw, kind, reject_authority_fields=True)
            if isinstance(parsed, dict):
                return parsed
            return validator(raw, kind, reject_authority_fields=True)

        validated = validator(raw, kind, reject_authority_fields=True)
        try:
            parsed = strict_loads(raw)
        except ProtocolError:
            return validated
        if not isinstance(parsed, dict) or not _matches_parse(validated, parsed):
            return validated
        with self._lock:
            # Concurrent successful misses insert and account for a key once.
            if key in self._entries:
                self._entries.move_to_end(key)
            else:
                while self._entries and (
                    len(self._entries) >= self._max_entries
                    or self._retained_bytes + size > self._max_retained_bytes
                ):
                    evicted, _ = self._entries.popitem(last=False)
                    self._retained_bytes -= len(evicted[-1])
                self._entries[key] = None
                self._retained_bytes += size
        return validated


_METADATA_VALIDATION_CACHE = _MetadataValidationCache()


def _reset_metadata_validation_cache_after_fork() -> None:
    _METADATA_VALIDATION_CACHE._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_metadata_validation_cache_after_fork)
