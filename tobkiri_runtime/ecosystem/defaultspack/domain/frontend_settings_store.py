"""Legacy settings client with no filesystem or ambient owner fallback."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from tobkiri_protocol.settings_state import (
    FrontendSettingsCorruptError as FrontendSettingsCorruptError,
    FrontendSettingsIdempotencyConflict as FrontendSettingsIdempotencyConflict,
    FrontendSettingsRevisionConflict as FrontendSettingsRevisionConflict,
    MAX_MUTATION_RECEIPTS as MAX_MUTATION_RECEIPTS,
    MUTATION_RECEIPTS_KEY as MUTATION_RECEIPTS_KEY,
    REVISION_KEY as REVISION_KEY,
    STATE_REVISIONS_KEY as STATE_REVISIONS_KEY,
    SettingsOwnerPort,
    settings_state_revision as settings_state_revision,
)


def defaultspack_frontend_settings_path(pack_root: Path | None = None) -> Path:
    """Return the durable settings path for a Defaultspack installation.

    Managed desktop packs are unpacked into a replaceable application bundle.
    The launcher supplies ``RUMI_USER_DATA`` for state that must survive a
    bundle update; an explicit path still takes precedence for tests.
    """
    override = os.environ.get("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if pack_root is not None:
        return Path(pack_root).expanduser() / "user_data" / "shared" / "frontend_settings.json"

    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if user_data:
        return (
            Path(user_data).expanduser()
            / "defaultspack"
            / "shared"
            / "frontend_settings.json"
        )
    return (
        Path(__file__).resolve().parents[1]
        / "user_data"
        / "shared"
        / "frontend_settings.json"
    )



class FrontendSettingsStore:
    """Require an explicit owner port; a legacy path does not confer access.

    The path remains diagnostic/compatibility metadata only. Unconnected
    callers fail rather than resurrecting the old writer or selecting an
    ambient Profile. This client is not a public full-document PackVM API.
    """

    def __init__(
        self, path: Path | None = None, *, owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.path = path
        self._owner = owner

    def _require_owner(self) -> SettingsOwnerPort:
        if self._owner is None:
            raise RuntimeError("explicit settings owner binding is required")
        return self._owner

    def read(self, *, preserve_corrupt: bool = False) -> dict[str, Any]:
        """Read through the explicit owner recovery policy."""
        return self._require_owner().read(preserve_corrupt=preserve_corrupt)

    def read_snapshot(self) -> dict[str, Any]:
        """Request a read-only snapshot without opening the legacy path."""
        return self._require_owner().read_snapshot()

    def compare_and_swap_document(
        self, document: Mapping[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        """Submit data only to the explicitly bound owner."""
        return self._require_owner().compare_and_swap_document(
            document, expected_revision=expected_revision,
        )

    def compare_and_swap_state(
        self, state_ref: str, document: Mapping[str, Any], result: Mapping[str, Any], *,
        expected_document_revision: int, expected_revision: int | None = None,
        idempotency_key: str | None = None, request_fingerprint: str = "",
    ) -> dict[str, Any]:
        """Submit state and receipt data, never an application callback."""
        return self._require_owner().compare_and_swap_state(
            state_ref, document, result,
            expected_document_revision=expected_document_revision,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )

    def state_revision(self, state_ref: str) -> int:
        """Read a logical revision from an owner snapshot."""
        return settings_state_revision(self.read(), state_ref)
