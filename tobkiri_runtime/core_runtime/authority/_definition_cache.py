"""Bounded reuse of byte-identical, authenticated authority definitions.

This cache holds definitions, never authorization decisions or database row
sets. Callers must read the current rows and validate their pinned key before
using it. Both encrypted bytes and their recorded digest participate in keys.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import fields
from threading import Lock
from types import MappingProxyType
from typing import TypeAlias

from .v4_models import (
    AuthorityMode,
    AuthorityScope,
    FunctionPrincipal,
    GrantLifetime,
    GrantRecord,
    ProviderAuthorityRecord,
)


_Definition: TypeAlias = GrantRecord | ProviderAuthorityRecord
_DefinitionKey: TypeAlias = tuple[str, str, str, bytes]
_MAX_ENTRIES = 4096
_MAX_KEY_BYTES = 32 * 1024 * 1024


def _deeply_immutable(value: object) -> bool:
    """Exclude mutable values accepted by permissive legacy model fields."""
    if type(value) in (str, int, float, bool, type(None), AuthorityMode, GrantLifetime):
        return True
    if type(value) is tuple:
        return all(_deeply_immutable(item) for item in value)
    if type(value) is MappingProxyType:
        return all(
            _deeply_immutable(key) and _deeply_immutable(item) for key, item in value.items()
        )
    if type(value) in (
        GrantRecord,
        ProviderAuthorityRecord,
        FunctionPrincipal,
        AuthorityScope,
    ):
        return all(_deeply_immutable(getattr(value, field.name)) for field in fields(value))
    return False


class _DefinitionCache:
    """Store immutable definitions under an entry and serialized-key byte cap."""

    def __init__(
        self,
        *,
        max_entries: int = _MAX_ENTRIES,
        max_key_bytes: int = _MAX_KEY_BYTES,
    ) -> None:
        if max_entries <= 0 or max_key_bytes <= 0:
            raise ValueError("definition cache limits must be positive")
        self._max_entries = max_entries
        self._max_key_bytes = max_key_bytes
        self._entries: OrderedDict[_DefinitionKey, tuple[_Definition, int]] = OrderedDict()
        self._key_bytes = 0
        self._lock = Lock()

    def get(self, key: _DefinitionKey) -> _Definition | None:
        """Reuse only an exact row key previously authenticated by this store."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0]

    def put(self, key: _DefinitionKey, definition: _Definition) -> None:
        """Skip oversized or mutable definitions and evict least-recent entries."""
        weight = len(key[3]) + sum(len(part.encode("utf-8")) for part in key[:3])
        if weight > self._max_key_bytes or not _deeply_immutable(definition):
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._key_bytes -= previous[1]
            self._entries[key] = (definition, weight)
            self._key_bytes += weight
            while len(self._entries) > self._max_entries or self._key_bytes > self._max_key_bytes:
                _, (_, evicted_weight) = self._entries.popitem(last=False)
                self._key_bytes -= evicted_weight

    def clear(self) -> None:
        """Release retained definitions when their owning store closes."""
        with self._lock:
            self._entries.clear()
            self._key_bytes = 0
