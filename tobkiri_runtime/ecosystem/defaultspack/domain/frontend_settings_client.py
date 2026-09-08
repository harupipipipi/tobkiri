"""Application-side transforms over a finite, data-only settings owner port."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol

from .frontend_settings_store import FrontendSettingsRevisionConflict, REVISION_KEY


class SettingsOwnerPort(Protocol):
    """Only JSON data crosses this port; persistence stays with the owner."""

    def read(self, *, preserve_corrupt: bool = False) -> dict[str, Any]:
        """Read a recoverable snapshot for an already write-capable operation."""

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


def update_settings_document(
    owner: SettingsOwnerPort,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Recompute pure application values only after a proven document conflict."""
    for attempt in range(8):
        current = owner.read()
        revision = current.get(REVISION_KEY, 0)
        proposed = transform(current)
        try:
            return owner.compare_and_swap_document(proposed, expected_revision=revision)
        except FrontendSettingsRevisionConflict as error:
            if error.state_ref != "settings.document" or attempt == 7:
                raise
    raise AssertionError("settings retry limit was not enforced")


def update_settings_state(
    owner: SettingsOwnerPort,
    state_ref: str,
    transform: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
    *,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str = "",
) -> dict[str, Any]:
    """Keep state conflicts and ambiguous I/O failures out of the CAS retry loop."""
    for attempt in range(8):
        current = owner.read()
        revision = current.get(REVISION_KEY, 0)
        document, result = transform(current)
        try:
            return owner.compare_and_swap_state(
                state_ref, document, result, expected_document_revision=revision,
                expected_revision=expected_revision, idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except FrontendSettingsRevisionConflict as error:
            if error.state_ref != "settings.document" or attempt == 7:
                raise
    raise AssertionError("settings retry limit was not enforced")
