"""Bounded reuse of successful, record-local activation schema validation.

Only the three freshly parsed records in the activation reload path use this
cache. Keys contain their exact canonical serialization, not original disk
bytes. A hit establishes neither a record graph nor current authority: all
fresh reads, graph/projection checks, artifact checks and fences remain live.

Protocol schemas (including referenced schemas), aliases, format checkers and
semantic rules are process-stable dependencies. This is not a schema hot-reload
mechanism. The validator's identity is nevertheless keyed so replacement of a
call-site validator cannot reuse a previous implementation's success.
"""

from __future__ import annotations

import copy
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.errors import ProtocolError

_MAX_RECORD_BYTES = 512 * 1024
_MAX_RETAINED_BYTES = 2 * 1024 * 1024
_MAX_ENTRIES = 16
_RECORD_KINDS = frozenset({"profile", "profile_lock", "resolved_plan"})
_VALIDATION_POLICY = ("activation-record-local-v1", True)
_Validator = Callable[..., dict[str, Any]]


@dataclass(frozen=True, eq=False)
class _ValidatorIdentity:
    validator: _Validator

    def __hash__(self) -> int:
        return id(self.validator)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ValidatorIdentity) and self.validator is other.validator


_Key = tuple[str, tuple[str, bool], _ValidatorIdentity, bytes]


class _RecordValidationCache:
    """Retain success markers and serialized keys, never parsed record aliases.

    The byte budget covers retained serialized keys, not total process RSS or
    temporary serialization/copying memory. Validation and copying run outside
    the mutex; concurrent misses can validate independently.
    """

    def __init__(
        self,
        *,
        max_record_bytes: int = _MAX_RECORD_BYTES,
        max_retained_bytes: int = _MAX_RETAINED_BYTES,
        max_entries: int = _MAX_ENTRIES,
    ) -> None:
        self._max_record_bytes = max_record_bytes
        self._max_retained_bytes = max_retained_bytes
        self._max_entries = max_entries
        self._reset_after_fork()

    def _reset_after_fork(self) -> None:
        # Never acquire an inherited mutex: its owner may not exist in a child.
        self._lock = threading.Lock()
        self._entries: OrderedDict[_Key, None] = OrderedDict()
        self._retained_bytes = 0

    def validate(
        self,
        document: Mapping[str, Any] | str | bytes,
        kind: str,
        *,
        validator: _Validator,
    ) -> dict[str, Any]:
        """Validate a privately owned parsed record and return a fresh copy.

        The complete serialized record covers API-version schema selection.
        Unsupported inputs and serialization errors take the ordinary validator
        path, preserving its exceptions and diagnostics. Authority-field
        rejection is always enabled, including on the uncached path.
        """
        if type(document) is not dict or kind not in _RECORD_KINDS:
            return validator(document, kind, reject_authority_fields=True)
        try:
            raw = canonical_json(document)
        except (ProtocolError, TypeError, ValueError, OverflowError, RecursionError):
            return validator(document, kind, reject_authority_fields=True)
        size = len(raw)
        if (
            size > self._max_record_bytes
            or size > self._max_retained_bytes
            or self._max_entries <= 0
        ):
            return validator(document, kind, reject_authority_fields=True)
        key = (kind, _VALIDATION_POLICY, _ValidatorIdentity(validator), raw)
        with self._lock:
            hit = key in self._entries
            if hit:
                self._entries.move_to_end(key)
        if hit:
            return copy.deepcopy(document)

        validated = validator(document, kind, reject_authority_fields=True)
        with self._lock:
            # A concurrent successful miss may already have inserted this key.
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


_RECORD_VALIDATION_CACHE = _RecordValidationCache()


def _reset_record_validation_cache_after_fork() -> None:
    _RECORD_VALIDATION_CACHE._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_record_validation_cache_after_fork)
