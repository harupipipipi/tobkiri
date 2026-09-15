"""Data-only settings owner port and shared revision failures."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

REVISION_KEY = "_settings_revision"
STATE_REVISIONS_KEY = "_state_revisions"
MUTATION_RECEIPTS_KEY = "_mutation_receipts"
MAX_MUTATION_RECEIPTS = 64


def settings_state_revision(snapshot: Mapping[str, Any], state_ref: str) -> int:
    """Read a logical revision from the same snapshot as its associated value."""
    revisions = snapshot.get(STATE_REVISIONS_KEY, {})
    if not isinstance(revisions, dict):
        return 0
    revision = revisions.get(str(state_ref or "").strip(), 0)
    return revision if type(revision) is int and revision >= 0 else 0


class FrontendSettingsRevisionConflict(RuntimeError):
    """Raised when a state mutation targets an obsolete state revision."""

    def __init__(self, state_ref: str, expected: int, actual: int) -> None:
        super().__init__(
            f"state revision conflict for {state_ref}: expected {expected}, current {actual}"
        )
        self.state_ref = state_ref
        self.expected = expected
        self.actual = actual


class FrontendSettingsIdempotencyConflict(RuntimeError):
    """Raised when an idempotency key is reused for a different mutation."""


class FrontendSettingsCorruptError(ValueError):
    """Raised when settings cannot be decoded under the chosen recovery policy."""


class SettingsOwnerPort(Protocol):
    """Only JSON data crosses this port; persistence stays with the owner."""

    def read(self, *, preserve_corrupt: bool = False) -> dict[str, Any]:
        """Read a recoverable snapshot for an already write-capable operation."""

    def read_snapshot(self) -> dict[str, Any]:
        """Read without recovery, lock creation or migration."""

    def compare_and_swap_document(
        self, document: Mapping[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        """Commit a complete proposal without changing owner control metadata."""

    def compare_and_swap_state(
        self, state_ref: str, document: Mapping[str, Any], result: Mapping[str, Any], *,
        expected_document_revision: int, expected_revision: int | None = None,
        idempotency_key: str | None = None, request_fingerprint: str = "",
    ) -> dict[str, Any]:
        """Commit one logical-state proposal and its idempotent result."""
